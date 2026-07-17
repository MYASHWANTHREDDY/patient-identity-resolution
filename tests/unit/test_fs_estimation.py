import math
from datetime import date

from mdm.fs_estimation import estimate_fs_params, sample_non_match_pairs
from mdm.scoring import FIELDS


def _record(first, last, dob, ssn, gender):
    return {"first_name": first, "last_name": last, "dob": dob, "ssn": ssn, "gender": gender}


RECORDS = {
    "A:1": _record("ROBERT", "SMITH", date(1980, 1, 1), "111111111", "M"),
    "B:1": _record("ROBERT", "SMITH", date(1980, 1, 1), "111111111", "M"),
    "A:2": _record("MARY", "JONES", date(1975, 5, 5), "222222222", "F"),
    "B:2": _record("MARY", "JONES", date(1975, 5, 5), "222222222", "F"),
    "A:3": _record("JAMES", "BROWN", date(1990, 3, 3), "333333333", "M"),
    "B:3": _record("JOHN", "GREEN", date(1965, 8, 8), "444444444", "M"),
    "C:3": _record("PATRICIA", "WHITE", date(1955, 2, 2), "555555555", "F"),
    "D:3": _record("SUSAN", "BLACK", date(1945, 7, 7), "666666666", "F"),
}
TRUE_PAIRS = {("A:1", "B:1"), ("A:2", "B:2")}


def test_sample_non_match_pairs_excludes_true_pairs():
    sampled = sample_non_match_pairs(list(RECORDS), TRUE_PAIRS, sample_size=10, seed=42)
    assert all(pair not in TRUE_PAIRS for pair in sampled)


def test_sample_non_match_pairs_deterministic_given_seed():
    first = sample_non_match_pairs(list(RECORDS), TRUE_PAIRS, sample_size=10, seed=42)
    second = sample_non_match_pairs(list(RECORDS), TRUE_PAIRS, sample_size=10, seed=42)
    assert first == second


def test_estimate_fs_params_covers_all_fields_and_levels():
    params = estimate_fs_params(RECORDS, TRUE_PAIRS, sample_size=20, seed=42)
    assert set(params) == set(FIELDS)
    for field in FIELDS:
        for level_stats in params[field].values():
            assert "m" in level_stats and "u" in level_stats and "weight" in level_stats
            assert math.isfinite(level_stats["weight"])


def test_estimate_fs_params_exact_agreement_gets_positive_weight():
    # both true-match pairs agree exactly on every field -- m(exact) should dominate
    # u(exact) since non-match pairs in this fixture never agree exactly on anything.
    params = estimate_fs_params(RECORDS, TRUE_PAIRS, sample_size=20, seed=42)
    assert params["first_name"]["exact"]["weight"] > 0
    assert params["dob"]["exact"]["weight"] > 0
    assert params["ssn"]["exact"]["weight"] > 0


def test_estimate_fs_params_never_divides_by_zero_for_unobserved_levels():
    # "nickname" agreement never occurs in this tiny fixture -- must still produce a
    # finite (Laplace-smoothed) weight rather than raising or returning -inf/nan.
    params = estimate_fs_params(RECORDS, TRUE_PAIRS, sample_size=20, seed=42)
    assert math.isfinite(params["first_name"]["nickname"]["weight"])
