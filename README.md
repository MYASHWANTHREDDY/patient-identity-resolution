# patient-dedup-system

**Status:** Phases 0–23 complete. A shippable local build (dbt conformance/blocking,
Fellegi-Sunter scoring, clustering/crosswalk/survivorship, quality gates, Streamlit dashboard,
CI) working end to end at `ci`/`dev` tier, plus the full GCP path (BigQuery, Dataproc
Serverless, Airflow orchestration) verified against a real 5,048,389-record run — see
[docs/scale-run.md](docs/scale-run.md) for candidate-pair counts, block skew, Dataproc cost,
and what broke going from 50K to 5M records. On top of that core matching engine, the system
now spans six data domains (member eligibility, medical history, medical claims, pharmacy
claims, pharmacy info, lab results) across four vendors, tied to one `patient_global_id`, and
exposed through both a resolve-and-fetch API and a full cross-domain dashboard search — see
[docs/domain-linking-strategy.md](docs/domain-linking-strategy.md) and "The multi-domain
model" below. That layer is verified at `ci`/`dev` tier only; the 5M-record proof point above
covers the core matching engine it's built on, not (yet) the six-domain layer itself.

## The problem

Healthcare data arrives from multiple vendors, under multiple schemas, describing overlapping
sets of people — and not just one kind of data. The same person might have registration data,
medical history, and pharmacy claims from one vendor, and a completely different insurance
relationship's claims from another. Some of a vendor's own data types share one ID; others use
a separate ID for the same person; some data (a lab, say) has no shared ID with anything at
all. The goal: one stable identifier per real person, with every domain's data reachable
through it, regardless of which vendor it came from or how it happened to be linked.

## The solution

A Master Data Management pipeline: three-layer conformance architecture, probabilistic record
linkage (Fellegi-Sunter) at multi-million-record scale, and golden-record survivorship —
producing one stable `patient_global_id` per real person with a full audit trail. One codebase
runs locally against 50,000 records in under two minutes (DuckDB/pandas) and against 5,000,000
records on GCP (BigQuery + Dataproc Serverless), unchanged apart from a `--tier` flag — the
same comparator, scoring, clustering, and survivorship logic runs on both.

That core engine is then reused, unchanged, to resolve six data domains to the same
`patient_global_id` three different ways depending on what each domain actually has to link
on — a straight join, a join through a per-vendor ID map, or real matching for domains with no
shared ID at all. `member_360` (a live view, always current) and the fact tables underneath it
are queryable directly, through a FastAPI resolve-and-fetch service, or through the Streamlit
dashboard's full cross-domain member search. See "The multi-domain model" below.

- **[docs/architecture.md](docs/architecture.md)** — the three-layer data flow, the tiered
  backend model, storage layout, and the two backend choices (BigQuery vs. Spark, SOUNDEX vs.
  Double Metaphone) with the reasoning behind each.
- **[docs/matching-methodology.md](docs/matching-methodology.md)** — how the matching itself
  works: blocking strategy, comparators, Fellegi-Sunter scoring (with this project's own
  measured weights), threshold triage, clustering guards, crosswalk resolution, survivorship.
- **[docs/domain-linking-strategy.md](docs/domain-linking-strategy.md)** — the six-domain
  model: the vendor × domain matrix, why each domain links the way it does (straight join,
  ID-map join, or real matching), and the real reference code sets (ICD-10-CM, HCPCS,
  LOINC, NDC) each domain carries.
- **[docs/design-decisions.md](docs/design-decisions.md)** — every non-obvious choice, written
  at the time it was made, alternatives considered and why they were rejected.
- **[docs/scale-run.md](docs/scale-run.md)** — the real 5M-record run: costs, skew, and six
  things that broke at that scale which never showed up at 50K.

## Quick start

No GCP account required.

```bash
pip install -r requirements-dev.txt
pip install -e .
cp dbt/profiles.yml.example dbt/profiles.yml
make demo TIER=dev
```

`make demo` generates synthetic data (all six domains), runs the dbt conformance/blocking
layers, estimates Fellegi-Sunter parameters, runs core matching (scoring → triage →
clustering → crosswalk → survivorship), resolves the two no-shared-ID domains against that
population, runs the dbt serving layer + quality gates, and opens the Streamlit dashboard —
about 90 seconds end to end. Swap `TIER=ci` for a ~5,000-record run in a few seconds instead
of the `dev` tier's 50,000.

**No `make` on Windows.** Git Bash doesn't ship GNU Make. Either install it (`choco install
make`, or use WSL), or run the Makefile's `demo` target by hand — it's `data-dev`,
`dbt-build-pre`, `estimate-params`, `match`, `match-path`, `dbt-build`, `evaluate`,
`quality-checks`, `dashboard` in that order; see the `Makefile` for the exact command each one
runs.

To try the API instead of (or alongside) the dashboard: `python -m mdm.api --tier dev`, then
open `http://127.0.0.1:8000/docs`.

## Proof, not a demo

Every number below is generated by a script, not typed by hand (`python -m mdm.evaluate`
regenerates the dev-tier ones into [docs/results.md](docs/results.md); the scale-tier ones
came from real `gcloud`/`bq` commands documented in [docs/scale-run.md](docs/scale-run.md)).

