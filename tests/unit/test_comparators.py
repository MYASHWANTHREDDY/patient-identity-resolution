from datetime import date

import pytest

from mdm.comparators import (
    build_nickname_index,
    compare_dob,
    compare_gender,
    compare_name,
    compare_ssn,
)

NICKNAME_TABLE = {"ROBERT": ["BOB", "BOBBY", "ROB"], "WILLIAM": ["BILL", "WILL"]}
NICKNAME_INDEX = build_nickname_index(NICKNAME_TABLE)


def test_build_nickname_index_maps_canonical_to_itself():
    assert NICKNAME_INDEX["ROBERT"] == "ROBERT"


def test_build_nickname_index_maps_variants_to_canonical():
    assert NICKNAME_INDEX["BOB"] == "ROBERT"
    assert NICKNAME_INDEX["BOBBY"] == "ROBERT"


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("SMITH", "SMITH", "exact"),
        (None, "SMITH", "missing"),
        ("SMITH", None, "missing"),
        ("", "SMITH", "missing"),
        ("ROBERT", "BOB", "nickname"),
        ("BOB", "BOBBY", "nickname"),
        ("SMITH", "SMITX", "near"),  # JW ~= 0.92
        ("SMITH", "SMYTH", "similar"),  # JW ~= 0.89
        ("SMITH", "ZZZZZZ", "different"),
    ],
)
def test_compare_name_levels(a, b, expected):
    assert compare_name(a, b, nickname_index=NICKNAME_INDEX) == expected


def test_compare_name_without_nickname_index_never_returns_nickname():
    assert compare_name("ROBERT", "BOB") != "nickname"


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (date(1980, 3, 7), date(1980, 3, 7), "exact"),
        (None, date(1980, 3, 7), "missing"),
        (date(1980, 3, 7), None, "missing"),
        (date(1980, 3, 7), date(1980, 7, 3), "transposed"),
        (date(1980, 3, 7), date(1980, 3, 8), "one_component_off"),
        (date(1980, 3, 7), date(1980, 5, 7), "one_component_off"),
        (date(1980, 3, 7), date(1981, 3, 7), "one_component_off"),
        (date(1980, 3, 7), date(1980, 5, 9), "year_only"),
        (date(1980, 3, 7), date(1990, 5, 9), "different"),
    ],
)
def test_compare_dob_levels(a, b, expected):
    assert compare_dob(a, b) == expected


def test_compare_dob_no_transposition_when_day_equals_month():
    # day == month (e.g. the 3rd of March) can't produce a distinguishable transposition
    assert compare_dob(date(1980, 3, 3), date(1980, 3, 3)) == "exact"


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("123456789", "123456789", "exact"),
        ("123456789", "987654321", "different"),
        (None, "123456789", "missing"),
        ("", "123456789", "missing"),
        (None, None, "missing"),
    ],
)
def test_compare_ssn_levels(a, b, expected):
    assert compare_ssn(a, b) == expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("M", "M", "exact"),
        ("M", "F", "different"),
        (None, "M", "missing"),
        ("U", "M", "missing"),
        ("U", "U", "missing"),
    ],
)
def test_compare_gender_levels(a, b, expected):
    assert compare_gender(a, b) == expected
