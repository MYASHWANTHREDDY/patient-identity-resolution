# patient-dedup-system

**Status:** Phases 0–14 complete — shippable local build (dbt conformance/blocking,
Fellegi-Sunter scoring, clustering/crosswalk/survivorship, quality gates, Streamlit dashboard,
CI all working end to end at `ci`/`dev` tier) plus the full GCP path (BigQuery, Dataproc
Serverless, Airflow orchestration) verified at a real 5,048,389-record scale run — see
[docs/scale-run.md](docs/scale-run.md) for real candidate-pair counts, block skew, Dataproc
cost, and what broke at 5M that didn't at 50K. Phases 15–16 (documentation polish, stretch
goals) not yet started. See [PROJECT_CONSTITUTION.md](PROJECT_CONSTITUTION.md) for the full
spec, architecture, and build plan; this README will grow into the reviewer-facing summary as
Phase 15 lands.

## The problem

Three healthcare vendors each send patient records under their own schema, with no shared
identifier. The same person appears in all three feeds — spelled differently, with a
transposed birth date, under a nickname — and occasionally twice within one vendor's own feed.

## The solution

A cloud-native Master Data Management pipeline: three-layer conformance architecture,
probabilistic record linkage (Fellegi-Sunter) at multi-million-record scale, and golden-record
survivorship — producing one stable `patient_global_id` per real person with a full audit
trail. One codebase runs locally against 50k records in under two minutes (DuckDB/pandas) and
at 5,000,000 records on GCP (BigQuery + Dataproc Serverless), unchanged apart from a `--tier`
flag.

## Quick start

No GCP account required.

```bash
pip install -r requirements-dev.txt
pip install -e .
cp dbt/profiles.yml.example dbt/profiles.yml
make demo TIER=dev
```

`make demo` generates synthetic data, runs the dbt conformance/blocking layers, estimates
Fellegi-Sunter parameters, runs matching (scoring → triage → clustering → crosswalk →
survivorship), runs the dbt serving layer + quality gates, and opens the Streamlit dashboard.
Swap `TIER=ci` for a ~5,000-record run in a few seconds instead of the `dev` tier's 50,000.

See the phase-by-phase build plan in
[PROJECT_CONSTITUTION.md #19](PROJECT_CONSTITUTION.md#19-build-phases) and what each phase
actually verified in [docs/design-decisions.md](docs/design-decisions.md).

## Scale run: real numbers

At the 5,048,389-record scale tier (BigQuery + Dataproc Serverless, no local hardware):

| Metric | Value |
| --- | --- |
| Candidate pairs (post blocking-skew fix) | 337.3M (down from 647.1M pre-fix) |
| Scored pairs | 327,366,916 |
| Auto-match edges / clusters | 2,830,681 / 2,244,989 |
| Scoring runtime / cost | ~33 min / ~$16.62 |
| Clustering runtime / cost (tuned) | ~18 min / ~$9.25 |
| Total Dataproc spend, this phase | ~$77.57 (GCP free-trial credit) |

Full breakdown, block-size distribution, and every real thing that broke going from 50K to
5M records (blocking key skew, a Dataproc autoscaler quota ceiling, a threshold that didn't
transfer across scale, a local memory ceiling): [docs/scale-run.md](docs/scale-run.md).

## Repository map

See [PROJECT_CONSTITUTION.md #18](PROJECT_CONSTITUTION.md#18-repository-layout) for the target
layout and [#20](PROJECT_CONSTITUTION.md#20-definition-of-done) for what "done" means.

## License

[MIT](LICENSE)
