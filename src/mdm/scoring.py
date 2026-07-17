"""Fellegi-Sunter scoring (PROJECT_CONSTITUTION.md #11.4): weight = log2(m/u) per field per
agreement level, summed across fields. Weights are estimated from labeled data
(scripts/estimate_fs_params.py) via mdm.fs_estimation, never hand-tuned.

The naive scorer is kept alongside as `score_naive` -- a deliberately hand-tuned baseline
("40% name, 30% DOB...") that exists only so Phase 6's evaluation can quantify what
hand-tuning gives up relative to weights derived from data.
"""

from __future__ import annotations

from mdm.comparators import (
    DEFAULT_NEAR_THRESHOLD,
    DEFAULT_SIMILAR_THRESHOLD,
    compare_dob,
    compare_gender,
    compare_name,
    compare_ssn,
)

FIELDS = ("first_name", "last_name", "dob", "ssn", "gender")

# Deliberately hand-tuned, not derived -- "where did those numbers come from? I made them
# up" is the honest answer, and that's the point of keeping this around (see #11.4).
NAIVE_FIELD_WEIGHTS = {
    "first_name": 0.20,
    "last_name": 0.20,
    "dob": 0.30,
    "ssn": 0.20,
    "gender": 0.10,
}
NAIVE_AGREEMENT_SCORE = {
    "exact": 1.0,
    "nickname": 0.8,
    "near": 0.7,
    "similar": 0.5,
    "transposed": 0.8,
    "one_component_off": 0.6,
    "year_only": 0.3,
    "different": 0.0,
    "missing": 0.0,
}


def compare_record_pair(
    record_a: dict,
    record_b: dict,
    *,
    nickname_index: dict[str, str] | None = None,
    near_threshold: float = DEFAULT_NEAR_THRESHOLD,
    similar_threshold: float = DEFAULT_SIMILAR_THRESHOLD,
) -> dict[str, str]:
    """record_a/record_b need keys: first_name, last_name, dob, ssn, gender."""
    return {
        "first_name": compare_name(
            record_a["first_name"],
            record_b["first_name"],
            nickname_index=nickname_index,
            near_threshold=near_threshold,
            similar_threshold=similar_threshold,
        ),
        "last_name": compare_name(
            record_a["last_name"],
            record_b["last_name"],
            nickname_index=nickname_index,
            near_threshold=near_threshold,
            similar_threshold=similar_threshold,
        ),
        "dob": compare_dob(record_a["dob"], record_b["dob"]),
        "ssn": compare_ssn(record_a["ssn"], record_b["ssn"]),
        "gender": compare_gender(record_a["gender"], record_b["gender"]),
    }


def score_fs(agreement: dict[str, str], fs_params: dict) -> float:
    """Sum of per-field log2(m/u) weights. A field/level absent from fs_params
    contributes 0 -- the same "uncomparable contributes nothing" rule, applied
    defensively if a level was never observed during estimation."""
    total = 0.0
    for field, level in agreement.items():
        total += fs_params.get(field, {}).get(level, {}).get("weight", 0.0)
    return total


def score_naive(agreement: dict[str, str]) -> float:
    total = 0.0
    for field, level in agreement.items():
        total += NAIVE_FIELD_WEIGHTS[field] * NAIVE_AGREEMENT_SCORE.get(level, 0.0)
    return total
