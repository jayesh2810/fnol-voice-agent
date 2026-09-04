"""Fraud-signal analysis for a First Notice of Loss call.

Two layers, run after the caller has told their story:

1. RULES  - plain Python checks on hard facts (policy dates, status, claim
            history, vehicle on file). Cheap, deterministic, easy to explain.
2. LLM    - one call to Guava's hosted model that reads the whole transcript
            and lists contradictions or evasive answers, quoting the caller's
            exact words. Catches story-level problems the rules can't see.

Both layers produce "findings". Findings are weighted into a 0-100 risk
score, and the score decides whether the claim is filed now or held for a
human reviewer. The caller is never told about any of this.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from policies import parse_date

logger = logging.getLogger("fnol_agent.fraud")

# --- Tunables ----------------------------------------------------------------

RECENT_POLICY_DAYS = 30      # incident this soon after policy start is worth a look
MAX_PRIOR_CLAIMS_12M = 1     # more than this many claims in a year is worth a look

RISK_MEDIUM = 30             # score >= this -> "medium"
RISK_HIGH = 60               # score >= this -> "high" -> hold the claim

SEVERITY_WEIGHT = {"low": 10, "medium": 20, "high": 60}  # one "high" holds the claim on its own


# Every rule, with the sentence shown when it does NOT fire. The dashboard uses
# this to show the checks that passed, so a reviewer sees what was verified,
# not only what went wrong.
RULE_CATALOG = {
    "policy_not_active": "Policy is active",
    "incident_before_policy_start": "Incident happened after the policy started",
    "recent_policy": f"Policy was more than {RECENT_POLICY_DAYS} days old at the incident",
    "incident_in_future": "Incident date is not in the future",
    "incident_date_unclear": "Incident date was understood",
    "claim_frequency": f"No more than {MAX_PRIOR_CLAIMS_12M} prior claim(s) in 12 months",
    "vehicle_mismatch": "Vehicle described matches the vehicle on file",
    "plate_mismatch": "Licence plate stated matches the plate on file",
    "injuries_without_police_report": "No injuries reported without police involvement",
    "liability_only_coverage": "Coverage includes damage to the caller's own vehicle",
}


def _finding(source: str, code: str, severity: str, explanation: str, quotes: list[str] | None = None) -> dict:
    return {
        "source": source,            # "rule" or "llm"
        "code": code,                # short machine-readable label
        "severity": severity,        # low | medium | high
        "weight": SEVERITY_WEIGHT[severity],
        "explanation": explanation,  # one sentence a human reviewer can read
        "quotes": quotes or [],      # verbatim caller lines that support it
    }


# --- Layer 1: rules ----------------------------------------------------------

def _normalize(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


_PLATE_AFTER_WORD = re.compile(r"(?:licen[cs]e\s*)?(?:plate|registration|reg)\s*(?:number|no\.?|is|was|:)?\s*((?:[A-Za-z0-9][\s\-]?){4,9})", re.I)
_PLATE_TOKEN = re.compile(r"\b(?=[A-Z0-9\-]*[A-Z])(?=[A-Z0-9\-]*\d)[A-Z0-9\-]{5,8}\b")


def stated_plate(text: str | None) -> str | None:
    """Pull a licence plate out of what the caller said, or None if they did not give one."""
    if not text:
        return None
    m = _PLATE_AFTER_WORD.search(text)
    if m:
        cand = re.sub(r"[^A-Z0-9]", "", m.group(1).upper())
        if 4 <= len(cand) <= 9:
            return cand
    m = _PLATE_TOKEN.search(text.upper())
    return re.sub(r"[^A-Z0-9]", "", m.group(0)) if m else None


def _chars_differ(a: str, b: str) -> int:
    if len(a) != len(b):
        return abs(len(a) - len(b)) + sum(x != y for x, y in zip(a, b))
    return sum(x != y for x, y in zip(a, b))


def run_rules(policy: dict, fields: dict, incident_date: date | None) -> list[dict]:
    findings: list[dict] = []
    today = date.today()
    policy_start = date.fromisoformat(policy["policy_start"])

    if policy["status"] != "active":
        findings.append(_finding(
            "rule", "policy_not_active", "high",
            f"Policy status is '{policy['status']}'"
            + (f" since {policy['lapsed_on']}" if policy.get("lapsed_on") else "")
            + "; no cover was in force.",
        ))

    if incident_date is not None:
        if incident_date > today:
            findings.append(_finding(
                "rule", "incident_in_future", "medium",
                f"Stated incident date {incident_date} is in the future; likely misheard, needs confirming.",
            ))
        elif incident_date < policy_start:
            findings.append(_finding(
                "rule", "incident_before_policy_start", "high",
                f"Incident date {incident_date} is before the policy started on {policy_start}.",
            ))
        elif (incident_date - policy_start).days <= RECENT_POLICY_DAYS:
            days = (incident_date - policy_start).days
            findings.append(_finding(
                "rule", "recent_policy", "medium",
                f"Incident occurred only {days} day(s) after the policy started on {policy_start}.",
            ))
    else:
        findings.append(_finding(
            "rule", "incident_date_unclear", "low",
            f"Could not turn the stated incident date '{fields.get('incident_date')}' into a real date.",
        ))

    if policy["prior_claims_12m"] > MAX_PRIOR_CLAIMS_12M:
        findings.append(_finding(
            "rule", "claim_frequency", "medium",
            f"{policy['prior_claims_12m']} prior claims in the last 12 months.",
        ))

    vehicle = policy["vehicle"]
    spoken_vehicle = _normalize(fields.get("vehicle"))
    on_file = [_normalize(vehicle["make"]), _normalize(vehicle["model"]), _normalize(vehicle["plate"])]
    if spoken_vehicle and not any(part and part in spoken_vehicle for part in on_file):
        findings.append(_finding(
            "rule", "vehicle_mismatch", "medium",
            f"Caller described '{fields.get('vehicle')}' but the policy covers a "
            f"{vehicle['year']} {vehicle['make']} {vehicle['model']} (plate {vehicle['plate']}).",
        ))

    plate_said = stated_plate(fields.get("vehicle"))
    plate_on_file = _normalize(vehicle["plate"]).upper()
    if plate_said and plate_said != plate_on_file:
        diff = _chars_differ(plate_said, plate_on_file)
        findings.append(_finding(
            "rule", "plate_mismatch", "medium",
            f"Caller gave plate {plate_said}; the plate on file is {vehicle['plate']} "
            f"({diff} character{'s' if diff != 1 else ''} different"
            + ("; could be a mishearing, needs confirming" if diff <= 2 else "") + ").",
        ))

    if str(fields.get("injuries", "")).lower() == "yes" and str(fields.get("police_report", "")).lower() == "no":
        findings.append(_finding(
            "rule", "injuries_without_police_report", "low",
            "Injuries reported but no police involvement; unusual for an injury accident.",
        ))

    if policy["coverage"] == "liability_only":
        findings.append(_finding(
            "rule", "liability_only_coverage", "low",
            "Policy is liability-only; damage to the caller's own vehicle is not covered. Adjuster to confirm what is being claimed.",
        ))

    return findings


# --- Layer 2: LLM transcript review -------------------------------------------

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "incident_date_iso": {"type": ["string", "null"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["contradiction", "changed_story", "vague_or_evasive", "implausible_detail", "other"]},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "incompatible": {"type": "boolean"},
                    "explanation": {"type": "string"},
                    "caller_quotes": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["category", "severity", "incompatible", "explanation", "caller_quotes"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["incident_date_iso", "findings", "summary"],
}


def _build_prompt(policy: dict, fields: dict, transcript: list[dict], probes: list[dict] | None = None) -> str:
    lines = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in transcript)
    vehicle = policy["vehicle"]
    probe_note = ""
    if probes:
        probe_note = "\nFOLLOW-UP QUESTIONS THE AGENT ASKED DURING THE CALL\n" + "\n".join(
            f"- after line {p['line']} ({p['kind']}): {p['question']}" for p in probes
        ) + "\nIf the caller's answer to a follow-up clearly resolved the concern, treat it as compatible and do not report it.\n"
    return f"""You are a claims-review analyst for an auto insurer. Read the transcript of a
