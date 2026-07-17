"""Estimate Fellegi-Sunter m/u parameters from labeled data (PROJECT_CONSTITUTION.md #11.4).

m = P(agreement level | true match), estimated from labeled true-match pairs.
u = P(agreement level | non-match), estimated from a random sample of pairs.
weight = log2(m/u), Laplace-smoothed so no level is ever exactly 0 (which would make the
weight undefined).

Deliberately pure and I/O-free so it's unit-testable without a database -- scripts/
estimate_fs_params.py handles loading records and writing config/fs_params.yml.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from mdm.comparators import DOB_LEVELS, GENDER_LEVELS, NAME_LEVELS, SSN_LEVELS
from mdm.scoring import FIELDS, compare_record_pair

LEVELS_BY_FIELD = {
    "first_name": NAME_LEVELS,
    "last_name": NAME_LEVELS,
    "dob": DOB_LEVELS,
    "ssn": SSN_LEVELS,
    "gender": GENDER_LEVELS,
}

PairKey = tuple[str, str]


def _count_agreement_levels(
    pairs: list[PairKey], records_by_key: dict[str, dict], nickname_index: dict[str, str] | None
) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {field: defaultdict(int) for field in FIELDS}
    for key_a, key_b in pairs:
        agreement = compare_record_pair(
            records_by_key[key_a], records_by_key[key_b], nickname_index=nickname_index
        )
        for field, level in agreement.items():
            counts[field][level] += 1
    return counts


def sample_non_match_pairs(
    record_keys: list[str], true_pairs: set[PairKey], sample_size: int, seed: int
) -> list[PairKey]:
    rng = random.Random(seed)
    sampled: list[PairKey] = []
    max_attempts = sample_size * 20
    attempts = 0
    while len(sampled) < sample_size and attempts < max_attempts:
        attempts += 1
        key_a, key_b = rng.sample(record_keys, 2)
        pair = (key_a, key_b) if key_a < key_b else (key_b, key_a)
        if pair in true_pairs:
            continue
        sampled.append(pair)
    return sampled


def estimate_fs_params(
    records_by_key: dict[str, dict],
    true_pairs: set[PairKey],
    *,
    sample_size: int,
    seed: int,
    nickname_index: dict[str, str] | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    m_counts = _count_agreement_levels(list(true_pairs), records_by_key, nickname_index)

    non_match_pairs = sample_non_match_pairs(
        list(records_by_key), true_pairs, sample_size, seed
    )
    u_counts = _count_agreement_levels(non_match_pairs, records_by_key, nickname_index)

    params: dict[str, dict[str, dict[str, float]]] = {}
    for field in FIELDS:
        levels = LEVELS_BY_FIELD[field]
        num_levels = len(levels)
        m_total = sum(m_counts[field].values())
        u_total = sum(u_counts[field].values())

        field_params: dict[str, dict[str, float]] = {}
        for level in levels:
            m_smoothed = (m_counts[field][level] + 1) / (m_total + num_levels)
            u_smoothed = (u_counts[field][level] + 1) / (u_total + num_levels)
            weight = math.log2(m_smoothed / u_smoothed)
            field_params[level] = {
                "m": round(m_smoothed, 6),
                "u": round(u_smoothed, 6),
                "weight": round(weight, 4),
            }
        params[field] = field_params

    return params
