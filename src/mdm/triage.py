"""Two-threshold triage (PROJECT_CONSTITUTION.md #11.5). Two thresholds, not one, because
false merges and false splits don't cost the same (P13): a single cutoff silently asserts
they do.
"""

from __future__ import annotations

AUTO_MATCH = "auto_match"
REVIEW = "review"
NON_MATCH = "non_match"


def decide(score: float, *, upper: float, lower: float) -> str:
    if score >= upper:
        return AUTO_MATCH
    if score >= lower:
        return REVIEW
    return NON_MATCH
