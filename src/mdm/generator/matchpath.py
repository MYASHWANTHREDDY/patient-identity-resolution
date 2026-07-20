"""Match-path domain records (Phase 20, PROJECT_CONSTITUTION.md) -- pharmacy_info and
lab_results, the two domains Phase 17 found with no linkable key at all
(docs/domain-linking-strategy.md). Unlike Phase 19's join-path fact domains, these carry
noisy demographic fields (reusing the same noise functions the member domain itself uses)
and get record_keys added to ground truth -- resolving them to patient_global_id requires
real matching (the existing comparator/blocking/Fellegi-Sunter pipeline), not a join.

`VENDOR_B_PHARMACY`: a PBM relationship for some VENDOR_B members, member-level (one row
per person), no shared ID with VENDOR_B's own enrollment records.
`VENDOR_D`: a lab, entirely separate from any payer -- no eligibility relationship to key
off of at all. Test detail rows attach to a VENDOR_D "lab identity" record the same way
Phase 19's pharmacy_claims attach to a PBM member id, except here that identity record
itself needs matching first, not a join.

Field-naming stays consistent (unlike the member domain's deliberately-varied per-vendor
schemas) for the same reason Phase 19's fact domains do: each match-path domain has exactly
one source, so there's no cross-vendor schema reconciliation to stress-test here -- the hard
problem is noise plus no shared key, not format diversity.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pyarrow as pa
from faker import Faker

from mdm.generator.identity import Identity
from mdm.generator.noise import apply_noise, choose_requested_noise_type

# Not every identity shows up in a PBM or a lab -- modeled plainly rather than assumed
# universal coverage.
PHARMACY_INFO_APPEARANCE_PROB = 0.5
LAB_APPEARANCE_PROB = 0.5

PLAN_TIERS = ("bronze", "silver", "gold", "platinum")
ABNORMAL_FLAGS = ("normal", "high", "low", "critical")

_ZERO_LAB_RESULTS_PROB = 0.20  # a lab identity with zero attached tests is a real possibility
_MAX_LAB_RESULTS = 10

DEFAULT_WINDOW_START = date(2023, 1, 1)
DEFAULT_WINDOW_END = date(2026, 1, 1)

PHARMACY_INFO_SCHEMA = pa.schema(
    [
        ("source_record_id", pa.string()),
        ("first_name", pa.string()),
        ("last_name", pa.string()),
        ("dob", pa.string()),
        ("gender", pa.string()),
        ("address", pa.string()),
        ("phone", pa.string()),
        ("plan_tier", pa.string()),
    ]
)
LAB_IDENTITY_SCHEMA = pa.schema(
    [
        ("source_record_id", pa.string()),
        ("first_name", pa.string()),
        ("last_name", pa.string()),
        ("dob", pa.string()),
        ("gender", pa.string()),
        ("address", pa.string()),
        ("phone", pa.string()),
    ]
)
LAB_RESULTS_SCHEMA = pa.schema(
    [
        ("source_record_id", pa.string()),
        ("test_date", pa.string()),
        ("test_code", pa.string()),
        ("result_value", pa.string()),
        ("result_unit", pa.string()),
        ("abnormal_flag", pa.string()),
    ]
)


def _random_date_within(rng: random.Random, start: date, end: date) -> date:
    span_days = (end - start).days
    return start + timedelta(days=rng.randint(0, max(span_days, 0)))


def generate_pharmacy_info_appearance(
    rng: random.Random,
    faker: Faker,
    identity: Identity,
    identity_index: int,
    nickname_table: dict[str, list[str]],
) -> tuple[dict, str]:
    """Returns (raw record, actual_noise_type) -- mirrors the member domain's own
    per-appearance shape (mdm.generator.shard.generate_shard), just for a source with no ID
    to lean on."""
    requested = choose_requested_noise_type(rng, allow_missing_ssn=False)
    overrides, actual_noise_type = apply_noise(
        rng,
        first_name=identity.first_name,
        last_name=identity.last_name,
        dob=identity.dob,
        requested_noise_type=requested,
        nickname_table=nickname_table,
        has_ssn_field=False,
    )
    record = {
        "source_record_id": f"PHARM{rng.randint(10_000_000, 99_999_999)}-{identity_index:08d}",
        "first_name": overrides.get("first_name", identity.first_name),
        "last_name": overrides.get("last_name", identity.last_name),
        "dob": overrides.get("dob", identity.dob).isoformat(),
        "gender": identity.gender,
        "address": faker.address().replace("\n", ", "),
        "phone": faker.phone_number(),
        "plan_tier": rng.choice(PLAN_TIERS),
    }
    return record, actual_noise_type


def generate_lab_identity_appearance(
    rng: random.Random,
    faker: Faker,
    identity: Identity,
    identity_index: int,
    nickname_table: dict[str, list[str]],
) -> tuple[dict, str]:
    requested = choose_requested_noise_type(rng, allow_missing_ssn=False)
    overrides, actual_noise_type = apply_noise(
        rng,
        first_name=identity.first_name,
        last_name=identity.last_name,
        dob=identity.dob,
        requested_noise_type=requested,
        nickname_table=nickname_table,
        has_ssn_field=False,
    )
    record = {
        "source_record_id": f"LABD{rng.randint(10_000_000, 99_999_999)}-{identity_index:08d}",
        "first_name": overrides.get("first_name", identity.first_name),
        "last_name": overrides.get("last_name", identity.last_name),
        "dob": overrides.get("dob", identity.dob).isoformat(),
        "gender": identity.gender,
        "address": faker.address().replace("\n", ", "),
        "phone": faker.phone_number(),
    }
    return record, actual_noise_type


def generate_lab_results(
    rng: random.Random,
    *,
    source_record_id: str,
    loinc_codes: list[str],
    window_start: date = DEFAULT_WINDOW_START,
    window_end: date = DEFAULT_WINDOW_END,
) -> list[dict]:
    if rng.random() < _ZERO_LAB_RESULTS_PROB:
        return []
    count = rng.randint(1, _MAX_LAB_RESULTS)
    rows = []
    for _ in range(count):
        rows.append(
            {
                "source_record_id": source_record_id,
                "test_date": _random_date_within(rng, window_start, window_end).isoformat(),
                "test_code": rng.choice(loinc_codes),
                "result_value": f"{rng.uniform(0.1, 500):.1f}",
                "result_unit": rng.choice(("mg/dL", "mmol/L", "U/L", "%", "10^3/uL")),
                "abnormal_flag": rng.choices(
                    ABNORMAL_FLAGS, weights=(0.7, 0.15, 0.1, 0.05), k=1
                )[0],
            }
        )
    return rows