| | dev tier (50,482 records) | scale tier (5,048,389 records) |
| --- | --- | --- |
| Backend | DuckDB + pandas, a laptop | BigQuery + Dataproc Serverless |
| Candidate pairs | 60,534 | 337.3M (down from 647.1M pre-fix) |
| Blocking pair completeness | 0.9561 | 0.9555 |
| Fellegi-Sunter F1 vs. naive baseline | 0.9972 vs. 0.9962 | — |
| Golden records | 24,924 | 2,559,287 |
| Auto-match precision / recall (this tier's own threshold) | 0.9920 / 0.9561 | 0.965 / 0.942 |
| Runtime | ~90 seconds | scoring ~33 min, clustering ~18 min |
| Real cost | $0 | ~$77.57 (GCP free-trial credit) |

Dev-tier auto-match recall (0.9561) lands exactly on blocking's own pair completeness — the
scoring/threshold step loses essentially none of what blocking already made available to it.

The scale-tier auto-match precision/recall is *lower* than dev tier's, on purpose reported
that way rather than hidden: the same Fellegi-Sunter score means something different at 5M
records than at 50K, and re-measuring the threshold per tier — instead of assuming it
transfers — is one of the six real findings in
[docs/scale-run.md](docs/scale-run.md#what-broke-at-5m-that-didnt-at-50k----summary).

## The multi-domain model

Six domains, four vendors, one `patient_global_id` — but not all six domains link to that ID
the same way. `docs/domain-linking-strategy.md` classifies every (vendor, domain) pair into
one of three cases:

| Domain | Vendor(s) | How it resolves to a person |
| --- | --- | --- |
| Member eligibility | A, B, C | the core matching population itself |
| Medical history | A, C | **Path A** — same ID as that vendor's own eligibility record, a straight join |
| Medical claims | A, B | **Path A** — straight join |
| Pharmacy claims | A, C | **Path B** — a different ID (separate PBM relationship), one extra hop through a per-vendor ID map |
| Pharmacy info | B | **match-path** — no shared ID at all, resolved by real matching against the golden population |
| Lab results | D (a lab, no payer relationship) | **match-path** — same as above; `VENDOR_D` has no eligibility relationship to key off of |

The two match-path domains reuse the exact same comparator/blocking/Fellegi-Sunter pipeline as
core matching — run a second time as an asymmetric lookup, never a join. Measured at `dev`
tier: 24,181 match-path records, 22,649 auto-matched (93.66% recall, 99.99% precision) against
a ground-truth table kept fully separate from the core one, so these numbers can never
silently mix into the core metrics above. Full numbers in
[docs/results.md](docs/results.md#match-path-resolution-dev-tier-phase-20) after running
`python -m mdm.evaluate --tier dev`.

**Reference codes are real, not invented:** ICD-10-CM (diagnoses, 54,778 codes), HCPCS Level
II (procedures, 8,725 — not CPT, which is AMA-copyrighted), LOINC (lab tests, 12,133), and NDC
(drugs, 24,441), all fetched live from CMS/NLM/Regenstrief/FDA public APIs.

**Consuming it:**

- `member_360` — a live view (recomputed per query, never materialized), one row per person,
  summarizing every domain: claim/encounter counts, most recent dates, active prescription
  count, lab abnormal count.
- **Member 360 API** (`src/mdm/api.py`, `python -m mdm.api --tier dev`) — `POST /resolve`
  matches (or creates) a `patient_global_id` for a new record; `GET /members/{id}` returns
  the full cross-domain profile; `GET /members/{id}/{domain}` paginates into one domain. A
  caller never needs to know a vendor name, a domain name beyond the six above, or a table
  structure.
- **Dashboard** (`dashboard/app.py`, "Golden records" tab) — search by name or jump straight
  to a `patient_global_id`; selecting a person shows eligibility (each vendor's raw data,
  side by side), field lineage (which value won and why), and all five fact domains, each
  gracefully empty rather than erroring for a person with no data in that domain.

## Repository map

```text
config/             Tunable pipeline behavior -- tiers, blocking passes, thresholds, survivorship rules
config/reference/   Real ICD-10-CM, HCPCS, LOINC, NDC code tables (fetched, never invented)
src/mdm/            Pure comparator/scoring/clustering/survivorship logic, shared by both backends
src/mdm/api.py      Member 360 API (FastAPI) -- resolve a record, fetch a full cross-domain profile
src/mdm/generator/  Synthetic data generation, all six domains, deterministic given a seed
src/mdm/backends/   The DuckDB (local) and Spark (Dataproc) execution boundaries
dbt/                Conformance, blocking, and serving models -- one project, two targets
spark_jobs/         Submittable Dataproc Serverless entry points (scoring, clustering)
scripts/            CLI entry points: generate data, run matching, evaluate, quality checks
airflow/            Three DAGs: ingestion, conformance, dedup -- Docker, never Cloud Composer
terraform/          GCP bucket, datasets, service account, IAM
dashboard/          Streamlit app, reads only from DuckDB/BigQuery, never recomputes matching
tests/              Unit tests (pure functions) + integration tests (full pipeline, ci tier)
docs/               architecture.md, matching-methodology.md, domain-linking-strategy.md,
                    design-decisions.md, scale-run.md, results.md
```

## License

[MIT](LICENSE)