First Notice of Loss phone call and look for signs that the caller's story is not consistent.

Be conservative. The caller has just had an accident and may be stressed, so small slips,
approximate times, or hesitation are NOT findings on their own. Adding a new detail in the
recap that was simply not mentioned before (a traffic light colour, the weather, a street name)
is NOT a finding either; only report the recap if it is INCOMPATIBLE with the first account.
Only report things a careful human reviewer would want to look at:
- the caller states two incompatible facts (date, location, who was driving, sequence of events)
- the story changes materially when they are asked to recap it
- they avoid or deflect a direct question more than once
- a detail is physically or logically implausible

For each finding set "incompatible" to true ONLY if the quoted statements cannot both be
true at the same time. A recap that adds a colour, a name, or a street is compatible with the
first account, so incompatible is false and it should not be reported at all.

Report each inconsistency ONCE, under the single best category; never list the same
pair of statements twice. Only report what the caller actually said. Do not speculate about
what "could" or "might" have been true (for example, who else may have been present).

For every finding, copy the caller's exact words that support it into caller_quotes
(verbatim substrings of CALLER lines only). If the transcript is consistent, return an
empty findings list and say so in the summary.

Also convert the stated incident date to ISO format (YYYY-MM-DD) in incident_date_iso.
Today's date is {date.today().isoformat()}. Use null if you cannot tell.

