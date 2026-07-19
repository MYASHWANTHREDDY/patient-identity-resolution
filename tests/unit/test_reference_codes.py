import csv

import pytest

from mdm.reference_codes import REFERENCE_DIR, load_hcpcs, load_icd10cm, load_loinc, load_ndc

LOADERS = {
    "icd10cm": (load_icd10cm, "A00.0", "Cholera due to Vibrio cholerae 01, biovar cholerae"),
    "hcpcs": (load_hcpcs, "A0021", "Outside state ambulance serv"),
    "loinc": (load_loinc, "100086-8", "R cornea Analysis method"),
    "ndc": (load_ndc, "0002-0013", "Insulin human"),
}


@pytest.mark.parametrize("name", LOADERS)
def test_reference_table_loads_real_codes(name):
    """Every code loaded here came from a live public API (scripts/fetch_reference_codes.py)
    -- this is a real-data spot check, not a fixture, so it pins one known row from the
    actual fetched file rather than an invented example."""
    loader, known_code, known_description = LOADERS[name]
    codes = loader()
    assert len(codes) > 1000  # a hand-picked handful would fail this; P3
    assert codes[known_code] == known_description


@pytest.mark.parametrize("name", LOADERS)
def test_reference_table_has_no_duplicate_codes(name):
    loader, _known_code, _known_description = LOADERS[name]
    codes = loader()
    # dict keys are already deduplicated by construction; this instead re-reads the raw
    # CSV to prove the *source file* has one row per code, not just that the loader hides
    # a duplicate silently.
    path = REFERENCE_DIR / f"{name}.csv"
    with path.open(encoding="utf-8") as f:
        raw_codes = [row["code"] for row in csv.DictReader(f)]
    assert len(raw_codes) == len(set(raw_codes))
    assert len(raw_codes) == len(codes)
