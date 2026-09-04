"""Remove payment and identity numbers from text before it is stored or sent anywhere.

Guava redacts values only for fields we declare sensitive. Anything a caller
volunteers on their own would otherwise land in our transcript files in clear
text, so every line and every collected answer passes through here first.
"""

from __future__ import annotations

import re

_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")          # 13-19 digits, spaces/dashes allowed
_SSN = re.compile(r"(?<!\d)\d{3}[ \-]\d{2}[ \-]\d{4}(?!\d)")        # US social security number
_EXPIRY = re.compile(r"\b(?:exp(?:iry|ires|\.)?\s*(?:date)?\s*:?\s*)?(0[1-9]|1[0-2])\s*/\s*(\d{2}|\d{4})\b", re.I)
_CVV = re.compile(r"\b(?:cvv|cvc|security code)\s*(?:is|:)?\s*\d{3,4}\b", re.I)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def redact(text):
    """Return the text with card numbers, SSNs, expiry dates and CVVs replaced by labels."""
    if not isinstance(text, str) or not text:
        return text

    def card(m):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        tail = raw[len(raw.rstrip(" -")):]  # keep any trailing space the pattern swallowed
        return ("[card number removed]" + tail) if _luhn_ok(digits) else raw

    out = _CARD.sub(card, text)
    out = _SSN.sub("[SSN removed]", out)
    out = _CVV.sub("[security code removed]", out)
    if "[card number removed]" in out:  # only treat MM/YY as an expiry when a card was present
        out = _EXPIRY.sub("[expiry removed]", out)
    return out


def redact_fields(fields: dict) -> dict:
    return {k: redact(v) if isinstance(v, str) else v for k, v in fields.items()}
