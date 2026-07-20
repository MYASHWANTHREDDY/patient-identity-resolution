#!/usr/bin/env python
"""Fetch real, public-domain (or free-to-use) medical code reference tables (Phase 18,
PROJECT_CONSTITUTION.md) -- ICD-10-CM, HCPCS Level II, and LOINC via NLM's Clinical Table
Search Service (https://clinicaltables.nlm.nih.gov, no auth), and NDC via openFDA's public
API (https://open.fda.gov, no auth for this volume). No CPT: it's AMA-copyrighted and this
project holds no license (see docs/domain-linking-strategy.md).

Real API behavior, found by testing rather than assumed, drives three different fetch
strategies here -- there is no one pagination approach that works for all four sources:

- ICD-10-CM/HCPCS: `terms=` is a genuine prefix match on the code field, and any prefix
  bucket under the API's 500-row page cap comes back complete in one request. Empty-term
  "browse everything via offset" looks like it works for the first ~500 rows, then silently
  degrades (7 rows per request) and eventually 400s -- a real API quirk found by testing, not
  a documented limitation, which is exactly why this fetches by systematic 2-character
  prefix (letter + digit) instead, recursively splitting any bucket that hits the 500 cap.
- LOINC: codes are arbitrary sequential IDs with no category-meaningful prefix (unlike
  ICD-10-CM/HCPCS), so prefix enumeration doesn't apply -- instead this searches a broad,
  fixed list of clinical topic terms (chemistry, hematology, endocrine, ...) covering common
  lab categories. Not the full ~109k LOINC codes; a large, systematically-obtained subset.
- NDC (openFDA): plain skip/limit pagination, but openFDA hard-caps `skip` at 25000
  (undocumented in the response until you hit it -- confirmed by testing: skip=25000 works,
  skip=26000 returns a 400). With limit=1000 that's ~26,000 of the real ~137k NDC products --
  the achievable ceiling for this API without a labeler-prefix partitioning scheme.

Every row in every output file is real: fetched from the live source, never invented, never
hand-picked. Coverage is honestly short of 100% for LOINC and NDC for the API-behavior
reasons above -- reported as real counts when the fetch finishes, not asserted as complete.

    python scripts/fetch_reference_codes.py
    python scripts/fetch_reference_codes.py --only icd10cm --max-prefixes 5   # smoke test
"""

from __future__ import annotations

import argparse
import csv
import json
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "config" / "reference"

NLM_BASE = "https://clinicaltables.nlm.nih.gov/api"
REQUEST_TIMEOUT = 15
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2
BETWEEN_REQUEST_SECONDS = 0.05
PAGE_CAP = 500  # server-enforced regardless of requested maxList

LOINC_TOPICS = [
    "glucose", "hemoglobin", "hematocrit", "sodium", "potassium", "chloride",
    "bicarbonate", "creatinine", "urea nitrogen", "calcium", "magnesium", "phosphate",
    "albumin", "total protein", "bilirubin", "alkaline phosphatase", "AST", "ALT",
    "cholesterol", "triglyceride", "HDL", "LDL", "TSH", "T4", "T3", "cortisol",
    "hemoglobin A1c", "white blood cell", "red blood cell", "platelet count",
    "neutrophil", "lymphocyte", "monocyte", "eosinophil", "basophil",
    "prothrombin time", "INR", "partial thromboplastin", "D-dimer", "fibrinogen",
    "troponin", "creatine kinase", "BNP", "natriuretic peptide", "CRP",
    "erythrocyte sedimentation rate", "ferritin", "iron", "vitamin B12", "folate",
    "vitamin D", "PSA", "CA 125", "CA 19-9", "CEA", "AFP", "urinalysis", "urine protein",
    "urine glucose", "urine culture", "blood culture", "hepatitis", "HIV", "influenza",
    "streptococcus", "COVID", "respiratory pathogen", "rapid plasma reagin", "ANA",
    "rheumatoid factor", "immunoglobulin", "complement", "blood type", "Rh factor",
    "arterial blood gas", "pH blood", "oxygen saturation", "lactate", "ammonia",
    "amylase", "lipase", "uric acid", "digoxin", "lithium", "phenytoin", "valproic acid",
    "vancomycin", "gentamicin", "acetaminophen", "salicylate", "ethanol", "cortisol",
    "estradiol", "testosterone", "progesterone", "prolactin", "parathyroid hormone",
    "insulin", "C-peptide", "homocysteine", "lipoprotein", "apolipoprotein",
    "microalbumin", "cystatin C", "beta-2 microglobulin", "haptoglobin",
    "reticulocyte", "sedimentation rate", "occult blood", "fecal", "stool culture",
]


def _get_json(url: str) -> dict | list:
    last_error: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(f"failed after {RETRY_ATTEMPTS} attempts: {url}") from last_error


def _nlm_query(
    endpoint: str, code_field: str, name_field: str, terms: str
) -> list[tuple[str, str]]:
    """Caller checks len(result) >= PAGE_CAP to detect a truncated (not genuinely
    complete) bucket -- the response's own `total` isn't reliable for that check, since
    it's the count matching `terms` overall, not a signal that this page was cut short."""
    url = (
        f"{NLM_BASE}/{endpoint}/v3/search?sf={code_field},{name_field}"
        f"&terms={urllib.parse.quote(terms)}&maxList={PAGE_CAP}"
    )
    _total, codes, _extra, display_rows = _get_json(url)
    return [
        (code, (display[-1] if display else ""))
        for code, display in zip(codes, display_rows, strict=True)
    ]


