"""Per-vendor Layer 1 rendering (PROJECT_CONSTITUTION.md #8). Every field is a string —
Layer 1 is an auditable record of exactly what arrived, not a parsed/cleaned schema."""

from __future__ import annotations

import pyarrow as pa

from mdm.generator.identity import Identity

VENDORS = ("VENDOR_A", "VENDOR_B", "VENDOR_C")
VENDOR_HAS_SSN = {"VENDOR_A": True, "VENDOR_B": True, "VENDOR_C": False}

VENDOR_SCHEMAS = {
    "VENDOR_A": pa.schema(
        [(f, pa.string()) for f in ("record_id", "first_name", "last_name", "dob", "gender", "ssn")]
    ),
    "VENDOR_B": pa.schema(
        [
            (f, pa.string())
            for f in ("record_id", "fname", "lname", "birth_date", "sex", "social_security_number")
        ]
    ),
    "VENDOR_C": pa.schema(
        [
            (f, pa.string())
            for f in ("record_id", "given_name", "surname", "date_of_birth", "Gender", "member_id")
        ]
    ),
}

GROUND_TRUTH_SCHEMA = pa.schema(
    [(f, pa.string()) for f in ("record_key", "true_identity_id", "noise_type")]
)


def format_ssn(digits: str) -> str:
    if not digits:
        return ""
    return f"{digits[0:3]}-{digits[3:5]}-{digits[5:9]}"


def format_dob(vendor: str, dob) -> str:
    if vendor == "VENDOR_A":
        return dob.isoformat()  # YYYY-MM-DD
    if vendor == "VENDOR_B":
        return dob.strftime("%m/%d/%Y")
    if vendor == "VENDOR_C":
        return dob.strftime("%d-%b-%Y")  # DD-Mon-YYYY
    raise ValueError(f"Unknown vendor: {vendor}")


def render_record(vendor: str, record_id: str, identity: Identity, overrides: dict) -> dict:
    first_name = overrides.get("first_name", identity.first_name)
    last_name = overrides.get("last_name", identity.last_name)
    dob = overrides.get("dob", identity.dob)
    ssn_raw = overrides.get("ssn", identity.ssn)
    dob_str = format_dob(vendor, dob)

    if vendor == "VENDOR_A":
        return {
            "record_id": record_id,
            "first_name": first_name,
            "last_name": last_name,
            "dob": dob_str,
            "gender": identity.gender,
            "ssn": format_ssn(ssn_raw),
        }
    if vendor == "VENDOR_B":
        return {
            "record_id": record_id,
            "fname": first_name,
            "lname": last_name,
            "birth_date": dob_str,
            "sex": identity.gender,
            "social_security_number": format_ssn(ssn_raw),
        }
    if vendor == "VENDOR_C":
        return {
            "record_id": record_id,
            "given_name": first_name,
            "surname": last_name,
            "date_of_birth": dob_str,
            "Gender": identity.gender,
            "member_id": f"M{record_id}",
        }
    raise ValueError(f"Unknown vendor: {vendor}")
