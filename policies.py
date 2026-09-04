"""Fake policy records that stand in for the insurer's policy database.

In a real deployment this module would call the insurer's policy API.
Everything the agent needs to know about a customer lives here, plus the
helpers that check whether a caller's answers match a policy on file.
"""

from __future__ import annotations

import difflib
import re
from datetime import date, timedelta

# --- Fake policies -----------------------------------------------------------
# Each record is chosen to exercise one fraud signal. See README.md.

_today = date.today()

POLICIES: dict[str, dict] = {
    # Clean, long-standing customer. The "honest caller" baseline.
    "100234": {
        "policy_number": "100234",
        "holder_name": "Maria Lopez",
        "date_of_birth": "1988-04-12",
        "vehicle": {"year": 2021, "make": "Toyota", "model": "Camry", "plate": "7ABC123"},
        "policy_start": "2023-06-01",
        "status": "active",
        "coverage": "comprehensive",
        "prior_claims_12m": 0,
    },
    # Policy started only nine days ago. Triggers the "recent policy" rule.
    "100571": {
        "policy_number": "100571",
        "holder_name": "James Carter",
        "date_of_birth": "1975-11-30",
        "vehicle": {"year": 2019, "make": "Ford", "model": "F-150", "plate": "8XYZ456"},
        "policy_start": (_today - timedelta(days=9)).isoformat(),
        "status": "active",
        "coverage": "comprehensive",
        "prior_claims_12m": 0,
    },
    # Two claims already this year. Triggers the "claim frequency" rule.
    "100892": {
        "policy_number": "100892",
        "holder_name": "Priya Nair",
        "date_of_birth": "1992-07-08",
        "vehicle": {"year": 2022, "make": "Honda", "model": "Civic", "plate": "5KLM789"},
        "policy_start": "2022-01-15",
        "status": "active",
        "coverage": "comprehensive",
        "prior_claims_12m": 2,
    },
    # Policy lapsed for non-payment. Triggers the "policy not active" rule.
    "101347": {
        "policy_number": "101347",
        "holder_name": "Daniel Kim",
        "date_of_birth": "1983-02-19",
        "vehicle": {"year": 2018, "make": "Subaru", "model": "Outback", "plate": "3QRS246"},
        "policy_start": "2021-09-01",
        "status": "lapsed",
        "lapsed_on": "2026-05-31",
        "coverage": "comprehensive",
        "prior_claims_12m": 0,
    },
    # Liability-only cover. Own-vehicle damage is not covered; adjuster must check.
    "101605": {
        "policy_number": "101605",
        "holder_name": "Aisha Rahman",
        "date_of_birth": "1996-09-25",
        "vehicle": {"year": 2020, "make": "Hyundai", "model": "Elantra", "plate": "6TUV135"},
        "policy_start": "2024-03-10",
        "status": "active",
        "coverage": "liability_only",
        "prior_claims_12m": 0,
    },
}


# --- Lookup ------------------------------------------------------------------

def normalize_policy_number(text: str | None) -> str:
    """Keep only the digits. Callers say things like 'one zero zero, two three four'."""
    return re.sub(r"\D", "", str(text or ""))


def find_policy(policy_number: str | None) -> dict | None:
    return POLICIES.get(normalize_policy_number(policy_number))


# --- Matching helpers --------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_number(word: str) -> int | None:
    return _MONTHS.get(word.strip(".").lower()[:3])


def parse_date(text: str | None) -> date | None:
    """Turn a spoken or typed date into a real date, or None if we can't.

    Handles the SDK's date dict, plus text: 1990-03-05, 03/05/1990,
    'March 5 1990', 'March 5th, 1990', '5 March 1990', '5th of March 1990'.
    """
    if not text:
        return None
    if isinstance(text, date):
        return text
    if isinstance(text, dict):  # Guava "date" fields arrive as {"year": 1988, "month": 4, "day": 12}
        try:
            return date(int(text["year"]), int(text["month"]), int(text["day"]))
        except (KeyError, TypeError, ValueError):
            return None
    t = str(text).strip().lower()

    candidates: list[tuple[int, int, int]] = []  # (year, month, day)

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        candidates.append((int(m[1]), int(m[2]), int(m[3])))

    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", t)
    if m:
        year = int(m[3])
        if year < 100:
            year += 1900 if year > 30 else 2000
        candidates.append((year, int(m[1]), int(m[2])))  # US order month/day/year

    m = re.search(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t)
    if m and _month_number(m[1]):
        candidates.append((int(m[3]), _month_number(m[1]), int(m[2])))

    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+),?\s+(\d{4})", t)
    if m and _month_number(m[2]):
        candidates.append((int(m[3]), _month_number(m[2]), int(m[1])))

    for year, month, day in candidates:
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def _name_tokens(name: str | None) -> list[str]:
    return [tok for tok in re.sub(r"[^a-z ]", " ", str(name or "").lower()).split() if tok]


def names_match(spoken: str | None, on_file: str, threshold: float = 0.75) -> bool:
    """Every part of the name on file must appear in what the caller said.

    Uses a fuzzy comparison so 'Lopes' still matches 'Lopez' (speech-to-text
    is not perfect), while 'Lopez' vs 'Nguyen' does not.
    """
    spoken_tokens = _name_tokens(spoken)
    if not spoken_tokens:
        return False
    for part in _name_tokens(on_file):
        best = max(
            (difflib.SequenceMatcher(None, part, tok).ratio() for tok in spoken_tokens),
            default=0.0,
        )
        if best < threshold:
            return False
    return True


def verify_identity(
    policy_number: str | None, full_name: str | None, date_of_birth: str | None
) -> tuple[dict | None, str]:
    """Check the three answers against the policy on file.

    Returns (policy, "") on success or (None, reason) on failure. The reason
    is for the log and the dashboard only. The agent never reads it to the
    caller, because telling a stranger *which* detail was wrong helps them
    guess the right one next time.
    """
    policy = find_policy(policy_number)
    if policy is None:
        return None, "no policy with that number"
    if not names_match(full_name, policy["holder_name"]):
        return None, "name does not match policy holder"
    spoken_dob = parse_date(date_of_birth)
    if spoken_dob is None:
        return None, "date of birth could not be understood"
    if spoken_dob != date.fromisoformat(policy["date_of_birth"]):
        return None, "date of birth does not match"
    return policy, ""
