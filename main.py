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
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import guava
from guava import logging_utils
from guava.events import AgentSpeechEvent, BotSessionEnded, CallerSpeechEvent

from fraud import analyze, check_turn, live_rules, live_total
from policies import verify_identity
from redact import redact, redact_fields

logger = logging.getLogger("fnol_agent")

CLAIMS_DIR = Path(__file__).resolve().parent / "claims"
MAX_VERIFY_RETRIES = 1  # one second chance, then end the call

# Live "eyebrow" checks during the accident account (see fraud.check_turn).
LIVE_CHECKS = True            # set False to switch the feature off
MAX_PROBES_PER_CALL = 2       # follow-up questions the agent may ask on its own
PROBE_COOLDOWN_LINES = 3      # never probe again within this many caller lines
TURN_SETTLE_SECONDS = 0.8     # wait for the speech system to finish correcting a line


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
    probes: list[dict] = field(default_factory=list)   # follow-ups asked live, and why
    last_probe_line: int = -100
    fired_rules: set = field(default_factory=set)        # rule codes already counted live
    ledger: list[dict] = field(default_factory=list)     # every score change, in order
    live: dict = field(default_factory=lambda: {"score": 0, "confirmed": 0, "provisional": 0, "level": "low", "final": False})
    _timer: threading.Timer | None = None                # pending live check for the newest line
    _lock: threading.Lock = field(default_factory=threading.Lock)


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
        "or suspicion, and never explain which verification detail was wrong. "
        "Never answer questions about call recording, coverage, approval, timelines, or "
        "anything outside the checklist from your own knowledge: always request the "
        "answer from your expert and relay exactly what you are given. If the caller offers payment card "
        "or bank details, say nothing is charged on this call, ask them not to share "
        "those details, and do not repeat them."
    ),
)

# Fixed answers for questions the checklist does not cover. Plain strings so a
# compliance reviewer can read and approve them. Anything else gets the default.
RECORDING_DISCLOSURE = "This call may be recorded for accuracy and quality purposes."
APPROVED_ANSWERS = [
    (r"record|tap(e|ed|ing)|monitor", "Yes, calls to the claims line are recorded for accuracy and quality purposes."),
    (r"how long|when will|time ?frame|how soon", "An adjuster contacts you within two business days of a claim being filed."),
    (r"who (will|is going to) (call|contact)|who.*adjuster", "A Northwind claims adjuster will contact you directly."),
    (r"what happens next|next step", "Once the report is complete, an adjuster reviews it and contacts you to arrange the next steps."),
    (r"cover|deductible|pay ?out|how much|approved|approve", "I can't discuss coverage or approval on this call; the adjuster who contacts you can go through that with you."),
]
DEFAULT_ANSWER = "I can't answer that on this call, but the adjuster who contacts you will be able to."


@agent.on_question
def on_question(call: guava.Call, question: str) -> str:
    import re
    for pattern, answer in APPROVED_ANSWERS:
        if re.search(pattern, question, re.I):
            logger.info("Question answered from the approved list: %s", question)
            return answer
    logger.info("Question outside the approved list: %s", question)
    return DEFAULT_ANSWER


@agent.on_call_start
def on_call_start(call: guava.Call):
    state = _state(call)
    logger.info("Call started (%s)", state.call_id)
    call.read_script(RECORDING_DISCLOSURE)  # spoken word for word, before anything else
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
        recompute_live(call, state, line=len(state.transcript) - 1, publish=False)
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
    state.fields = redact_fields(state.fields)

    # Keep the caller informed while the analysis runs (it takes a few seconds).
    call.send_instruction("Let the caller know you're just saving the details and it will take a moment.")
    state.phase = "analyzing"
    if state._timer:
        state._timer.cancel()
    publish_live(state)

    try:
        state.analysis = analyze(state.policy, state.fields, list(state.transcript), state.probes)
    except Exception:
        logger.exception("Analysis crashed; holding the claim for a human")
        state.analysis = {"score": None, "level": "unknown", "decision": "hold_for_review",
                          "findings": [], "error": "analysis crashed", "transcript": state.transcript}

    level, score = state.analysis["level"], state.analysis["score"]
    logger.info("Risk score %s (%s) -> %s", score, level, state.analysis["decision"])
    finalize_live(state)

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
        state.transcript[-1]["text"] = redact(event.utterance)
        publish_live(state)
        return
    state.transcript.append({
        "i": len(state.transcript), "role": "caller", "text": redact(event.utterance),
        "time": _now(), "utterance_id": event.utterance_id,
    })
    recompute_live(call, state, line=len(state.transcript) - 1, publish=False)
    publish_live(state)
    schedule_live_check(call, state)


# --- Live suspicion: raise an eyebrow, ask one follow-up ----------------------
# After each caller line during the accident account, a background check asks
# the helper model whether the line is vague or contradicts something earlier.
# If so, the agent is nudged to ask one neutral follow-up before moving on.
# The check runs off the event thread so the conversation is never held up.

def _open_probe(state: CallState) -> dict | None:
    return next((p for p in reversed(state.probes) if not p.get("resolved")), None)


