"""Real medical code reference tables (Phase 18, PROJECT_CONSTITUTION.md) -- ICD-10-CM, NDC,
HCPCS Level II, and LOINC, sourced from live public APIs by scripts/fetch_reference_codes.py
and stored as config/reference/*.csv. Loaded here the same way config/nicknames.yml is
loaded elsewhere in this project: read once into a plain dict, handed to whatever needs it --
the fact-table generator (Phase 19/20), to sample a real code; validation, to confirm a
generated code is real rather than invented (P3).

No CPT: it's AMA-copyrighted and this project holds no license (see
docs/domain-linking-strategy.md). HCPCS Level II fills the same procedure-coding role.
"""

from __future__ import annotations

import csv

from mdm.config import REPO_ROOT

REFERENCE_DIR = REPO_ROOT / "config" / "reference"


def load_code_set(filename: str) -> dict[str, str]:
    """code -> description, for any of the four reference CSVs. All share a `code` column;
    the three NLM-sourced files (icd10cm, hcpcs, loinc) have a `description` column, ndc.csv
    has `generic_name` in that role instead (its own real field, not renamed to fit)."""
    path = REFERENCE_DIR / filename
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        desc_field = "description" if "description" in (reader.fieldnames or []) else "generic_name"
        return {row["code"]: row[desc_field] for row in reader}


def load_icd10cm() -> dict[str, str]:
    return load_code_set("icd10cm.csv")


def load_hcpcs() -> dict[str, str]:
    return load_code_set("hcpcs.csv")


def load_loinc() -> dict[str, str]:
    return load_code_set("loinc.csv")


def load_ndc() -> dict[str, str]:
    return load_code_set("ndc.csv")
