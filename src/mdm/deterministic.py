"""Stage 1 deterministic baseline (PROJECT_CONSTITUTION.md #11.1): exact agreement on a
high-specificity key. Establish this first and measure its recall -- everything after is
improvement over it.

Both rules are equi-joins, so they're computed as vectorized self-merges rather than a
pairwise O(n^2) scan -- no blocking infrastructure needed for exact matches.
"""

from __future__ import annotations

import pandas as pd

SSN_RULE = "ssn_exact"
NAME_DOB_RULE = "name_dob_exact"
_RULE_PRIORITY = {SSN_RULE: 0, NAME_DOB_RULE: 1}


def _pairs_from_exact_key(df: pd.DataFrame, key_cols: list[str], rule: str) -> pd.DataFrame:
    subset = df.dropna(subset=key_cols)
    if subset.empty:
        return pd.DataFrame(columns=["record_key_a", "record_key_b", "rule"])

    merged = subset.merge(subset, on=key_cols, suffixes=("_a", "_b"))
    merged = merged[merged["record_key_a"] < merged["record_key_b"]]
    result = merged[["record_key_a", "record_key_b"]].drop_duplicates()
    result["rule"] = rule
    return result


def deterministic_match_pairs(patient_normalized: pd.DataFrame) -> pd.DataFrame:
    """`patient_normalized` needs columns: record_key, first_name, last_name, dob, ssn.
    Returns one row per matched pair (record_key_a < record_key_b) with the rule that
    matched it -- ssn_exact wins when a pair satisfies both rules."""
    ssn_pairs = _pairs_from_exact_key(patient_normalized, ["ssn"], SSN_RULE)
    name_dob_pairs = _pairs_from_exact_key(
        patient_normalized, ["first_name", "last_name", "dob"], NAME_DOB_RULE
    )

    combined = pd.concat([ssn_pairs, name_dob_pairs], ignore_index=True)
    if combined.empty:
        return combined

    combined["_priority"] = combined["rule"].map(_RULE_PRIORITY)
    combined = combined.sort_values("_priority").drop_duplicates(
        subset=["record_key_a", "record_key_b"], keep="first"
    )
    return combined.drop(columns="_priority").reset_index(drop=True)
