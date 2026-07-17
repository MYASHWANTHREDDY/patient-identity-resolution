import pandas as pd

from mdm.blocking_metrics import (
    all_pairs_from_block_keys,
    blocking_metrics_by_pass,
    pair_completeness,
    reduction_ratio,
)


def test_reduction_ratio_zero_candidates_is_one():
    assert reduction_ratio(0, 100) == 1.0


def test_reduction_ratio_all_pairs_is_zero():
    all_possible = 100 * 99 // 2
    assert reduction_ratio(all_possible, 100) == 0.0


def test_reduction_ratio_single_record_is_zero_by_convention():
    assert reduction_ratio(0, 1) == 0.0


def test_pair_completeness_full_recovery():
    true_pairs = {("A", "B"), ("C", "D")}
    assert pair_completeness(true_pairs, true_pairs) == 1.0


def test_pair_completeness_partial_recovery():
    true_pairs = {("A", "B"), ("C", "D")}
    candidate_pairs = {("A", "B")}
    assert pair_completeness(candidate_pairs, true_pairs) == 0.5


def test_pair_completeness_no_true_pairs_is_zero():
    assert pair_completeness({("A", "B")}, set()) == 0.0


def test_blocking_metrics_by_pass_reports_unioned_and_per_pass():
    candidate_pairs_df = pd.DataFrame(
        [
            {"record_key_a": "A", "record_key_b": "B", "blocking_pass": "bp_ssn"},
            {"record_key_a": "A", "record_key_b": "B", "blocking_pass": "bp_coarse"},
            {"record_key_a": "C", "record_key_b": "D", "blocking_pass": "bp_coarse"},
        ]
    )
    true_pairs = {("A", "B"), ("C", "D"), ("E", "F")}
    result = blocking_metrics_by_pass(candidate_pairs_df, true_pairs, num_records=10)

    assert result["bp_ssn"]["candidate_pairs"] == 1
    assert result["bp_ssn"]["pair_completeness"] == 1 / 3

    assert result["bp_coarse"]["candidate_pairs"] == 2
    assert result["bp_coarse"]["pair_completeness"] == 2 / 3

    # unioned: {(A,B), (C,D)} found via either pass -- still just 2 distinct pairs
    assert result["unioned"]["candidate_pairs"] == 2
    assert result["unioned"]["pair_completeness"] == 2 / 3


def test_all_pairs_from_block_keys_generates_full_combinations_per_block():
    block_keys_df = pd.DataFrame(
        [
            {"record_key": "A", "blocking_pass": "bp_coarse", "block_key": "X"},
            {"record_key": "B", "blocking_pass": "bp_coarse", "block_key": "X"},
            {"record_key": "C", "blocking_pass": "bp_coarse", "block_key": "X"},
            {"record_key": "D", "blocking_pass": "bp_coarse", "block_key": "Y"},
        ]
    )
    pairs = all_pairs_from_block_keys(block_keys_df)
    assert pairs == {("A", "B"), ("A", "C"), ("B", "C")}


def test_all_pairs_from_block_keys_does_not_merge_same_block_key_across_passes():
    block_keys_df = pd.DataFrame(
        [
            {"record_key": "A", "blocking_pass": "bp_ssn", "block_key": "X"},
            {"record_key": "B", "blocking_pass": "bp_ssn", "block_key": "X"},
            {"record_key": "C", "blocking_pass": "bp_coarse", "block_key": "X"},
            {"record_key": "D", "blocking_pass": "bp_coarse", "block_key": "X"},
        ]
    )
    # block_key "X" means different things in different passes -- if grouping ignored
    # blocking_pass, all four records would be treated as one block and produce spurious
    # cross-pass pairs like (A, C).
    pairs = all_pairs_from_block_keys(block_keys_df)
    assert pairs == {("A", "B"), ("C", "D")}