POLICY ON FILE
- Holder: {policy['holder_name']}
- Vehicle: {vehicle['year']} {vehicle['make']} {vehicle['model']}, plate {vehicle['plate']}
- Policy start: {policy['policy_start']}, status: {policy['status']}, coverage: {policy['coverage']}

ANSWERS THE AGENT RECORDED
{json.dumps(fields, indent=2)}

{probe_note}
TRANSCRIPT
{lines}
"""


_CORE_EVENT = re.compile(
    r"\b(hit|struck|collid|crash|ran (over|into)|rear[- ]end|from behind|front|side[- ]?swipe|pedestrian|cyclist|"
    r"stopped|stationary|parked|driving|moving|turning|reversing|red light|green light|who (hit|was driving)|"
    r"sequence|order of events|first|then)\b", re.I)


def _touches_core_event(item: dict) -> bool:
    text = item.get("explanation", "") + " " + " ".join(item.get("caller_quotes", []))
    return bool(_CORE_EVENT.search(text))


# --- Recap versus first account: a direct, narrow comparison ------------------

_RECAP_SCHEMA = {
    "type": "object",
    "properties": {
        "compatible": {"type": "boolean"},
        "core_event_changed": {"type": "boolean"},
        "explanation": {"type": "string"},
    },
    "required": ["compatible", "core_event_changed", "explanation"],
}


def compare_recap(fields: dict) -> dict | None:
    """Ask one narrow question: can the first account and the recap both be true?

    Returns a finding dict, or None when they agree (or when either is missing).
    """
    from guava.helpers.llm import generate

    first, recap = fields.get("description"), fields.get("timeline_recap")
    if not first or not recap:
        return None
    prompt = f"""Two descriptions of the same car accident, given by the same caller a few minutes apart.

FIRST ACCOUNT: {first}
RECAP: {recap}

