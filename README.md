# FNOL Voice Agent

An auto-insurance **First Notice of Loss** voice agent built on [Guava](https://goguava.ai).
It verifies the caller, takes the account of the accident, scores the call for fraud
signals while the conversation is running, and holds anything suspicious for a person
instead of filing it. Comes with a local dashboard that shows the call live.

## Features

- Identity verification against the policy on file, one retry, never reveals which detail failed
- Guided intake of the accident details, ending with a "tell me again from the start" recap
- Live follow-up questions when an answer is vague or contradicts an earlier one (max two per call)
- Risk score from rule checks, a transcript review, and a direct recap-vs-account comparison
- Claims scoring high are held for human review; the caller only hears that a specialist will call
- Recording disclosure, approved answers for off-script questions, and redaction of card/ID numbers
- Dashboard with live transcript, moving score, highlighted lines, and a ledger of every change

## Quick start

Requires Python 3.11+, the [Guava CLI](https://goguava.ai/docs/quickstart), and a Guava account.

```bash
git clone https://github.com/jayesh2810/fnol-voice-agent.git
cd fnol-voice-agent
guava login
uv sync                          # or: pip install guava-sdk

python main.py --chat            # text chat in the terminal
python main.py                   # talk through the laptop mic
python main.py --phone +1555...  # answer a number from your Guava account

python dashboard.py              # in a second terminal, then open http://localhost:8787
```

Test as **Maria Lopez**, policy **100234**, born **April 12, 1988** for a clean call.
See `policies.py` for the other sample customers and what each one triggers.

## How it works

Guava runs the conversation (speech, voice, checklists). This code decides what to
collect, checks the answers, and scores the call. Audio never reaches it.

```
call start ──▶ verify caller ──▶ intake (11 fields + recap) ──▶ score ──▶ file or hold ──▶ save
                   │                     │                          │
             policies.py        live check per line           fraud.py: rules +
                                (fraud.check_turn)            transcript review
```

Scoring: low 10, medium 20, high 60 points per finding. Under 30 low, 30–59 medium,
60+ held. Rules, weights and prompts live in `fraud.py`; limits for the live check at
the top of `main.py`.

## Project layout

| File | Purpose |
|---|---|
| `main.py` | agent: tasks, handlers, live score, saving |
| `fraud.py` | rules, transcript review, recap comparison, live check |
| `policies.py` | sample policy records and identity matching |
| `redact.py` | scrubs card, ID and security-code numbers before storage |
| `dashboard.py`, `dashboard.html` | local review dashboard |
| `claims/` | one JSON per call plus `live.json` (git-ignored) |

## Known limits

- No hand-off to a human yet; callers who ask are told an adjuster will follow up
- The live check can ask a needless confirmation on honest calls
- Third-party callers (reporting on the policyholder's behalf) are not handled
- Answers collected during a call that ends early are not saved
- Sample data is a Python dict; swap `find_policy` for a real lookup
