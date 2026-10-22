"""Checks on values the operator types, before they reach a command. Plain Python,
no Textual."""
import re

_ISBN_13 = re.compile(r"^\d{13}$")
_ISBN_10 = re.compile(r"^\d{9}[\dX]$")


def normalize_isbn(text: str):
    """(isbn, problem). Hyphens and spaces are dropped, so the stored value is the
    plain digit string that royalty reports carry (System 3 joins on it, ADR 011).
    A basic check: 13 digits, or 10 characters with an optional final X. No checksum."""
    cleaned = re.sub(r"[\s-]", "", text or "").upper()
    if not cleaned:
        return None, "Enter the ISBN."
    if _ISBN_13.match(cleaned) or _ISBN_10.match(cleaned):
        return cleaned, None
    return None, "An ISBN has 13 digits (or 10, the last may be X). Hyphens and spaces are fine."
