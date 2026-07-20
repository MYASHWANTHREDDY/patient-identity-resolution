"""The canonical "true" identity behind every corrupted record — the thing ground truth
points back to."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta

from faker import Faker

NICKNAME_TABLE_DRAW_RATE = 0.30  # fraction of identities whose first name is drawn
# directly from the nickname table's canonical names, so nickname noise has enough real
# collisions to be exercised at every tier — plain Faker names hit the table only by chance.

# Fixed reference date, not date.today() -- the same discipline
# mdm.generator.facts/matchpath's DEFAULT_WINDOW_END already uses (P6: identical seed must
# produce identical output regardless of which real-world day this runs on). Faker's own
# date_of_birth() computes minimum_age/maximum_age relative to datetime.now() internally,
# which silently violates that -- found when two regenerations, a couple of real days
# apart, same seed, produced a different DOB for the same identity (see
# docs/design-decisions.md). DOB_REFERENCE_DATE matches DEFAULT_WINDOW_END so "the person
# is between MIN_AGE_YEARS and MAX_AGE_YEARS old" means the same thing everywhere in this
# project's synthetic data.
DOB_REFERENCE_DATE = date(2026, 1, 1)
MIN_AGE_YEARS = 1
MAX_AGE_YEARS = 95


@dataclass(frozen=True)
class Identity:
    identity_id: str
    first_name: str
    last_name: str
    dob: date
    gender: str
    ssn: str  # 9 raw digits, unformatted — vendor-specific formatting happens at render time


def _synthesize_dob(rng: random.Random) -> date:
    """A uniform-random date such that the person is MIN_AGE_YEARS-MAX_AGE_YEARS old as of
    DOB_REFERENCE_DATE -- drawn from `rng`, not faker.date_of_birth(), which is what
    silently pulled in datetime.now(). Same "pick an offset within a fixed window" pattern
    as mdm.generator.matchpath._random_date_within."""
    earliest = DOB_REFERENCE_DATE.replace(year=DOB_REFERENCE_DATE.year - MAX_AGE_YEARS)
    latest = DOB_REFERENCE_DATE.replace(year=DOB_REFERENCE_DATE.year - MIN_AGE_YEARS)
    span_days = (latest - earliest).days
    return earliest + timedelta(days=rng.randint(0, span_days))


def synthesize_identity(
    identity_index: int,
    faker: Faker,
    rng: random.Random,
    nickname_table: dict[str, list[str]],
) -> Identity:
    identity_id = f"ID{identity_index:08d}"
    gender = rng.choice(("M", "F"))

    if nickname_table and rng.random() < NICKNAME_TABLE_DRAW_RATE:
        first_name = rng.choice(list(nickname_table.keys()))
    elif gender == "M":
        first_name = faker.first_name_male()
    else:
        first_name = faker.first_name_female()

    last_name = faker.last_name()
    dob = _synthesize_dob(rng)
    ssn = f"{rng.randint(0, 999_999_999):09d}"

    return Identity(identity_id, first_name, last_name, dob, gender, ssn)
