"""Tier 2 statistical quality checks (PROJECT_CONSTITUTION.md #14) -- warn and surface on
the dashboard, never halt the pipeline. Tier 1 structural checks are dbt tests (conservation,
uniqueness, referential integrity) and halt on failure; that boundary is deliberate --
Tier 1 catches bugs, Tier 2 catches drift worth a human's attention.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

PASS = "pass"
WARN = "warn"

DEFAULT_MIN_DEDUP_RATE = 0.0
DEFAULT_MAX_DEDUP_RATE = 0.9
DEFAULT_MAX_REVIEW_QUEUE_RATE = 0.5
DEFAULT_MAX_BLOCK_SHARE = 0.05
DEFAULT_MIN_DOB_YEAR = 1900
DEFAULT_MAX_CLUSTER_SIZE_WARN = 10


@dataclass(frozen=True)
class ValidationResult:
    check_name: str
    status: str
    value: float | int | None
    message: str


def check_dedup_rate(
    total_source_records: int,
    total_golden_records: int,
    *,
    min_rate: float = DEFAULT_MIN_DEDUP_RATE,
    max_rate: float = DEFAULT_MAX_DEDUP_RATE,
) -> ValidationResult:
    rate = 1 - (total_golden_records / total_source_records) if total_source_records else 0.0
    status = PASS if min_rate <= rate <= max_rate else WARN
    return ValidationResult(
        "dedup_rate", status, rate, f"dedup_rate={rate:.4f} expected [{min_rate}, {max_rate}]"
    )


def check_review_queue_volume(
    review_count: int, total_scored_pairs: int, *, max_rate: float = DEFAULT_MAX_REVIEW_QUEUE_RATE
) -> ValidationResult:
    rate = review_count / total_scored_pairs if total_scored_pairs else 0.0
    status = PASS if rate <= max_rate else WARN
    return ValidationResult(
        "review_queue_rate", status, rate, f"review_queue_rate={rate:.4f} expected <= {max_rate}"
    )


def check_block_skew(
    block_stats: pd.DataFrame, *, max_share: float = DEFAULT_MAX_BLOCK_SHARE
) -> ValidationResult:
    """Largest block's share of all blocked records, per pass -- the skew analysis
    (PROJECT_CONSTITUTION.md #12) surfaced as a single number worth watching over time."""
    if block_stats.empty:
        return ValidationResult("block_skew", PASS, 0.0, "no blocks to evaluate")
    total_records = block_stats["record_count"].sum()
    max_block = block_stats["record_count"].max()
    share = max_block / total_records if total_records else 0.0
    status = PASS if share <= max_share else WARN
    return ValidationResult(
        "block_skew", status, share, f"largest_block_share={share:.4f} expected <= {max_share}"
    )


def check_dob_plausibility(
    patient_normalized: pd.DataFrame,
    *,
    min_year: int = DEFAULT_MIN_DOB_YEAR,
    max_year: int | None = None,
) -> ValidationResult:
    import datetime

    max_year = max_year or datetime.date.today().year
    implausible = patient_normalized[
        (patient_normalized["dob_year"] < min_year) | (patient_normalized["dob_year"] > max_year)
    ]
    count = len(implausible)
    status = PASS if count == 0 else WARN
    return ValidationResult(
        "dob_plausibility",
        status,
        count,
        f"{count} records with dob_year outside [{min_year}, {max_year}]",
    )


def check_cluster_size_distribution(
    membership: pd.DataFrame, *, warn_above: int = DEFAULT_MAX_CLUSTER_SIZE_WARN
) -> ValidationResult:
    if membership.empty:
        return ValidationResult("cluster_size_distribution", PASS, 0, "no clusters to evaluate")
    max_size = int(membership["source_record_count"].max())
    status = PASS if max_size <= warn_above else WARN
    return ValidationResult(
        "cluster_size_distribution",
        status,
        max_size,
        f"largest cluster has {max_size} members, expected <= {warn_above}",
    )


def run_all_checks(
    *,
    total_source_records: int,
    total_golden_records: int,
    review_count: int,
    total_scored_pairs: int,
    block_stats: pd.DataFrame,
    patient_normalized: pd.DataFrame,
    membership: pd.DataFrame,
) -> list[ValidationResult]:
    return [
        check_dedup_rate(total_source_records, total_golden_records),
        check_review_queue_volume(review_count, total_scored_pairs),
        check_block_skew(block_stats),
        check_dob_plausibility(patient_normalized),
        check_cluster_size_distribution(membership),
    ]
