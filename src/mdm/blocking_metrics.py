"""Blocking metrics (PROJECT_CONSTITUTION.md #12): Reduction Ratio and Pair Completeness,
per pass and unioned. Blocking recall is a hard ceiling on system recall -- these numbers
say exactly how much ceiling was traded for tractability.
"""

from __future__ import annotations

from itertools import combinations
from math import comb

import pandas as pd

PairKey = tuple[str, str]


def reduction_ratio(num_candidate_pairs: int, num_records: int) -> float:
    all_possible_pairs = comb(num_records, 2)
    if all_possible_pairs == 0:
        return 0.0
    return 1 - (num_candidate_pairs / all_possible_pairs)


def pair_completeness(candidate_pairs: set[PairKey], true_pairs: set[PairKey]) -> float:
    if not true_pairs:
        return 0.0
    return len(candidate_pairs & true_pairs) / len(true_pairs)


def blocking_metrics_by_pass(
    candidate_pairs_df: pd.DataFrame, true_pairs: set[PairKey], num_records: int
) -> dict[str, dict]:
    """`candidate_pairs_df` needs columns record_key_a, record_key_b, blocking_pass (one row
    per pair per pass that found it -- matching.candidate_pairs' shape)."""
    results: dict[str, dict] = {}
    for pass_name, group in candidate_pairs_df.groupby("blocking_pass"):
        pairs = set(zip(group["record_key_a"], group["record_key_b"], strict=False))
        results[pass_name] = {
            "candidate_pairs": len(pairs),
            "reduction_ratio": reduction_ratio(len(pairs), num_records),
            "pair_completeness": pair_completeness(pairs, true_pairs),
        }

    all_pairs: set[PairKey] = set(
        zip(candidate_pairs_df["record_key_a"], candidate_pairs_df["record_key_b"], strict=False)
    )
    results["unioned"] = {
        "candidate_pairs": len(all_pairs),
        "reduction_ratio": reduction_ratio(len(all_pairs), num_records),
        "pair_completeness": pair_completeness(all_pairs, true_pairs),
    }
    return results


def all_pairs_from_block_keys(block_keys_df: pd.DataFrame) -> set[PairKey]:
    """The uncapped candidate set -- every pair blocking would produce with no
    max_block_size exclusion, computed straight from matching.block_keys. Used to measure
    what the size cap actually costs (PROJECT_CONSTITUTION.md #12)."""
    pairs: set[PairKey] = set()
    for _group_key, group in block_keys_df.groupby(["blocking_pass", "block_key"]):
        keys = sorted(group["record_key"].tolist())
        pairs.update(combinations(keys, 2))
    return pairs
