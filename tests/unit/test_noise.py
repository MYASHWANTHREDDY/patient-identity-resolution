import random
from datetime import date

import pytest

from mdm.generator.noise import (
    apply_nickname,
    apply_noise,
    apply_typo,
    choose_requested_noise_type,
    transpose_dob,
)

NICKNAME_TABLE = {"Robert": ["Bob", "Bobby", "Rob"]}


@pytest.mark.parametrize("seed", range(20))
def test_apply_typo_changes_a_two_char_or_longer_name(seed):
    rng = random.Random(seed)
    corrupted = apply_typo(rng, "SMITH")
    assert corrupted != "SMITH"
    assert abs(len(corrupted) - len("SMITH")) <= 1


def test_apply_typo_leaves_short_names_untouched():
    rng = random.Random(0)
    assert apply_typo(rng, "A") == "A"
    assert apply_typo(rng, "") == ""


def test_apply_nickname_known_name_returns_table_entry():
    rng = random.Random(1)
    result, applied = apply_nickname(rng, "Robert", NICKNAME_TABLE)
    assert applied is True
    assert result in NICKNAME_TABLE["Robert"]


def test_apply_nickname_unknown_name_is_unchanged():
    rng = random.Random(1)
    result, applied = apply_nickname(rng, "Zelmira", NICKNAME_TABLE)
    assert applied is False
    assert result == "Zelmira"


def test_transpose_dob_swaps_day_and_month_when_valid():
    rng = random.Random(0)
    corrupted, transposed = transpose_dob(rng, date(1985, 3, 7))
    assert transposed is True
    assert corrupted == date(1985, 7, 3)


def test_transpose_dob_falls_back_when_day_exceeds_12():
    rng = random.Random(0)
    original = date(1985, 3, 25)
    corrupted, transposed = transpose_dob(rng, original)
    assert transposed is False
    assert corrupted != original
    assert abs((corrupted - original).days) <= 3


def test_transpose_dob_falls_back_when_day_equals_month():
    # day == month (e.g. the 3rd of March) would be a no-op transposition
    rng = random.Random(0)
    original = date(1985, 3, 3)
    corrupted, transposed = transpose_dob(rng, original)
    assert transposed is False
    assert corrupted != original


def test_choose_requested_noise_type_respects_allow_missing_ssn():
    rng = random.Random(0)
    draws = {choose_requested_noise_type(rng, allow_missing_ssn=False) for _ in range(500)}
    assert "missing_ssn" not in draws


def test_choose_requested_noise_type_can_draw_missing_ssn_when_allowed():
    rng = random.Random(0)
    draws = {choose_requested_noise_type(rng, allow_missing_ssn=True) for _ in range(500)}
    assert "missing_ssn" in draws


def test_apply_noise_exact_returns_no_overrides():
    rng = random.Random(0)
    overrides, actual = apply_noise(
        rng,
        first_name="Robert",
        last_name="Smith",
        dob=date(1980, 1, 1),
        requested_noise_type="exact",
        nickname_table=NICKNAME_TABLE,
        has_ssn_field=True,
    )
    assert overrides == {}
    assert actual == "exact"


def test_apply_noise_nickname_hit():
    rng = random.Random(1)
    overrides, actual = apply_noise(
        rng,
        first_name="Robert",
        last_name="Smith",
        dob=date(1980, 1, 1),
        requested_noise_type="nickname",
        nickname_table=NICKNAME_TABLE,
        has_ssn_field=True,
    )
    assert actual == "nickname"
    assert overrides["first_name"] in NICKNAME_TABLE["Robert"]


def test_apply_noise_nickname_miss_falls_back_to_typo_name():
    rng = random.Random(1)
    overrides, actual = apply_noise(
        rng,
        first_name="Zelmira",
        last_name="Quintanilla",
        dob=date(1980, 1, 1),
        requested_noise_type="nickname",
        nickname_table=NICKNAME_TABLE,
        has_ssn_field=True,
    )
    assert actual == "typo_name"
    assert overrides  # some field was corrupted


def test_apply_noise_missing_ssn_blanks_the_field():
    rng = random.Random(0)
    overrides, actual = apply_noise(
        rng,
        first_name="Robert",
        last_name="Smith",
        dob=date(1980, 1, 1),
        requested_noise_type="missing_ssn",
        nickname_table=NICKNAME_TABLE,
        has_ssn_field=True,
    )
    assert actual == "missing_ssn"
    assert overrides == {"ssn": ""}


def test_apply_noise_missing_ssn_falls_back_when_vendor_has_no_ssn_field():
    rng = random.Random(0)
    _overrides, actual = apply_noise(
        rng,
        first_name="Robert",
        last_name="Smith",
        dob=date(1980, 1, 1),
        requested_noise_type="missing_ssn",
        nickname_table=NICKNAME_TABLE,
        has_ssn_field=False,
    )
    assert actual != "missing_ssn"


def test_apply_noise_unknown_type_raises():
    rng = random.Random(0)
    with pytest.raises(ValueError):
        apply_noise(
            rng,
            first_name="Robert",
            last_name="Smith",
            dob=date(1980, 1, 1),
            requested_noise_type="not_a_real_type",
            nickname_table=NICKNAME_TABLE,
            has_ssn_field=True,
        )
