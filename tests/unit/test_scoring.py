from datetime import date

import pytest

from mdm.scoring import compare_record_pair, score_fs, score_naive

FS_PARAMS = {
    "first_name": {"exact": {"weight": 5.0}, "different": {"weight": -3.0}},
    "last_name": {"exact": {"weight": 5.0}, "different": {"weight": -3.0}},
    "dob": {"exact": {"weight": 8.0}, "different": {"weight": -4.0}},
    "ssn": {"exact": {"weight": 20.0}, "missing": {"weight": 0.1}},
    "gender": {"exact": {"weight": 1.0}, "different": {"weight": -0.5}},
}

RECORD_A = {
    "first_name": "ROBERT",
    "last_name": "SMITH",
    "dob": date(1980, 1, 1),
    "ssn": "123456789",
    "gender": "M",
}
RECORD_B = {
    "first_name": "ROBERT",
    "last_name": "SMITH",
    "dob": date(1980, 1, 1),
    "ssn": "123456789",
    "gender": "M",
}
RECORD_C = {
    "first_name": "ZELMIRA",
    "last_name": "QUINTANILLA",
    "dob": date(1990, 6, 6),
    "ssn": "987654321",
    "gender": "F",
}


def test_compare_record_pair_all_fields():
    agreement = compare_record_pair(RECORD_A, RECORD_B)
    assert agreement == {
        "first_name": "exact",
        "last_name": "exact",
        "dob": "exact",
        "ssn": "exact",
        "gender": "exact",
    }


def test_score_fs_sums_field_weights():
    agreement = compare_record_pair(RECORD_A, RECORD_B)
    score = score_fs(agreement, FS_PARAMS)
    assert score == 5.0 + 5.0 + 8.0 + 20.0 + 1.0


def test_score_fs_identical_pair_scores_higher_than_different_pair():
    identical_agreement = compare_record_pair(RECORD_A, RECORD_B)
    different_agreement = compare_record_pair(RECORD_A, RECORD_C)
    assert score_fs(identical_agreement, FS_PARAMS) > score_fs(different_agreement, FS_PARAMS)


def test_score_fs_missing_field_in_params_contributes_zero():
    agreement = {
        "first_name": "near",
        "last_name": "exact",
        "dob": "exact",
        "ssn": "exact",
        "gender": "exact",
    }
    # "near" isn't in FS_PARAMS["first_name"] -- must not raise, must contribute 0
    score = score_fs(agreement, FS_PARAMS)
    assert score == 5.0 + 8.0 + 20.0 + 1.0


def test_score_naive_identical_pair_scores_higher_than_different_pair():
    identical_agreement = compare_record_pair(RECORD_A, RECORD_B)
    different_agreement = compare_record_pair(RECORD_A, RECORD_C)
    assert score_naive(identical_agreement) > score_naive(different_agreement)


def test_score_naive_perfect_match_is_one():
    agreement = compare_record_pair(RECORD_A, RECORD_B)
    assert score_naive(agreement) == pytest.approx(1.0)
