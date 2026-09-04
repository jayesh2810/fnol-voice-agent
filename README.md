# Northwind FNOL Agent

A voice agent that takes **First Notice of Loss (FNOL)** calls for an auto insurer
and quietly scores each call for fraud signals while it talks to the customer.

Built on the [Guava](https://goguava.ai) voice SDK.

## What the caller experiences

1. The agent greets them and asks for policy number, full name and date of birth.
2. If the details match a policy on file, it collects the accident details.
3. At the end it asks them to recap the sequence of events once more.
4. It either reads out a claim number, or says a specialist will call back.

The caller never hears the words fraud, risk, review or suspicion.

## What happens behind the scenes

```
call starts
   |
   v
VERIFY  -- policy number + name + DOB checked against policies.py
   |         wrong?  one retry, then a polite goodbye (status: unverified)
   v
INTAKE  -- 11 fields collected, ending with a "tell me again from the start" recap
   |
   v
ANALYZE -- fraud.py
   |         layer 1: rules on hard facts (dates, status, claim history, vehicle)
   |         layer 2: one LLM read of the transcript, quoting contradictions
   |         -> risk score 0-100 and a level: low / medium / high
   v
DECIDE  -- low or medium: claim FILED, claim number read out
   |         high:          claim HELD, not filed, specialist will call back
   v
SAVE    -- claims/<timestamp>_<call>.json with everything the dashboard needs
```

### Why the claim is held instead of filed

If a high-risk claim went straight into the claims system, an operator would
later have to find and delete it. Holding it means a human looks first and
nothing has to be undone.

### Why the caller isn't told

Someone who has just had an accident is stressed and gets details wrong
innocently. Accusing an honest customer is worse than letting a reviewer
spend five minutes on a false alarm. Automated denial or delay of a claim is
also heavily regulated, so a person makes that call, never the bot.

## Fraud signals

**Layer 1: rules** (`fraud.py`, `run_rules`). Deterministic, one sentence each.

| Code | Severity | Fires when |
|---|---|---|
| `policy_not_active` | high | policy is lapsed or cancelled |
| `incident_before_policy_start` | high | incident date is before the policy began |
| `incident_in_future` | medium | stated date is after today (probably misheard, still needs confirming) |
| `recent_policy` | medium | incident within 30 days of policy start |
| `claim_frequency` | medium | more than 1 claim in the last 12 months |
| `vehicle_mismatch` | medium | described vehicle doesn't match make, model or plate on file |
| `injuries_without_police_report` | low | injuries reported but no police involvement |
| `liability_only_coverage` | low | own-vehicle damage isn't covered; adjuster to confirm |
| `llm_review_unavailable` | high | the LLM check didn't run, so a human must |

**Layer 2: LLM transcript review** (`fraud.py`, `run_llm_review`). One call to
Guava's hosted model (`guava.helpers.llm.generate`) with a JSON schema, so no
extra API key is needed. It is told to be conservative and to quote the
caller's exact words for every finding. Categories: `contradiction`,
`changed_story`, `vague_or_evasive`, `implausible_detail`, `other`.

**Layer 2b: live follow-ups** (`fraud.py`, `check_turn`, wired in `main.py`).
While the caller gives their account, each new caller line is checked in the
background against everything said so far. If the line is too vague to act on
or cannot be true alongside an earlier statement, the agent is nudged to ask
one neutral, factual follow-up before moving on, for example "Just so I have
it right, was that on Tuesday or on Wednesday?". Limits, all at the top of
`main.py`: at most 2 follow-ups per call, never within 3 lines of the last
one, never during identity checks, and the feature can be switched off with
one flag. Every follow-up is recorded with its reason, and the end-of-call
review is told about them so a clear correction counts in the caller's favour.

**Scoring.** low = 10, medium = 20, high = 60 points per finding, capped at 100.
Score 30+ is medium, 60+ is high. So one high finding holds the claim on its own, and it takes three mediums to do the same. Thresholds live at the top of `fraud.py`.

**Highlighting.** Each finding's quotes are matched back to transcript lines.
Every transcript entry carries a `flags` list of finding indexes, so the
dashboard can colour the suspicious lines without doing any text matching.

## Test callers

`policies.py` has five fake customers. Policy numbers are six digits so they
can be spoken or keyed in.

| Policy | Name | DOB | Vehicle | Designed to show |
|---|---|---|---|---|
| 100234 | Maria Lopez | 1988-04-12 | 2021 Toyota Camry, 7ABC123 | clean baseline, should file |
| 100571 | James Carter | 1975-11-30 | 2019 Ford F-150, 8XYZ456 | policy started 9 days ago |
| 100892 | Priya Nair | 1992-07-08 | 2022 Honda Civic, 5KLM789 | 2 prior claims this year |
| 101347 | Daniel Kim | 1983-02-19 | 2018 Subaru Outback, 3QRS246 | policy lapsed, should hold |
| 101605 | Aisha Rahman | 1996-09-25 | 2020 Hyundai Elantra, 6TUV135 | liability-only cover |

Suggested scenarios:

- **Honest call.** Maria Lopez, consistent story. Expect low risk and a claim number.
- **Wrong identity.** Any policy with a wrong date of birth twice. Expect a polite goodbye and a JSON with `status: unverified`.
- **Changed story.** Maria Lopez, say the accident was Tuesday, then say Wednesday in the recap. Expect an LLM `contradiction` finding with her words quoted.
- **Stacked signals.** Daniel Kim, driving "a BMW", injuries yes, police no. Expect high risk, claim held.

## Running it

```bash
cd appointment-reminder
python main.py --chat            # type instead of talk, fastest for testing
python main.py                   # talk through your laptop mic
python main.py --webrtc          # browser link
python main.py --phone +1555...  # answer a real number
```

`guava run` from this folder does the same as `python main.py`.

## Live dashboard

```bash
python dashboard.py          # then open http://localhost:8787
```

Run it in a second terminal next to the agent. It needs nothing beyond the
Python standard library.

- **While a call is live** the transcript appears line by line, with a marker
  when identity is verified and the phase shown in the header (verifying,
  taking the account, scoring, ended).
- **When the call ends** the score, level and decision appear, every flagged
  line is highlighted (amber for low/medium, red for high) with a chip showing
  how many points it added, and the side panel lists what moved the score:
  each finding with its points and quoted evidence, LLM findings that were
  judged compatible and counted for nothing, and every rule that passed.
- **Policy vs. statements** shows the vehicle, incident date, policy status,
  claim history and coverage side by side, with mismatches in red.
- **Follow-ups asked live** appear in the transcript under the line that
  triggered them, with the reason, and in their own panel on the right.
- **Past calls** can be picked from the dropdown in the header.

How it works: the agent writes `claims/live.json` after every spoken line
(atomically, so the page never reads a half-written file), and the page polls
it once a second. Nothing else is needed, no websockets and no database.

## Output for the dashboard

One file per call in `claims/`. Shape:

```jsonc
{
  "call_id": "...",
  "started_at": "...", "ended_at": "...", "termination_reason": "...",
  "status": "filed | held_for_review | unverified | abandoned",
  "claim_number": "CLM-XXXXXX or null",
  "verification": { "attempts": 0, "failure_reason": null },
  "policy": { "policy_number": "...", "holder_name": "...", "vehicle": {...}, ... },   // no DOB
  "fields": { "incident_date": "...", "description": "...", "timeline_recap": "...", ... },
  "risk": {
    "score": 40, "level": "medium", "decision": "file",
    "findings": [
      { "source": "rule|llm", "code": "...", "severity": "...", "weight": 20,
        "explanation": "...", "quotes": ["caller's exact words"] }
    ],
    "llm_summary": "...", "llm_review_ok": true,
    "thresholds": { "medium": 30, "high": 60 }
  },
  "transcript": [
    { "i": 0, "role": "agent",  "text": "...", "time": "...", "flags": [] },
    { "i": 1, "role": "caller", "text": "...", "time": "...", "flags": [0] }   // points at findings[0]
  ]
}
```

## Files

| File | What it is |
|---|---|
| `main.py` | the agent: tasks, checklists, event handlers, saving |
| `fraud.py` | rules, LLM review, scoring, transcript highlighting |
| `policies.py` | fake policy data, identity matching, date parsing |
| `dashboard.py` | local web server for the review page |
| `dashboard.html` | the review page: live transcript, score, highlights |
| `claims/` | one JSON per call, plus `live.json` for the call in progress |

## Known limits

- Speech-to-text can mishear names and dates. Name matching is fuzzy and dates are
  parsed leniently, but a real deployment would confirm by reading back.
- The live follow-up check costs one helper-model call per caller line during the
  account. Fine for a prototype; a production version would batch or throttle it.
- Fake data lives in a Python dictionary. Swap `find_policy` for a real API call.
- Guava's hosted LLM endpoint is convenient for a prototype. For production in a
  regulated setting you would confirm where that data is processed and retained.