def schedule_live_check(call: guava.Call, state: CallState) -> None:
    if not LIVE_CHECKS or state.phase != "intake" or state.policy is None:
        return
    line = state.transcript[-1]
    can_probe = len(state.probes) < MAX_PROBES_PER_CALL and line["i"] - state.last_probe_line >= PROBE_COOLDOWN_LINES
    if not can_probe and _open_probe(state) is None:
        return  # nothing to ask and nothing to resolve
    # The speech system may still revise this line; wait a moment, then check the final text.
    if state._timer:
        state._timer.cancel()
    state._timer = threading.Timer(TURN_SETTLE_SECONDS, run_live_check, args=(call, state, line["i"]))
    state._timer.daemon = True
    state._timer.start()


def run_live_check(call: guava.Call, state: CallState, line_index: int) -> None:
    try:
        if state.phase != "intake":
            return
        snapshot = list(state.transcript)
        latest = next((t for t in snapshot if t["i"] == line_index), None)
        if latest is None or latest["role"] != "caller":
            return
        fields_so_far = {k: call.get_field(k) for k in INTAKE_FIELDS if call.has_field(k)}
        open_probe = _open_probe(state)
        verdict = check_turn(state.policy, fields_so_far, snapshot, latest, open_probe)

        if open_probe and verdict["resolves"]:
            with state._lock:
                open_probe["resolved"] = True
                open_probe["resolved_line"] = line_index
            add_ledger(state, line_index, -open_probe["points"], "cleared", open_probe["kind"],
                       f"Follow-up answered clearly; {open_probe['kind']} no longer counts", provisional=True)
            logger.info("Follow-up resolved at line %d", line_index)

        result = verdict["probe"]
        if result is None:
            recompute_live(call, state, line_index)
            return
        with state._lock:
            if len(state.probes) >= MAX_PROBES_PER_CALL or line_index - state.last_probe_line < PROBE_COOLDOWN_LINES:
                recompute_live(call, state, line_index)
                return
            state.probes.append({**result, "time": _now(), "resolved": False})
            state.last_probe_line = line_index
        add_ledger(state, line_index, result["points"], "live", result["kind"], result["reason"], provisional=True)
        logger.info("Eyebrow raised at line %d (%s): %s -> asking: %s", line_index, result["kind"], result["reason"], result["question"])
        call.send_instruction(
            "Before moving on to the next item, ask this one follow-up question in a warm, neutral "
            f"tone, exactly once: \"{result['question']}\" Do not say or imply that anything the "
            "caller said was wrong or inconsistent. Accept whatever they answer and continue."
        )
        recompute_live(call, state, line_index)
    except Exception:
        logger.exception("Live check failed; ignoring")


@agent.on_agent_speech
def on_agent_speech(call: guava.Call, event: AgentSpeechEvent):
    state = _state(call)
    state.transcript.append({
        "i": len(state.transcript), "role": "agent", "text": redact(event.utterance),
        "time": _now(), "interrupted": event.interrupted,
    })
    # Collected answers usually land just before the agent's next line, so check the rules here too.
    recompute_live(call, state, line=len(state.transcript) - 1, publish=False)
    publish_live(state)


# --- Live score --------------------------------------------------------------
# Solid points come from rule checks on facts collected so far. Striped
# ("provisional") points come from the live check and can be taken back when
# a follow-up is answered clearly. The final review replaces the estimate.

def add_ledger(state: CallState, line: int, delta: int, kind: str, code: str, label: str, provisional: bool) -> None:
    state.ledger.append({"time": _now(), "line": line, "delta": delta, "kind": kind,
                         "code": code, "label": label, "provisional": provisional})


def recompute_live(call: guava.Call, state: CallState, line: int, publish: bool = True) -> None:
    if state.policy is None or state.live.get("final"):
        return
    try:
        fields = {k: call.get_field(k) for k in INTAKE_FIELDS if call.has_field(k)}
        for finding in live_rules(state.policy, fields):
            if finding["code"] not in state.fired_rules:
                state.fired_rules.add(finding["code"])
                add_ledger(state, line, finding["weight"], "rule", finding["code"], finding["explanation"], provisional=False)
        confirmed = sum(e["delta"] for e in state.ledger if not e["provisional"])
        provisional = max(0, sum(e["delta"] for e in state.ledger if e["provisional"]))
        score, level = live_total(confirmed, provisional)
        state.live = {"score": score, "confirmed": confirmed, "provisional": provisional, "level": level, "final": False}
        if publish:
            publish_live(state)
    except Exception:
        logger.exception("Live score update failed; ignoring")


def finalize_live(state: CallState) -> None:
    """The end-of-call review replaces the live estimate."""
    final = state.analysis or {}
    if final.get("score") is None:
        return
    delta = final["score"] - state.live.get("score", 0)
    add_ledger(state, len(state.transcript) - 1, delta, "final", "final_review",
               "Final review replaces the live estimate", provisional=False)
    state.live = {"score": final["score"], "confirmed": final["score"], "provisional": 0,
                  "level": final["level"], "final": True}


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
        "probes": state.probes,
        "live": {**state.live, "ledger": state.ledger},
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
