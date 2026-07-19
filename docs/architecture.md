# Architecture

What this system is built from, and why each structural choice was made. For the matching
algorithm specifically (blocking, scoring, clustering, survivorship), see
[matching-methodology.md](matching-methodology.md). For the story behind any individual
decision, see [design-decisions.md](design-decisions.md); for what happened at real 5M-record
scale, see [scale-run.md](scale-run.md).

## The problem this solves

Three healthcare vendors each send patient records under their own schema, with no shared
identifier. The same person appears in all three feeds — spelled differently, with a
transposed birth date, under a nickname — and occasionally twice within one vendor's own feed.
The system's job is to figure out, without any shared key, which records describe the same
real person, and produce one stable `patient_global_id` per person with a full audit trail of
how that conclusion was reached.

## Three layers, one direction

Data moves through three layers, each with a narrower job than the last. Nothing skips a
layer, and nothing writes backward.

```
raw_standard  →  conformance  →  matching  →  serving
(as received)    (one shape)     (who's who)   (golden records)
```

**`raw_standard`** — vendor data exactly as received. Vendor field names preserved
(`fname`/`given_name`/`first_name` stay distinct across the three vendors). Dates stored as
`STRING`, in whatever format the vendor sent (`MM/DD/YYYY`, `DD-Mon-YYYY`, ISO). No cleaning,
no coercion.

> **Why dates as `STRING` here, not `DATE`?** Parsing on ingest turns a malformed date into a
> load failure, destroying the evidence of what the vendor actually sent. Parsing belongs one
> layer downstream, in conformance, where a bad date is a *data quality finding you can report
> on* rather than a pipeline crash that loses the record entirely.

**`conformance`** — one unified schema (`patient_normalized`): dates parsed to `DATE`, gender
standardized to `M`/`F`/`U`, names cleaned and case-folded, phonetic keys (`SOUNDEX`)
precomputed. Still one row per source record — **no de-duplication happens here.** Every row
carries `source_vendor` + `source_record_id`, so lineage back to layer 1 is never lost.

**`matching`** — candidate pairs, scores, and clusters. Disposable intermediates: at the scale
tier, `matching.candidate_pairs`/`matching.pair_scores` carry a 7-day BigQuery table expiration
(everything else is permanent), because they're fully reproducible from `conformance` plus
config and cost real money to keep around at hundreds of millions of rows.

**`serving`** — the actual deliverable. `member_demographics` (the golden record),
`member_alternate_identifier` (the vendor-ID → `patient_global_id` lookup — see below on why
this table, not the golden record, is the real product), `field_lineage` (which source won
each field and why), `crosswalk` + `identity_events` (identity stability across runs),
`review_queue`, and a `dbt snapshot` that keeps SCD2 history every time a golden record
changes.

> **Why `ssn_last4`, never the full SSN, in `member_demographics`?** Full SSN is a comparison
> *signal* during matching, then deliberately not propagated to serving. Even on synthetic
> data, the pipeline is built to the minimization discipline real PHI would require.

> **The golden record is the headline; the crosswalk is the product.** When a downstream
> system holds a Vendor B record ID and needs to know which unified patient it belongs to,
> `member_alternate_identifier` is the table it actually queries.

## One codebase, two backends

Every tier runs the identical comparator, scoring, clustering, and survivorship logic — pure
Python functions with no I/O — over two different execution backends, selected by a single
`--tier` flag:

| | `ci` | `dev` | `scale` |
| --- | --- | --- | --- |
| Records | 5,000 | 50,000 | 5,000,000 |
| Backend | DuckDB + pandas | DuckDB + pandas | BigQuery + Dataproc Serverless |
| dbt target | `dev` | `dev` | `prod` |
| Where it runs | GitHub Actions | a laptop | GCP |
| Needs a cloud account? | No | No | Yes |

Locally, the matching loop runs as a plain Python `for` loop over a pandas DataFrame
(`mdm.pipeline.run_matching`). At scale, the *same* comparator/scoring functions run inside
`mapInPandas` on Dataproc Serverless (`mdm.backends.spark.score_candidate_pairs`) — identical
logic, invoked once per Spark partition instead of once per Python loop iteration. SQL
transforms (conformance, blocking) are written once in dbt and run against both DuckDB and
BigQuery via dialect macros. Divergence between tiers is treated as a defect: `scripts/
verify_tier_parity.py` diffs DuckDB and Spark output on the same input and asserts zero
mismatches — checked in this project's own history, not just claimed.

