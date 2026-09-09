# Northwind FNOL Agent

A voice agent that takes **First Notice of Loss (FNOL)** calls for an auto
insurer. It verifies the caller, collects the account of the accident, and
scores the call for fraud signals while the conversation is still going. Claims
that look fine are filed on the spot. Claims that do not are held for a person.
The caller never hears the words fraud, risk, review or suspicion.

Built on the [Guava](https://goguava.ai) voice platform. Guava handles the phone
line, speech recognition, the conversation itself, and speech synthesis. This
repository is the business logic that sits beside it, plus a local dashboard for
watching calls live and reviewing them afterwards.

## Contents

- [What a call looks like](#what-a-call-looks-like)
- [How it works](#how-it-works)
- [Fraud signals](#fraud-signals)
- [Safety and privacy](#safety-and-privacy)
- [Running it](#running-it)
- [The dashboard](#the-dashboard)
- [Test customers](#test-customers)
- [Call records](#call-records)
- [Files](#files)
- [Known limits and next steps](#known-limits-and-next-steps)

## What a call looks like

1. A recording disclosure is spoken first, word for word.
2. The agent asks for policy number, full name and date of birth, and checks
   them against the policy on file. One retry on a mismatch, then a polite
   goodbye. It never says which detail was wrong.
3. Once verified, it collects the accident details one question at a time:
   date, time, place, what happened, vehicle, other parties, police, injuries,
   damage, witnesses. Then it asks the caller to tell the sequence of events
   once more from the start.
4. If an answer is too vague to act on, or clashes with something said earlier,
   the agent asks one neutral follow-up before moving on. At most two per call.
5. At the end the call is scored. Low or medium risk: the claim is filed and a
   claim number is read out. High risk: the caller is told a specialist will
   call back within one business day, and nothing is filed.

## How it works

```
Guava (cloud)                          This code (main.py)
─────────────                          ───────────────────
hears the caller, speaks the replies,  hands Guava each task and checklist
runs each task's checklist             checks answers against the policy on file
                          ──events──▶  scores the call, decides file or hold
                          ◀─commands─  writes every line to claims/ for the dashboard
```

Guava calls into this code at a handful of moments: call started, a line was
spoken, a checklist is complete, the caller asked a question, the call ended.
Each moment has a handler in `main.py`. The handlers use three small modules:

- `policies.py`: the customer records and the identity check.
- `redact.py`: strips card, ID and security-code numbers from anything stored.
- `fraud.py`: the rules, the transcript review, the live check, and the score.

Audio never reaches this code. It works only with text, and it opens the
connection to Guava itself, so it runs from a laptop behind any firewall.

## Fraud signals

Three layers feed one score.

**Rules on hard facts** (`fraud.py`, `run_rules`). Deterministic, one sentence each.

| Code | Severity | Fires when |
|---|---|---|
| `policy_not_active` | high | policy is lapsed or cancelled |
| `incident_before_policy_start` | high | incident date is before the policy began |
| `incident_in_future` | medium | stated date is after today |
| `recent_policy` | medium | incident within 30 days of policy start |
| `claim_frequency` | medium | more than 1 claim in the last 12 months |
| `vehicle_mismatch` | medium | described vehicle matches neither make, model nor plate on file |
| `plate_mismatch` | medium | a plate was stated and differs from the plate on file, noting how many characters differ |
| `injuries_without_police_report` | low | injuries reported but no police involvement |
| `liability_only_coverage` | low | own-vehicle damage is not covered; adjuster to confirm |
| `llm_review_unavailable` | high | the transcript review did not run, so a person must |

**Transcript review** (`fraud.py`, `run_llm_review`, `compare_recap`). One call
to Guava's hosted model with a JSON schema, so no other API key is needed. It
reads the whole transcript and reports contradictions, changed stories and
evasive answers, quoting the caller's exact words for each. A finding the
model itself marks as compatible (a colour or a street name added on the
recap) is dropped, unless it concerns the core event: who hit whom, the
direction of impact, moving or stopped, the order of events. The recap is
also compared with the first account directly, with one question: can both
be true?

**Live check** (`fraud.py`, `check_turn`, wired in `main.py`). After each
caller line during the account, a background check asks whether the line is
too vague to act on or cannot be true alongside an earlier one. If so, the
agent is nudged to ask one neutral, factual follow-up, for example "Just so I
have it right, was that on Tuesday or on Wednesday?". Limits sit at the top
of `main.py`: two follow-ups per call, a cooldown between them, never during
identity checks, one flag to switch it off. A clear answer to a follow-up
takes its points back off.

**Scoring.** Low 10, medium 20, high 60 points per finding, capped at 100.
Under 30 is low, 30 to 59 medium, 60 and above high. One high finding holds a
claim on its own; three mediums do the same. The decision is made only from
the end-of-call score. The live score shown during the call is an estimate
for the reviewer's screen and never changes what the caller hears.

## Safety and privacy

- **Nothing is disclosed before verification**, and a failed check never says
  which detail was wrong.
- **Questions outside the checklist** go to a handler with a short approved
  list of answers (recording, timelines, who will call, next steps). Anything
  else gets a fixed "the adjuster who contacts you can answer that". The agent
  is instructed never to answer such questions from its own knowledge.
- **Volunteered payment or ID numbers** are declined by the agent and scrubbed
  from every stored line and answer by `redact.py` before anything is written
  or sent to the review model.
- **Held claims are not written to any claims system.** A person decides
  first, so nothing has to be undone.
- **The caller is never accused.** Follow-ups ask for a fact and nothing else.
  Automated denial or delay of a claim is heavily regulated; that call is a
  person's, never the agent's.

## Running it

Requirements: Python 3.11 or newer, the [Guava CLI](https://goguava.ai/docs/quickstart),
and a Guava account.

```bash
git clone https://github.com/jayesh2810/fnol-voice-agent.git
cd fnol-voice-agent
guava login                       # opens a browser once
uv sync                           # or: python -m venv .venv && .venv/bin/pip install guava-sdk
```

Then, with the environment activated:

```bash
python main.py --chat             # type instead of talk; fastest for testing
python main.py                    # talk through the laptop microphone
python main.py --webrtc           # prints a browser link
python main.py --phone +1555...   # answer calls on a number from your Guava account
```

`guava run` from this folder is equivalent to `python main.py`. The agent
answers only while the process is running; for an always-on number, deploy
with `guava deploy up` or run it on a server.

## The dashboard

```bash
python dashboard.py               # then open http://localhost:8787
```

Run it in a second terminal. It needs only the Python standard library.

- **During a call**: the transcript appears line by line, with the current
  phase in the header. The score bar fills as facts arrive: solid for rule
  points, striped for provisional points from the live check. Each change is
  tagged under the line that caused it, and a running ledger lists them all.
- **After a call**: the final score, level and decision; every flagged line
  highlighted with the points it added; each finding with its quoted evidence;
  findings judged compatible and counted for nothing; every rule that passed;
  and a side-by-side of the policy on file against what the caller said.
- **Past calls** can be opened from the dropdown in the header.

The agent writes `claims/live.json` after every spoken line, atomically, and
the page polls it once a second. No websockets, no database.

## Test customers

`policies.py` holds five sample customers. Each exists to trigger one signal.

| Policy | Name | Date of birth | Vehicle | Designed to show |
|---|---|---|---|---|
| 100234 | Maria Lopez | 1988-04-12 | 2021 Toyota Camry, 7ABC123 | clean baseline, files |
| 100571 | James Carter | 1975-11-30 | 2019 Ford F-150, 8XYZ456 | policy only days old |
| 100892 | Priya Nair | 1992-07-08 | 2022 Honda Civic, 5KLM789 | two prior claims this year |
| 101347 | Daniel Kim | 1983-02-19 | 2018 Subaru Outback, 3QRS246 | lapsed policy, holds |
| 101605 | Aisha Rahman | 1996-09-25 | 2020 Hyundai Elantra, 6TUV135 | liability-only cover |

A quick tour: call as Maria with a consistent story and expect a claim number.
Call again and tell a different story on the recap and expect a hold. Call as
Daniel and expect a hold with no reason given.

## Call records

One JSON file per call in `claims/`, git-ignored. The main sections:

```jsonc
{
  "status": "filed | held_for_review | unverified | abandoned",
  "claim_number": "CLM-XXXXXX or null",
  "verification": { "attempts": 0, "failure_reason": null, "last_heard": null },
  "policy": { "...": "the record on file, minus date of birth" },
  "fields": { "...": "every collected answer, redacted" },
  "probes": [ { "line": 14, "kind": "vague", "question": "...", "resolved": true } ],
  "live": { "score": 40, "ledger": [ { "delta": 20, "code": "recent_policy", "provisional": false } ] },
  "risk": {
    "score": 40, "level": "medium", "decision": "file",
    "findings": [ { "source": "rule | llm", "code": "...", "weight": 20, "explanation": "...", "quotes": ["..."] } ],
    "dropped": [], "checks": [], "comparison": [], "llm_review_ok": true
  },
  "transcript": [ { "i": 1, "role": "caller", "text": "...", "flags": [0] } ]
}
```

`flags` on a transcript line point at the findings that quote it.

## Files

| File | What it is |
|---|---|
| `main.py` | the agent: tasks, checklists, handlers, live score, saving |
| `fraud.py` | rules, transcript review, recap comparison, live check, scoring |
| `policies.py` | sample policy records, identity matching, date parsing |
| `redact.py` | scrubs card, ID and security-code numbers before anything is stored |
| `dashboard.py` | local web server for the review page |
| `dashboard.html` | the review page |
| `claims/` | one JSON per call, plus `live.json` for the call in progress |

## Known limits and next steps

- **No hand-off to a person yet.** A caller who asks for a human is told the
  adjuster will follow up. A transfer action or a callback request is the next
  thing to add.
- **The live check is too eager on honest callers.** It sometimes asks a
  needless confirmation because it compares the caller against the policy file
  rather than against the caller's own earlier lines. It stays polite and the
  points come off, but it should compare caller with caller only.
- **Third-party callers are not handled.** A friend or relative reporting on
  the policyholder's behalf either fails verification or, with the holder's
  details, passes as the holder. A "who am I speaking with" step and a
  third-party report path are planned.
- **Answers are saved only when the account completes.** A call that ends
  early keeps its transcript but not the collected answers.
- **Relative dates.** "It happened just now" leads the agent to ask for the
  date rather than knowing it. Giving the agent today's date at call start
  would fix this.
- **Sample data lives in a dictionary.** Replace `find_policy` with a real
  lookup for production.
- **Guava's hosted model** is convenient here. For a regulated deployment,
  confirm where transcript data is processed and retained.
