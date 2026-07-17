from datetime import date

import pandas as pd

from mdm.deterministic import NAME_DOB_RULE, SSN_RULE, deterministic_match_pairs


def _row(record_key, first_name, last_name, dob, ssn):
    return {
        "record_key": record_key,
        "first_name": first_name,
        "last_name": last_name,
        "dob": dob,
        "ssn": ssn,
    }


def test_matches_on_exact_ssn():
    df = pd.DataFrame(
        [
            _row("A:1", "ROBERT", "SMITH", date(1980, 1, 1), "123456789"),
            _row("B:1", "BOB", "SMYTH", date(1980, 1, 1), "123456789"),  # name/dob differ
        ]
    )
    pairs = deterministic_match_pairs(df)
    assert list(zip(pairs["record_key_a"], pairs["record_key_b"], strict=False)) == [("A:1", "B:1")]
    assert pairs.iloc[0]["rule"] == SSN_RULE


def test_matches_on_exact_name_and_dob_without_ssn():
    df = pd.DataFrame(
        [
            _row("A:1", "ROBERT", "SMITH", date(1980, 1, 1), None),
            _row("B:1", "ROBERT", "SMITH", date(1980, 1, 1), None),
        ]
    )
    pairs = deterministic_match_pairs(df)
    assert list(zip(pairs["record_key_a"], pairs["record_key_b"], strict=False)) == [("A:1", "B:1")]
    assert pairs.iloc[0]["rule"] == NAME_DOB_RULE


def test_ssn_rule_wins_when_both_rules_apply():
    df = pd.DataFrame(
        [
            _row("A:1", "ROBERT", "SMITH", date(1980, 1, 1), "123456789"),
            _row("B:1", "ROBERT", "SMITH", date(1980, 1, 1), "123456789"),
        ]
    )
    pairs = deterministic_match_pairs(df)
    assert len(pairs) == 1
    assert pairs.iloc[0]["rule"] == SSN_RULE


def test_no_match_when_nothing_agrees_exactly():
    df = pd.DataFrame(
        [
            _row("A:1", "ROBERT", "SMITH", date(1980, 1, 1), "123456789"),
            _row("B:1", "BOB", "SMYTH", date(1980, 1, 2), "987654321"),
        ]
    )
    pairs = deterministic_match_pairs(df)
    assert pairs.empty


def test_null_ssn_never_matches_null_ssn():
    df = pd.DataFrame(
        [
            _row("A:1", "ROBERT", "SMITH", date(1980, 1, 1), None),
            _row("B:1", "BOB", "SMYTH", date(1980, 1, 2), None),
        ]
    )
    pairs = deterministic_match_pairs(df)
    assert pairs.empty


def test_transitive_group_produces_all_pairs():
    df = pd.DataFrame(
        [
            _row("A:1", "ROBERT", "SMITH", date(1980, 1, 1), "123456789"),
            _row("B:1", "ROBERT", "SMITH", date(1980, 1, 1), "123456789"),
            _row("C:1", "ROBERT", "SMITH", date(1980, 1, 1), "123456789"),
        ]
    )
    pairs = deterministic_match_pairs(df)
    got = set(zip(pairs["record_key_a"], pairs["record_key_b"], strict=False))
    assert got == {("A:1", "B:1"), ("A:1", "C:1"), ("B:1", "C:1")}