**Why this design, not two separate implementations?** A second, cloud-only implementation
would let bugs drift silently between "what runs in CI" and "what runs against real money."
One codebase means a bug fixed at the 5,000-record tier is fixed everywhere, and every fix
gets tested for free, every push, before it ever touches a cloud bill.

## The two backend choices worth defending

**Why BigQuery for blocking but Spark for scoring, not one engine for both?** BigQuery ships
`SOUNDEX` natively and blocking is a pure self-join on a block key — BigQuery does that more
cheaply than Spark, in one dbt model instead of a Spark job. But scoring needs real
name-similarity comparators (Jaro-Winkler via `rapidfuzz`), and BigQuery has no native
equivalent — reaching for `EDIT_DISTANCE` just because it's built in would let the tool
dictate the methodology instead of the other way around. So scoring goes to Spark. Each engine
does what it's actually best at; that's a stronger answer than "wanted to show both."

**Why `SOUNDEX` for blocking, not Double Metaphone?** Metaphone is the better encoder, but
BigQuery has no native implementation — it would need a JavaScript UDF loaded from GCS, a
fiddly dependency for a marginal gain. `SOUNDEX` is native and handles the corruption that
actually matters for blocking: `SOUNDEX('SMITH')` and `SOUNDEX('SMTIH')` both yield `S530`, so
the transposition case blocking exists to survive is covered. Choosing the weaker-but-native
function, and being able to say precisely what was given up and why it doesn't matter for this
corruption profile, is more defensible than reaching for the theoretically superior option
without knowing the difference. (Double Metaphone via a BigQuery JS UDF is noted as future
work — see the constitution's stretch phase.)

## Orchestration

Three Airflow DAGs (Docker locally, never Cloud Composer — see cost discipline below), each
owning a stage that fails and re-runs independently:

- **`ingestion_dag`** — GCS Parquet → `raw_standard`, one BigQuery load job per vendor,
  parallel, idempotent (`WRITE_TRUNCATE`).
- **`conformance_dag`** — `dbt run` for staging/conformance/blocking, then `dbt test`.
- **`dedup_dag`** — Dataproc scoring → clustering → crosswalk/survivorship → `dbt run`
  serving → `dbt snapshot` → quality gates.

Retries: 2 with backoff on ingestion and Dataproc batches (a transient submission hiccup
shouldn't fail the whole run). Zero retries on quality gates — a quality-gate failure is real
information about the data, not a transient fault worth silently retrying.

## Cost discipline

Real GCP spend this project has incurred is documented, not estimated: **~$77.57** for the
full 5M-record scale run (see [scale-run.md](scale-run.md) for the per-stage breakdown), drawn
entirely from GCP's free-trial credit. The guardrails that kept it there:

- **Dataproc Serverless only, never a persistent cluster.** A forgotten always-on cluster is
  the classic way to lose a credit balance overnight; serverless batches terminate themselves.
- **Airflow in Docker, never Cloud Composer.** Composer bills for a running environment, not
  per-DAG execution — the monthly floor could consume most of a $300 budget in one month.
- **`maximum_bytes_billed` set on the BigQuery `prod` target** (`dbt/profiles.yml`) — dbt
  hard-fails a runaway query instead of billing it. Sized to what real scale-tier queries
  actually need (currently 50 GiB; see design-decisions.md for the incident that set this).
- **7-day table expiration on the `matching` dataset** — disposable intermediates
  (`candidate_pairs`, `pair_scores`) don't accumulate storage cost between runs.

## Non-negotiable principles that shaped this design

The full list (13 of them) lives in the project's own build spec; the ones most visible in
the architecture itself:

- **Measured, not asserted.** Every number in this project's docs is produced by a script
  whose output is reproducible — `python -m mdm.evaluate` regenerates `results.md`;
  `docs/scale-run.md`'s numbers came from real `gcloud`/`bq` commands, not projections.
- **One codebase, two backends.** Covered above — divergence between tiers is a defect, not a
  tradeoff.
- **Idempotency.** Every stage is safe to re-run against the same input without duplicating
  rows or churning identifiers — tested (`resolve_crosswalk`'s "what happens on the second
  run" behavior), not assumed.
- **Safety-aware defaults.** A false merge (two different people combined into one record) is
  a clinical safety incident; a false split (one person left as two) is a data quality
  problem. These costs are not symmetric — clustering's density/size guards flag uncertain
  merges for review rather than silently completing them. See
  [matching-methodology.md](matching-methodology.md) for how this shapes clustering and
  triage specifically.
