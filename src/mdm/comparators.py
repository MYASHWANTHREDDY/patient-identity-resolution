"""Field comparators (PROJECT_CONSTITUTION.md #11.3) -- pure Python, backend-agnostic (P8).

Each comparator returns a discrete agreement level, never a raw similarity float. Missing
input on either side is always its own level, never folded into "different" -- an
uncomparable field must contribute exactly zero weight in Fellegi-Sunter scoring (Phase 6),
not a disagreement penalty.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from rapidfuzz.distance import JaroWinkler

NAME_LEVELS = ("exact", "nickname", "near", "similar", "different", "missing")
DOB_LEVELS = ("exact", "transposed", "one_component_off", "year_only", "different", "missing")
SSN_LEVELS = ("exact", "different", "missing")
GENDER_LEVELS = ("exact", "different", "missing")

DEFAULT_NEAR_THRESHOLD = 0.90
DEFAULT_SIMILAR_THRESHOLD = 0.80


def _is_missing(value: Any) -> bool:
    """None, '', float('nan'), and pandas NaT are all "absent" -- `not value` alone isn't
    enough because `not float('nan')` is False (NaN is truthy in Python). Pandas
    round-trips (e.g. DataFrame.to_dict) silently turn a SQL NULL into NaN/NaT instead of
    None, so a naive truthiness check mislabels a missing field as present and then,
    since NaN/NaT never equal anything (including themselves), as "different" instead of
    "missing" -- exactly backwards from PROJECT_CONSTITUTION.md #8's requirement that an
    uncomparable field contribute zero weight, not a disagreement penalty.

    `value != value` is the generic trick: it's True for NaN and NaT alike (the only
    values in Python that aren't equal to themselves), with no pandas import needed here
    to keep this module backend-agnostic (P8)."""
    if value is None:
        return True
    if value != value:  # noqa: PLR0124 -- deliberate NaN/NaT self-inequality check
        return True
    return value == ""


def build_nickname_index(nickname_table: dict[str, list[str]]) -> dict[str, str]:
    """canonical-or-variant name (uppercased) -> canonical name (uppercased)."""
    index: dict[str, str] = {}
    for canonical, variants in nickname_table.items():
        canonical_upper = canonical.upper()
        index[canonical_upper] = canonical_upper
        for variant in variants:
            index[variant.upper()] = canonical_upper
    return index


def compare_name(
    a: str | None,
    b: str | None,
    *,
    nickname_index: dict[str, str] | None = None,
    near_threshold: float = DEFAULT_NEAR_THRESHOLD,
    similar_threshold: float = DEFAULT_SIMILAR_THRESHOLD,
) -> str:
    """Robert/Bob has Jaro-Winkler ~= 0.5 -- indistinguishable from a different name. No
    threshold recovers that; it needs the nickname lookup checked before falling back to
    similarity (PROJECT_CONSTITUTION.md #11.3)."""
    if _is_missing(a) or _is_missing(b):
        return "missing"
    if a == b:
        return "exact"

    if nickname_index:
        canonical_a = nickname_index.get(a)
        canonical_b = nickname_index.get(b)
        if canonical_a is not None and canonical_a == canonical_b:
            return "nickname"

    similarity = JaroWinkler.normalized_similarity(a, b)
    if similarity >= near_threshold:
        return "near"
    if similarity >= similar_threshold:
        return "similar"
    return "different"


def compare_dob(a: date | None, b: date | None) -> str:
    """`transposed` exists because day/month transposition is a systematic error, not a
    random one -- collapsing it into "different" throws away a strong signal."""
    if _is_missing(a) or _is_missing(b):
        return "missing"
    if a == b:
        return "exact"
    if a.year == b.year and a.month == b.day and a.day == b.month:
        return "transposed"

    diffs = (a.year != b.year) + (a.month != b.month) + (a.day != b.day)
    if diffs == 1:
        return "one_component_off"
    if a.year == b.year:
        return "year_only"
    return "different"


def compare_ssn(a: str | None, b: str | None) -> str:
    if _is_missing(a) or _is_missing(b):
        return "missing"
    return "exact" if a == b else "different"


def compare_gender(a: str | None, b: str | None) -> str:
    """Gender is a weak signal -- two random people agree ~50% of the time. U (unknown) is
    treated as missing, not as a value to compare (PROJECT_CONSTITUTION.md #11.3)."""
    if _is_missing(a) or _is_missing(b) or a == "U" or b == "U":
        return "missing"
    return "exact" if a == b else "different"