def fetch_nlm_by_prefix(
    endpoint: str, code_field: str, name_field: str, *, max_prefixes: int | None = None
) -> list[tuple[str, str]]:
    """Systematic 2-character (letter + digit) prefix enumeration over the code field,
    recursively splitting any prefix whose bucket hits the page cap (meaning it's been
    truncated, not that it's genuinely exactly 500 rows)."""
    seen: dict[str, str] = {}
    prefixes = [f"{letter}{digit}" for letter in string.ascii_uppercase for digit in string.digits]
    if max_prefixes is not None:
        prefixes = prefixes[:max_prefixes]

    def _fetch_prefix(prefix: str, depth: int) -> None:
        rows = _nlm_query(endpoint, code_field, name_field, prefix)
        if len(rows) >= PAGE_CAP and depth < 3:
            # bucket truncated at the page cap -- split into prefix+digit and recurse
            for digit in string.digits:
                _fetch_prefix(f"{prefix}{digit}", depth + 1)
                time.sleep(BETWEEN_REQUEST_SECONDS)
        else:
            for code, name in rows:
                seen[code] = name

    for i, prefix in enumerate(prefixes):
        _fetch_prefix(prefix, depth=0)
        if (i + 1) % 20 == 0 or i == len(prefixes) - 1:
            print(f"  {endpoint}: {i + 1}/{len(prefixes)} prefixes, {len(seen)} codes so far")
        time.sleep(BETWEEN_REQUEST_SECONDS)
    return sorted(seen.items())


def fetch_loinc(*, max_terms: int | None = None) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    terms_list = LOINC_TOPICS[:max_terms] if max_terms is not None else LOINC_TOPICS
    for i, term in enumerate(terms_list):
        rows = _nlm_query("loinc_items", "LOINC_NUM", "LONG_COMMON_NAME", term)
        for code, name in rows:
            seen[code] = name
        if (i + 1) % 10 == 0 or i == len(terms_list) - 1:
            print(f"  loinc: {i + 1}/{len(terms_list)} topics, {len(seen)} codes so far")
        time.sleep(BETWEEN_REQUEST_SECONDS)
    return sorted(seen.items())


def fetch_ndc(*, max_pages: int | None = None) -> list[tuple[str, str, str, str]]:
    """openFDA drug/ndc, skip/limit pagination up to the API's confirmed skip<=25000 limit
    (~26,000 records reachable this way out of ~137k total -- see module docstring)."""
    # sort=product_ndc:asc: without an explicit sort, openFDA's underlying search index
    # doesn't guarantee stable ordering across separate skip/limit requests against a live,
    # continuously-updated database -- confirmed by testing: the unsorted version of this
    # fetch produced 225 duplicate product_ndc values across pages. Sorting plus a `seen`
    # dict (matching the other three fetchers) makes this robust either way.
    seen: dict[str, tuple[str, str, str, str]] = {}
    skip = 0
    limit = 1000
    page = 0
    while True:
        url = f"https://api.fda.gov/drug/ndc.json?limit={limit}&skip={skip}&sort=product_ndc:asc"
        payload = _get_json(url)
        results = payload.get("results", [])
        total = payload["meta"]["results"]["total"]
        if not results:
            break
        for r in results:
            code = r.get("product_ndc", "")
            seen[code] = (
                code,
                r.get("generic_name", "") or "",
                r.get("brand_name", "") or "",
                r.get("dosage_form", "") or "",
            )
        skip += len(results)
        page += 1
        print(f"  ndc: {skip}/{total}, {len(seen)} distinct codes so far")
        if skip >= total or skip >= 25000:
            break
        if max_pages is not None and page >= max_pages:
            print(f"  ndc: stopping early at --max-pages={max_pages}")
            break
        time.sleep(BETWEEN_REQUEST_SECONDS)
    return sorted(seen.values())


def _write_csv(path: Path, header: list[str], rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows -> {path}")


SOURCES = ("icd10cm", "hcpcs", "loinc", "ndc")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=SOURCES, default=None)
    parser.add_argument("--max-prefixes", type=int, default=None, help="icd10cm/hcpcs smoke test")
    parser.add_argument("--max-terms", type=int, default=None, help="loinc smoke test")
    parser.add_argument("--max-pages", type=int, default=None, help="ndc smoke test")
    args = parser.parse_args(argv)

    sources = [args.only] if args.only else list(SOURCES)

    if "icd10cm" in sources:
        print("fetching icd10cm ...")
        rows = fetch_nlm_by_prefix("icd10cm", "code", "name", max_prefixes=args.max_prefixes)
        _write_csv(OUT_DIR / "icd10cm.csv", ["code", "description"], rows)

    if "hcpcs" in sources:
        print("fetching hcpcs ...")
        rows = fetch_nlm_by_prefix("hcpcs", "code", "short_desc", max_prefixes=args.max_prefixes)
        _write_csv(OUT_DIR / "hcpcs.csv", ["code", "description"], rows)

    if "loinc" in sources:
        print("fetching loinc ...")
        rows = fetch_loinc(max_terms=args.max_terms)
        _write_csv(OUT_DIR / "loinc.csv", ["code", "description"], rows)

    if "ndc" in sources:
        print("fetching ndc ...")
        ndc_rows = fetch_ndc(max_pages=args.max_pages)
        _write_csv(
            OUT_DIR / "ndc.csv",
            ["code", "generic_name", "brand_name", "dosage_form"],
            ndc_rows,
        )


if __name__ == "__main__":
    main()
