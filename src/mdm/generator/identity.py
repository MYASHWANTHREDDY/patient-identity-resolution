"""The canonical "true" identity behind every corrupted record — the thing ground truth
points back to."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

from faker import Faker

NICKNAME_TABLE_DRAW_RATE = 0.30  # fraction of identities whose first name is drawn
# directly from the nickname table's canonical names, so nickname noise has enough real
# collisions to be exercised at every tier — plain Faker names hit the table only by chance.


@dataclass(frozen=True)
class Identity:
    identity_id: str
    first_name: str
    last_name: str
    dob: date
    gender: str
    ssn: str  # 9 raw digits, unformatted — vendor-specific formatting happens at render time


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
    dob = faker.date_of_birth(minimum_age=1, maximum_age=95)
    ssn = f"{rng.randint(0, 999_999_999):09d}"

    return Identity(identity_id, first_name, last_name, dob, gender, ssn)
