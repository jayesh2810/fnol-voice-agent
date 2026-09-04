"""Auto-insurance First Notice of Loss (FNOL) voice agent with fraud-signal detection.

What happens on a call:

  1. VERIFY   The caller gives policy number, full name and date of birth.
              We check them against the policy on file (policies.py).
              One retry, then a polite goodbye. Nothing is filed.
  2. INTAKE   The agent collects the accident details, then asks the caller
              to recap the sequence of events once more from the start.
  3. ANALYZE  fraud.py runs rule checks plus one LLM read of the transcript
              and produces a 0-100 risk score with quoted evidence.
  4. DECIDE   Low/medium risk -> claim is filed and a claim number is read out.
              High risk       -> claim is held; the caller is told a specialist
                                 will call back. The caller is never told why.
  5. SAVE     Everything (transcript, fields, score, flagged lines) is written
              to claims/<timestamp>_<call>.json for the review dashboard.

Run:  python main.py --chat      (type instead of talk; fastest for testing)
      python main.py             (talk through your laptop mic)
      python main.py --phone     (answer a real phone number)
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import guava
from guava import logging_utils
from guava.events import AgentSpeechEvent, BotSessionEnded, CallerSpeechEvent

from fraud import analyze
from policies import verify_identity

logger = logging.getLogger("fnol_agent")

CLAIMS_DIR = Path(__file__).resolve().parent / "claims"
MAX_VERIFY_RETRIES = 1  # one second chance, then end the call


# --- Per-call memory ---------------------------------------------------------
# The SDK gives us one Call object per conversation. Everything we learn
# during that conversation is kept here, keyed by call id, until it is saved.

@dataclass
class CallState:
    call_id: str
    started_at: str
    status: str = "in_progress"  # in_progress | unverified | filed | held_for_review | abandoned
    phase: str = "verifying"     # verifying | intake | analyzing | ended  (for the live dashboard)
    verify_attempts: int = 0
    verify_failure_reason: str | None = None
    verify_last_heard: dict | None = None  # what the speech system recorded on the last failed attempt
    policy: dict | None = None
    claim_number: str | None = None
    fields: dict = field(default_factory=dict)
    transcript: list[dict] = field(default_factory=list)
    analysis: dict | None = None


CALLS: dict[str, CallState] = {}


def _state(call: guava.Call) -> CallState:
    if call.id not in CALLS:
        CALLS[call.id] = CallState(call_id=call.id, started_at=_now())
    return CALLS[call.id]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- The agent ---------------------------------------------------------------

agent = guava.Agent(
    name="Claims intake assistant",
    organization="Northwind Auto Insurance",
    purpose=(
        "You take first notice of loss calls for auto insurance claims. "
        "Callers have usually just had an accident, so be calm, warm and patient. "
        "Verify who you are speaking to before discussing anything about a policy. "
        "Collect the facts without leading the caller or suggesting answers. "
        "Never accuse the caller of anything, never mention fraud, risk, review, "
        "or suspicion, and never explain which verification detail was wrong."
    ),
)


@agent.on_call_start
def on_call_start(call: guava.Call):
    state = _state(call)
    logger.info("Call started (%s)", state.call_id)
    start_verification(call)
    publish_live(state)


def start_verification(call: guava.Call):
    call.set_task(
        "verify",
        objective="Verify the caller's identity against their policy before anything else.",
        checklist=[
            guava.Say(
                "Thank you for calling Northwind Auto Insurance claims. "
                "I'm sorry to hear you've had an accident. I'll help you report it. "
                "First I need to verify a few details."
            ),
            guava.Field(
                key="policy_number",
                field_type="digit_sequence",
                question="What is your six-digit policy number? You can say it or key it in.",
            ),
            guava.Field(
                key="full_name",
                field_type="text",
                question="And your full name as it appears on the policy?",
            ),
            guava.Field(
                key="date_of_birth",
                field_type="date",
                question="And your date of birth?",
            ),
        ],
    )


@agent.on_task_complete("verify")
def on_verify_complete(call: guava.Call):
    state = _state(call)
    policy_number = call.get_field("policy_number")
    full_name = call.get_field("full_name")
    dob = call.get_field("date_of_birth")

    logger.info("Verifying policy=%r name=%r dob=%r", policy_number, full_name, dob)
    policy, problem = verify_identity(policy_number, full_name, dob)
    if policy is not None:
        logger.info("Verified %s on policy %s", policy["holder_name"], policy["policy_number"])
        state.policy = policy
        state.fields.update({"policy_number": policy["policy_number"], "full_name": full_name, "date_of_birth": dob})
        state.phase = "intake"
        start_intake(call, policy)
        publish_live(state)
        return

    state.verify_attempts += 1
    state.verify_failure_reason = problem
    state.verify_last_heard = {"policy_number": policy_number, "full_name": full_name, "date_of_birth": dob}
    logger.warning("Verification failed (%s): attempt %d", problem, state.verify_attempts)

    if state.verify_attempts <= MAX_VERIFY_RETRIES:
        call.retry_task(
            "The details did not match our records. Do not say which detail was wrong. "
            "Apologise, say you were not able to match those details, and ask the caller "
            "to give the policy number, full name and date of birth once more, slowly."
        )
    else:
        state.status = "unverified"
        call.hangup(
            "Say you are sorry but you were not able to verify the details today, "
            "and suggest they call back with their policy document to hand. "
            "Do not say which detail was wrong. Wish them well."
        )
    publish_live(state)


def start_intake(call: guava.Call, policy: dict):
    first_name = policy["holder_name"].split()[0]
    call.set_task(
        "intake",
        objective=(
            "Collect a complete, accurate account of the accident. Let the caller tell "
            "it in their own words. Ask one thing at a time. Do not suggest answers."
        ),
        checklist=[
            guava.Say(
                f"Thank you, {first_name}, you're verified. Let's go through what happened. "
                "Take your time."
            ),
            guava.Field(
                key="incident_date",
                field_type="date",
                question="What date did the accident happen? Please include the year.",
            ),
            guava.Field(
                key="incident_time",
                field_type="text",
                question="Roughly what time of day was it?",
                required=False,
            ),
            guava.Field(
                key="incident_location",
                field_type="text",
                question="Where did it happen? A street, intersection, or landmark is fine.",
            ),
            guava.Field(
                key="description",
                field_type="text",
                description=(
                    "Ask the caller to describe, in their own words, what happened. "
                    "Let them talk without interrupting. Record their account."
                ),
            ),
            guava.Field(
                key="vehicle",
                field_type="text",
                question="Which vehicle were you driving? Make and model, and the plate if you know it.",
            ),
            guava.Field(
                key="other_party",
                field_type="text",
                question="Was another vehicle or person involved? If so, what can you tell me about them?",
            ),
            guava.Field(
                key="police_report",
                field_type="multiple_choice",
                choices=["yes", "no"],
                question="Were the police called or a report filed?",
            ),
            guava.Field(
                key="injuries",
                field_type="multiple_choice",
                choices=["yes", "no"],
                question="Was anyone injured?",
            ),
            guava.Field(
                key="damage",
                field_type="text",
                question="Can you describe the damage to your vehicle?",
            ),
            guava.Field(
                key="witnesses",
                field_type="text",
                question="Were there any witnesses?",
                required=False,
            ),
            guava.Field(
                key="timeline_recap",
                field_type="text",
                description=(
                    "Say you want to make sure you have everything right, and ask the caller "
                    "to walk you through the sequence of events one more time from the "
                    "beginning. Record their recap word for word as best you can."
                ),
            ),
            "Tell the caller you have everything you need and you are saving the details now.",
        ],
    )


INTAKE_FIELDS = [
    "incident_date", "incident_time", "incident_location", "description", "vehicle",
    "other_party", "police_report", "injuries", "damage", "witnesses", "timeline_recap",
]


@agent.on_task_complete("intake")
def on_intake_complete(call: guava.Call):
    state = _state(call)
    for key in INTAKE_FIELDS:
        state.fields[key] = call.get_field(key)

    # Keep the caller informed while the analysis runs (it takes a few seconds).
    call.send_instruction("Let the caller know you're just saving the details and it will take a moment.")
    state.phase = "analyzing"
    publish_live(state)

    try:
        state.analysis = analyze(state.policy, state.fields, state.transcript)
    except Exception:
        logger.exception("Analysis crashed; holding the claim for a human")
        state.analysis = {"score": None, "level": "unknown", "decision": "hold_for_review",
                          "findings": [], "error": "analysis crashed", "transcript": state.transcript}

    level, score = state.analysis["level"], state.analysis["score"]
    logger.info("Risk score %s (%s) -> %s", score, level, state.analysis["decision"])

    if state.analysis["decision"] == "hold_for_review":
        # Claim is NOT written to the claims system. A human decides first.
        state.status = "held_for_review"
        call.hangup(
            "Tell the caller their report has been recorded and that a claims specialist "
            "will call them back within one business day to continue. Do not mention any "
            "review, check, concern or reason. Thank them and wish them well."
        )
    else:
        state.status = "filed"
        state.claim_number = f"CLM-{uuid.uuid4().hex[:6].upper()}"
        digits = " ".join(state.claim_number.replace("CLM-", ""))
        call.hangup(
            f"Tell the caller their claim has been filed. Read the claim number as "
            f"'C L M, {digits}', slowly. Say an adjuster will contact them within two "
            "business days. Thank them warmly."
        )
    publish_live(state)


# --- Transcript capture ------------------------------------------------------
# Every line spoken by either side is kept so the fraud review can quote it
# and the dashboard can highlight it.

@agent.on_caller_speech
def on_caller_speech(call: guava.Call, event: CallerSpeechEvent):
    state = _state(call)
    # The SDK may send a corrected version of the same utterance; keep the latest.
    if (state.transcript and event.utterance_id
            and state.transcript[-1].get("utterance_id") == event.utterance_id):
        state.transcript[-1]["text"] = event.utterance
        publish_live(state)
        return
    state.transcript.append({
        "i": len(state.transcript), "role": "caller", "text": event.utterance,
        "time": _now(), "utterance_id": event.utterance_id,
    })
    publish_live(state)


@agent.on_agent_speech
def on_agent_speech(call: guava.Call, event: AgentSpeechEvent):
    state = _state(call)
    state.transcript.append({
        "i": len(state.transcript), "role": "agent", "text": event.utterance,
        "time": _now(), "interrupted": event.interrupted,
    })
    publish_live(state)


# --- Saving the record -------------------------------------------------------

@agent.on_session_end
def on_session_end(call: guava.Call, event: BotSessionEnded):
    state = CALLS.pop(call.id, None) or _state(call)
    if state.status == "in_progress":
        state.status = "abandoned"  # caller hung up before we finished
    state.phase = "ended"
    path = save_record(state, event)
    publish_live(state, termination_reason=str(event.termination_reason), saved_as=path.name)
    logger.info("Session ended (%s). Status: %s. Saved %s", state.call_id, state.status, path.name)


def save_record(state: CallState, event: BotSessionEnded) -> Path:
    CLAIMS_DIR.mkdir(exist_ok=True)
    record = build_record(state, termination_reason=str(event.termination_reason))
    stamp = state.started_at.replace(":", "").replace("-", "").replace("+0000", "Z")
    path = CLAIMS_DIR / f"{stamp}_{state.call_id[:8]}.json"
    path.write_text(json.dumps(record, indent=2))
    return path


LIVE_PATH = CLAIMS_DIR / "live.json"


def publish_live(state: CallState, termination_reason: str | None = None, saved_as: str | None = None) -> None:
    """Write the current call to claims/live.json for the dashboard to poll.

    Written atomically (temp file + rename) so the dashboard never reads a half-written file.
    Never allowed to break the call: any error is logged and ignored.
    """
    try:
        CLAIMS_DIR.mkdir(exist_ok=True)
        record = build_record(state, termination_reason=termination_reason)
        record["saved_as"] = saved_as
        record["published_at"] = _now()
        tmp = LIVE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(record))
        tmp.replace(LIVE_PATH)
    except Exception:
        logger.exception("Could not publish live state")


def build_record(state: CallState, termination_reason: str | None = None) -> dict:
    policy_public = None
    if state.policy:
        policy_public = {k: v for k, v in state.policy.items() if k != "date_of_birth"}

    analysis = state.analysis or {}
    return {
        "call_id": state.call_id,
        "started_at": state.started_at,
        "ended_at": _now() if state.phase == "ended" else None,
        "termination_reason": termination_reason,
        "phase": state.phase,
        "status": state.status,
        "claim_number": state.claim_number,
        "verification": {
            "attempts": state.verify_attempts,
            "failure_reason": state.verify_failure_reason,
            "last_heard": state.verify_last_heard,
        },
        "policy": policy_public,
        "fields": state.fields,
        "risk": {
            "score": analysis.get("score"),
            "level": analysis.get("level"),
            "decision": analysis.get("decision"),
            "incident_date": analysis.get("incident_date"),
            "findings": analysis.get("findings", []),
            "llm_summary": analysis.get("llm_summary"),
            "llm_review_ok": analysis.get("llm_review_ok"),
            "thresholds": analysis.get("thresholds"),
            "checks": analysis.get("checks", []),
            "dropped": analysis.get("dropped", []),
            "comparison": analysis.get("comparison", []),
            "error": analysis.get("error"),
        },
        # Use the live transcript (it keeps growing after the analysis ran, e.g. the
        # claim-number readout) and copy the flags the analysis attached by line index.
        "transcript": _merge_flags(state.transcript, analysis.get("transcript") or []),
    }


def _merge_flags(live: list[dict], analyzed: list[dict]) -> list[dict]:
    flags_by_index = {t["i"]: t.get("flags", []) for t in analyzed}
    return [{**t, "flags": flags_by_index.get(t["i"], [])} for t in live]


# --- Entry point -------------------------------------------------------------

if __name__ == "__main__":
    logging_utils.configure_logging()

    parser = argparse.ArgumentParser(description="Northwind FNOL voice agent")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--chat", action="store_true", help="Text chat in the terminal (no audio).")
    group.add_argument("--local", action="store_true", help="Talk through your laptop mic (default).")
    group.add_argument("--webrtc", action="store_true", help="Get a browser link to talk to the agent.")
    group.add_argument("--phone", metavar="NUMBER", help="Answer calls on this phone number, e.g. +15555555555.")
    args = parser.parse_args()

    if args.chat:
        agent.chat()
    elif args.webrtc:
        agent.listen_webrtc()
    elif args.phone:
        agent.listen_phone(args.phone)
    else:
        agent.call_local()