Can both be true at the same time? Adding detail (a colour, a name, a street, the light being red)
is compatible. Set compatible=false only if a fact in one rules out a fact in the other.
Set core_event_changed=true if the disagreement is about who hit whom, the direction of impact,
whether the caller's car was moving or stopped, who else was involved, or the order of events.
Explain in one sentence."""
    try:
        data = json.loads(generate(prompt, json_schema=_RECAP_SCHEMA))
    except Exception:
        logger.exception("Recap comparison failed")
        return None
    if data.get("compatible", True):
        return None
    severity = "high" if data.get("core_event_changed") else "medium"
    return _finding("llm", "recap_conflicts_with_account", severity, data.get("explanation", ""), [first, recap])


def run_llm_review(policy: dict, fields: dict, transcript: list[dict], probes: list[dict] | None = None) -> tuple[list[dict], list[dict], date | None, str, bool]:
    """Returns (findings, dropped, incident_date, summary, succeeded)."""
    from guava.helpers.llm import generate  # imported here so tests can stub it

    try:
        raw = generate(_build_prompt(policy, fields, transcript, probes), json_schema=_LLM_SCHEMA)
        data = json.loads(raw)
    except Exception as exc:  # network, auth, or bad JSON
        logger.exception("LLM transcript review failed")
        return [], [], None, f"LLM review failed: {exc}", False

    findings, dropped = [], []
    for item in data.get("findings", []):
        if item.get("severity") not in SEVERITY_WEIGHT:
            continue
        # A "contradiction" or "changed story" only counts if the model itself says the two
        # statements cannot both be true. Extra detail on a recap is not a finding.
        # Exception: anything about the core event (who hit whom, direction, sequence) is
        # never dropped, whatever the flag says.
        if (item["category"] in ("contradiction", "changed_story") and not item.get("incompatible", True)
                and not _touches_core_event(item)):
            logger.info("Dropping compatible story finding: %s", item["explanation"])
            dropped.append(_finding("llm", item["category"], item["severity"], item["explanation"], item.get("caller_quotes", [])))
            continue
        findings.append(_finding("llm", item["category"], item["severity"], item["explanation"], item.get("caller_quotes", [])))
    incident_date = parse_date(data.get("incident_date_iso"))
    return findings, dropped, incident_date, data.get("summary", ""), True


# --- Live score: what can be known before the call ends -----------------------

PROVISIONAL_POINTS = {"vague": 10, "contradiction": 20}   # "maybe" points from the live check


def live_rules(policy: dict, fields: dict) -> list[dict]:
    """Run only the rule checks whose inputs have been collected so far.

    Same rules as run_rules, but a missing incident date is not a finding yet.
    """
    incident_date = parse_date(fields.get("incident_date")) if "incident_date" in fields else None
    findings = run_rules(policy, fields, incident_date)
    if "incident_date" not in fields:
        findings = [x for x in findings if x["code"] != "incident_date_unclear"]
    return findings


def live_total(confirmed: int, provisional: int) -> tuple[int, str]:
    return score_findings([{"weight": confirmed}, {"weight": provisional}])


# --- Layer 2b: live "eyebrow" check, one caller line at a time ---------------
# Runs during the call, right after the caller says something. It looks only at
# the newest line against what came before and, if something is vague or does
# not fit, hands back ONE neutral follow-up question for the agent to ask.

_TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "suspicious": {"type": "boolean"},
        "kind": {"type": "string", "enum": ["none", "vague", "contradiction"]},
        "reason": {"type": "string"},
        "follow_up_question": {"type": "string"},
        "resolves_open_follow_up": {"type": "boolean"},
    },
    "required": ["suspicious", "kind", "reason", "follow_up_question", "resolves_open_follow_up"],
}


def _build_turn_prompt(policy: dict, fields: dict, transcript: list[dict], latest: dict, open_probe: dict | None = None) -> str:
    earlier = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in transcript if t["i"] < latest["i"])
    v = policy["vehicle"]
    open_note = ""
    if open_probe:
        open_note = f"""
