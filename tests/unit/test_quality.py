import pandas as pd

from mdm.quality import (
    PASS,
    WARN,
    check_block_skew,
    check_cluster_size_distribution,
    check_dedup_rate,
    check_dob_plausibility,
    check_review_queue_volume,
)


def test_check_dedup_rate_within_range_passes():
    result = check_dedup_rate(1000, 600, min_rate=0.0, max_rate=0.9)
    assert result.status == PASS
    assert result.value == 0.4


def test_check_dedup_rate_too_high_warns():
    result = check_dedup_rate(1000, 50, min_rate=0.0, max_rate=0.9)
    assert result.status == WARN


def test_check_dedup_rate_zero_source_records_is_safe():
    result = check_dedup_rate(0, 0)
    assert result.value == 0.0
    assert result.status == PASS


def test_check_review_queue_volume_within_bounds():
    result = check_review_queue_volume(10, 100, max_rate=0.5)
    assert result.status == PASS
    assert result.value == 0.1


def test_check_review_queue_volume_too_high_warns():
    result = check_review_queue_volume(60, 100, max_rate=0.5)
    assert result.status == WARN


def test_check_block_skew_passes_when_evenly_distributed():
    block_stats = pd.DataFrame(
        {"blocking_pass": ["bp_coarse"] * 4, "record_count": [10, 10, 10, 10]}
    )
    result = check_block_skew(block_stats, max_share=0.5)
    assert result.status == PASS
    assert result.value == 0.25


def test_check_block_skew_warns_on_a_dominant_block():
    block_stats = pd.DataFrame({"blocking_pass": ["bp_coarse"] * 2, "record_count": [900, 100]})
    result = check_block_skew(block_stats, max_share=0.5)
    assert result.status == WARN
    assert result.value == 0.9


def test_check_block_skew_empty_is_safe():
    result = check_block_skew(pd.DataFrame(columns=["blocking_pass", "record_count"]))
    assert result.status == PASS


def test_check_dob_plausibility_flags_out_of_range_years():
    patient_normalized = pd.DataFrame({"dob_year": [1980, 1850, 2999]})
    result = check_dob_plausibility(patient_normalized, min_year=1900, max_year=2026)
    assert result.status == WARN
    assert result.value == 2


def test_check_dob_plausibility_all_valid_passes():
    patient_normalized = pd.DataFrame({"dob_year": [1980, 1990, 2000]})
    result = check_dob_plausibility(patient_normalized, min_year=1900, max_year=2026)
    assert result.status == PASS
    assert result.value == 0


def test_check_cluster_size_distribution_passes_under_threshold():
    membership = pd.DataFrame({"source_record_count": [1, 2, 3, 4]})
    result = check_cluster_size_distribution(membership, warn_above=10)
    assert result.status == PASS
    assert result.value == 4


def test_check_cluster_size_distribution_warns_over_threshold():
    membership = pd.DataFrame({"source_record_count": [1, 2, 15]})
    result = check_cluster_size_distribution(membership, warn_above=10)
    assert result.status == WARN
    assert result.value == 15


def test_check_cluster_size_distribution_empty_is_safe():
    result = check_cluster_size_distribution(pd.DataFrame(columns=["source_record_count"]))
    assert result.status == PASS
