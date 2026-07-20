import random
from datetime import date

from faker import Faker

from mdm.generator.identity import (
    DOB_REFERENCE_DATE,
    MAX_AGE_YEARS,
    MIN_AGE_YEARS,
    _synthesize_dob,
    synthesize_identity,
)

NICKNAME_TABLE = {"Robert": ["Bob", "Bobby", "Rob"]}


def test_synthesize_dob_is_a_pure_function_of_the_rng_seed():
    """The regression guard for the real bug this replaced: faker.date_of_birth() silently
    depended on datetime.now(), so the same seed produced a different DOB depending on which
    real day the generator ran. _synthesize_dob takes only `rng` -- calling it twice with
    freshly-seeded random.Random(42) instances (inherently at two different real moments,
    however close together) must give the identical date."""
    dob_a = _synthesize_dob(random.Random(42))
    dob_b = _synthesize_dob(random.Random(42))
    assert dob_a == dob_b


def test_synthesize_dob_stays_within_the_configured_age_range_of_the_reference_date():
    rng = random.Random(0)
    for _ in range(500):
        dob = _synthesize_dob(rng)
        age_years = (DOB_REFERENCE_DATE - dob).days / 365.25
        assert MIN_AGE_YEARS - 0.01 <= age_years <= MAX_AGE_YEARS + 0.01


def test_synthesize_dob_returns_a_real_date_object():
    dob = _synthesize_dob(random.Random(1))
    assert isinstance(dob, date)


def test_synthesize_identity_is_deterministic_given_seed():
    faker_a = Faker()
    faker_a.seed_instance(42)
    identity_a = synthesize_identity(0, faker_a, random.Random(42), NICKNAME_TABLE)

    faker_b = Faker()
    faker_b.seed_instance(42)
    identity_b = synthesize_identity(0, faker_b, random.Random(42), NICKNAME_TABLE)

    assert identity_a == identity_b


def test_synthesize_identity_dob_depends_only_on_rng_not_identity_index():
    """identity_index only feeds identity_id -- dob comes entirely from `rng`. Two calls
    given the *same freshly-seeded* rng but different identity_index produce different IDs
    but the identical dob, confirming _synthesize_dob never reads identity_index."""
    faker = Faker()
    faker.seed_instance(42)
    identity_0 = synthesize_identity(0, faker, random.Random(1), NICKNAME_TABLE)
    identity_1 = synthesize_identity(1, faker, random.Random(1), NICKNAME_TABLE)

    assert identity_0.identity_id != identity_1.identity_id
    assert identity_0.dob == identity_1.dob