AN OPEN FOLLOW-UP QUESTION WAS JUST ASKED: "{open_probe['question']}" (because: {open_probe['reason']})
If the newest caller line answers it clearly and specifically, set resolves_open_follow_up=true.
If it dodges it or makes things less clear, set it false. Otherwise false.
"""
    return f"""You are listening in on a live insurance claims call. Judge ONLY the caller's newest line.
{open_note}
Decide whether it deserves one gentle follow-up question right now. Say suspicious=true only if:
- kind="vague": it is so vague or evasive that a claims handler could not act on it ("somewhere
  downtown", "it's complicated", "I'd rather not say") when a specific answer was asked for, OR
- kind="contradiction": it states a fact that cannot be true together with a fact the caller
  stated EARLIER on this call (a different day, place, vehicle, or sequence of events).
A non-answer is "vague", never "contradiction". Use "contradiction" only when you can point to
the earlier line it clashes with.

Do NOT flag: short answers that are still specific ("no", "yes", "around 6pm"), approximate times,
missing detail that was not asked for, nervous or informal phrasing, or an answer that simply adds
new information. When in doubt, suspicious=false. Most lines are fine.

If suspicious, write ONE follow-up question the agent can ask in a warm, neutral tone. It must ask
for a fact (which day, which street, which car) and must never say or imply the caller is wrong or
suspected. Good: "Just so I have it right, was that on Tuesday or on Wednesday?" Bad: "You said
something different before."

POLICY ON FILE: {policy['holder_name']}, {v['year']} {v['make']} {v['model']} plate {v['plate']}, started {policy['policy_start']}.
ANSWERS RECORDED SO FAR: {json.dumps({k: val for k, val in fields.items() if k not in ('date_of_birth',)})}

EARLIER LINES
{earlier or '(none yet)'}

NEWEST CALLER LINE
{latest['text']}
"""


def check_turn(policy: dict, fields: dict, transcript: list[dict], latest: dict, open_probe: dict | None = None) -> dict:
    """Judge the newest caller line.

    Returns {"probe": {...} or None, "resolves": bool}. "probe" is a follow-up
    suggestion when the line is vague or contradictory; "resolves" is true when
    the line clearly answers the open follow-up question, if there was one.
    Any failure (network, bad JSON) returns an empty verdict: a live check must
    never disturb the call.
    """
    from guava.helpers.llm import generate

    empty = {"probe": None, "resolves": False}
    try:
        raw = generate(_build_turn_prompt(policy, fields, transcript, latest, open_probe), json_schema=_TURN_SCHEMA)
        data = json.loads(raw)
    except Exception:
        logger.exception("Live turn check failed")
        return empty
    verdict = {"probe": None, "resolves": bool(open_probe) and bool(data.get("resolves_open_follow_up"))}
    if data.get("suspicious") and data.get("kind") in ("vague", "contradiction") and data.get("follow_up_question", "").strip():
        verdict["probe"] = {
            "line": latest["i"],
            "kind": data["kind"],
            "reason": data.get("reason", ""),
            "question": data["follow_up_question"].strip(),
            "points": PROVISIONAL_POINTS[data["kind"]],
        }
    return verdict


# --- Scoring and transcript highlighting -------------------------------------

def score_findings(findings: list[dict]) -> tuple[int, str]:
    total = min(100, sum(f["weight"] for f in findings))
    if total >= RISK_HIGH:
        return total, "high"
    if total >= RISK_MEDIUM:
        return total, "medium"
    return total, "low"


def _loose(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def highlight_transcript(transcript: list[dict], findings: list[dict]) -> list[dict]:
    """Mark which caller lines the findings point at, so a dashboard can highlight them.

    Each transcript entry gets a "flags" list of finding indexes.
    """
    out = []
    for entry in transcript:
        flags: list[int] = []
        if entry["role"] == "caller":
            line = _loose(entry["text"])
            for idx, finding in enumerate(findings):
                for quote in finding["quotes"]:
                    q = _loose(quote)
                    if len(q) >= 8 and (q in line or (len(line) >= 8 and line in q)):
                        flags.append(idx)
                        break
        out.append({**entry, "flags": flags})
    return out


def compare_policy_to_statements(policy: dict, fields: dict, incident_date: date | None) -> list[dict]:
    """Side-by-side of what the policy says versus what the caller said."""
    v = policy["vehicle"]
    spoken_vehicle = _normalize(fields.get("vehicle"))
    vehicle_ok = any(_normalize(p) in spoken_vehicle for p in (v["make"], v["model"], v["plate"])) if spoken_vehicle else None
    start = date.fromisoformat(policy["policy_start"])
    date_ok = None if incident_date is None else (start <= incident_date <= date.today())
    plate_said = stated_plate(fields.get("vehicle"))
    plate_ok = None if not plate_said else plate_said == _normalize(v["plate"]).upper()
    return [
        {"label": "Vehicle", "on_file": f"{v['year']} {v['make']} {v['model']}",
         "stated": fields.get("vehicle") or "", "ok": vehicle_ok},
        {"label": "Licence plate", "on_file": v["plate"],
         "stated": plate_said or "(not stated)", "ok": plate_ok},
        {"label": "Incident date", "on_file": f"policy started {policy['policy_start']}",
         "stated": incident_date.isoformat() if incident_date else str(fields.get("incident_date") or ""), "ok": date_ok},
        {"label": "Policy status", "on_file": policy["status"] + (f" since {policy['lapsed_on']}" if policy.get("lapsed_on") else ""),
         "stated": "claim being filed", "ok": policy["status"] == "active"},
        {"label": "Claims in last 12 months", "on_file": str(policy["prior_claims_12m"]),
         "stated": "new claim", "ok": policy["prior_claims_12m"] <= MAX_PRIOR_CLAIMS_12M},
        {"label": "Coverage", "on_file": policy["coverage"].replace("_", " "),
         "stated": fields.get("damage") or "", "ok": policy["coverage"] != "liability_only"},
    ]


# --- Entry point -------------------------------------------------------------

def analyze(policy: dict, fields: dict, transcript: list[dict], probes: list[dict] | None = None) -> dict:
    """Run both layers and produce the report saved next to the claim."""
    llm_findings, dropped, llm_incident_date, summary, llm_ok = run_llm_review(policy, fields, transcript, probes)

    incident_date = parse_date(fields.get("incident_date")) or llm_incident_date
    rule_findings = run_rules(policy, fields, incident_date)

    recap_finding = compare_recap(fields) if llm_ok else None
    if recap_finding:
        # The direct comparison wins; drop transcript findings that quote the recap so the
        # same clash is not counted twice.
        recap_text = _loose(fields.get("timeline_recap") or "")
        llm_findings = [x for x in llm_findings
                        if x["code"] not in ("changed_story", "contradiction")
                        or not any(_loose(q) and _loose(q) in recap_text for q in x["quotes"])]
        llm_findings.append(recap_finding)

    findings = rule_findings + llm_findings
    if not llm_ok:
        # We could not do the story-level check, so a human must look at it.
        findings.append(_finding("rule", "llm_review_unavailable", "high",
                                 "Automated transcript review did not run; manual review required."))

    score, level = score_findings(findings)
    decision = "hold_for_review" if level == "high" else "file"

    fired = {f["code"] for f in findings}
    checks = [{"code": code, "passed": code not in fired, "label": label} for code, label in RULE_CATALOG.items()]

    return {
        "checks": checks,          # every rule, passed or not
        "dropped": dropped,        # LLM findings judged compatible; did not change the score
        "comparison": compare_policy_to_statements(policy, fields, incident_date),
        "score": score,
        "level": level,
        "decision": decision,
        "incident_date": incident_date.isoformat() if incident_date else None,
        "findings": findings,
        "llm_summary": summary,
        "llm_review_ok": llm_ok,
        "thresholds": {"medium": RISK_MEDIUM, "high": RISK_HIGH},
        "transcript": highlight_transcript(transcript, findings),
    }
