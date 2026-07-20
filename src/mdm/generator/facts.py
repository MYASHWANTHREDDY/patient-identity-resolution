"""Synthetic fact-domain records (Phase 19, PROJECT_CONSTITUTION.md) -- medical history,
medical claims, and pharmacy claims for the same identities the member/eligibility domain
already generates. Grain is per-event, per-claim, per-fill -- many rows per identity, never
deduplicated, unlike the one-record-per-appearance member domain.

Which vendor sends which domain, and by which path, is fixed by
docs/domain-linking-strategy.md -- not re-derived here:

    VENDOR_A: medical_history (Path A), medical_claims (Path A), pharmacy_claims (Path B)
    VENDOR_B: medical_claims (Path A)
    VENDOR_C: medical_history (Path A), pharmacy_claims (Path B)

Path A domains reference the vendor's own enrollment `record_id` directly (`member_id`).
Path B (`pharmacy_claims`) instead mints a separate `pbm_member_id` per person per vendor,
unrelated to that vendor's enrollment ID, plus a `vendor_id_map` row tying the two together
-- modeling a PBM relationship with no shared ID space, exactly as Phase 17 classified it.
Codes are sampled from real reference tables (`mdm.reference_codes`), never invented (P3).

Field-naming stays consistent across vendors within a domain, unlike the member domain's
deliberately-varied per-vendor schemas -- that variance exists to stress-test conformance
normalization, already proven; this phase is about the join/linking logic (Path A vs. Path
B), not re-proving normalization diversity.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pyarrow as pa

MEDICAL_HISTORY_SCHEMA = pa.schema(
    [
        ("source_encounter_id", pa.string()),
        ("member_id", pa.string()),
        ("encounter_date", pa.string()),
        ("condition_code", pa.string()),
        ("encounter_type", pa.string()),
    ]
)
MEDICAL_CLAIMS_SCHEMA = pa.schema(
    [
        ("source_claim_id", pa.string()),
        ("member_id", pa.string()),
        ("claim_date", pa.string()),
        ("diagnosis_code", pa.string()),
        ("procedure_code", pa.string()),
        ("billed_amount", pa.float64()),
        ("paid_amount", pa.float64()),
        ("claim_status", pa.string()),
    ]
)
PHARMACY_CLAIMS_SCHEMA = pa.schema(
    [
        ("source_rx_id", pa.string()),
        ("pbm_member_id", pa.string()),
        ("fill_date", pa.string()),
        ("ndc_code", pa.string()),
        ("days_supply", pa.int64()),
        ("quantity", pa.int64()),
    ]
)
VENDOR_ID_MAP_SCHEMA = pa.schema(
    [
        ("source_vendor", pa.string()),
        ("pbm_member_id", pa.string()),
        ("enrollment_member_id", pa.string()),
    ]
)

MEDICAL_HISTORY_VENDORS = ("VENDOR_A", "VENDOR_C")
MEDICAL_CLAIMS_VENDORS = ("VENDOR_A", "VENDOR_B")
PHARMACY_CLAIMS_VENDORS = ("VENDOR_A", "VENDOR_C")  # all Path B

ENCOUNTER_TYPES = ("office_visit", "hospitalization", "diagnosis_only", "telehealth", "urgent_care")
CLAIM_STATUSES = ("paid", "denied", "pending")

# Fixed, not date.today() -- event/claim/fill dates must be a pure function of the seed
# (P6). Using "now" would make the same --seed produce different output depending on which
# day the generator happens to run, which is exactly the reproducibility bug P6 exists to
# rule out. Any 2-3 year window is fine; this one just has to never change.
DEFAULT_WINDOW_START = date(2023, 1, 1)
DEFAULT_WINDOW_END = date(2026, 1, 1)

# "Mostly in 10s, rarely in hundreds" (per-member volume, confirmed by the project owner) --
# a chance of zero records at all (most people don't touch every domain every year), then a
# small-integer count for those who do.
_ZERO_RECORDS_PROB = 0.35
_MAX_RECORDS_PER_DOMAIN = 14


def _record_count(rng: random.Random) -> int:
    if rng.random() < _ZERO_RECORDS_PROB:
        return 0
    return rng.randint(1, _MAX_RECORDS_PER_DOMAIN)


def _random_date_within(rng: random.Random, start: date, end: date) -> date:
    span_days = (end - start).days
    return start + timedelta(days=rng.randint(0, max(span_days, 0)))


def generate_medical_history(
    rng: random.Random,
    *,
    member_id: str,
    identity_index: int,
    icd10cm_codes: list[str],
    window_start: date = DEFAULT_WINDOW_START,
    window_end: date = DEFAULT_WINDOW_END,
) -> list[dict]:
    rows = []
    for seq in range(_record_count(rng)):
        rows.append(
            {
                "source_encounter_id": f"ENC{identity_index:08d}-{seq}",
                "member_id": member_id,
                "encounter_date": _random_date_within(rng, window_start, window_end).isoformat(),
                "condition_code": rng.choice(icd10cm_codes),
                "encounter_type": rng.choice(ENCOUNTER_TYPES),
            }
        )
    return rows


def generate_medical_claims(
    rng: random.Random,
    *,
    member_id: str,
    identity_index: int,
    icd10cm_codes: list[str],
    hcpcs_codes: list[str],
    window_start: date = DEFAULT_WINDOW_START,
    window_end: date = DEFAULT_WINDOW_END,
) -> list[dict]:
    rows = []
    for seq in range(_record_count(rng)):
        billed = round(rng.uniform(50, 5000), 2)
        status = rng.choice(CLAIM_STATUSES)
        paid = round(billed * rng.uniform(0.4, 0.95), 2) if status == "paid" else 0.0
        rows.append(
            {
                "source_claim_id": f"CLM{identity_index:08d}-{seq}",
                "member_id": member_id,
                "claim_date": _random_date_within(rng, window_start, window_end).isoformat(),
                "diagnosis_code": rng.choice(icd10cm_codes),
                "procedure_code": rng.choice(hcpcs_codes),
                "billed_amount": billed,
                "paid_amount": paid,
                "claim_status": status,
            }
        )
    return rows


def generate_pharmacy_claims(
    rng: random.Random,
    *,
    pbm_member_id: str,
    identity_index: int,
    ndc_codes: list[str],
    window_start: date = DEFAULT_WINDOW_START,
    window_end: date = DEFAULT_WINDOW_END,
) -> list[dict]:
    rows = []
    for seq in range(_record_count(rng)):
        rows.append(
            {
                "source_rx_id": f"RX{identity_index:08d}-{seq}",
                "pbm_member_id": pbm_member_id,
                "fill_date": _random_date_within(rng, window_start, window_end).isoformat(),
                "ndc_code": rng.choice(ndc_codes),
                "days_supply": rng.choice((30, 60, 90)),
                "quantity": rng.randint(1, 180),
            }
        )
    return rows


def mint_pbm_member_id(rng: random.Random, identity_index: int) -> str:
    """A PBM-issued ID with no relationship to the vendor's own enrollment record_id --
    Path B means no shared ID space, so this can't just be a transform of `member_id`."""
    return f"PBM{rng.randint(10_000_000, 99_999_999)}-{identity_index:08d}"
