# Design decisions

Every significant, non-obvious choice gets an entry here, written at the time the decision is
made — not reconstructed later (P12). Alternatives considered and why they were rejected matter
more than the decision itself.

Entry template:

```text
## <short title>

**Phase:** <n>
**Decision:** <what was chosen>
**Alternatives considered:** <what else was on the table>
**Why this:** <the actual reasoning>
```

---

## Tiered dataset strategy (ci / dev / scale) over a single fixed dataset size

**Phase:** 0
**Decision:** One codebase, three tiers (`ci`: 5k, `dev`: 50k, `scale`: 5M), selected via
`--tier` / `MDM_TIER`, resolving to a dataset size, a compute backend, and a dbt target.
Nothing else changes.
**Alternatives considered:** Building against 5M from the start; building only a local demo and
treating scale as an afterthought.
**Why this:** Nobody debugs at 5M — iteration has to happen in seconds, not tens of minutes.
Building the tier switch in before any pipeline logic exists means every later phase is
automatically tier-parameterized instead of needing a retrofit. See
PROJECT_CONSTITUTION.md #4.

## Config-driven tiers instead of hardcoded per-environment scripts

**Phase:** 0
**Decision:** `config/matching.yml` is the single source of truth for tier definitions (and,
progressively, blocking passes, comparator thresholds, scoring method, triage thresholds,
clustering guards, survivorship rules, and GCP settings). `src/mdm/config.py` loads and
validates it; nothing tunable lives in Python (P5).
**Alternatives considered:** Separate `dev.py`/`prod.py` settings modules; CLI flags for every
tunable with no central file.
**Why this:** A reviewer must be able to change the match threshold, the scale tier, or the GCP
project without editing Python. A single YAML file with env-var interpolation for secrets
(`${GCP_PROJECT}`) keeps that true as the config surface grows across later phases, and keeps
the schema visible in one place instead of scattered across modules.

## Editable install (`pip install -e .`) instead of sys.path hacks for cross-process imports

**Phase:** 1
**Decision:** `pyproject.toml` carries a minimal `[project]`/`[build-system]` table and the repo
is installed with `pip install -e .`, so `import mdm...` works identically in the main process,
pytest, and multiprocessing worker processes.
**Alternatives considered:** `sys.path.insert()` shims at the top of `scripts/generate.py`.
**Why this:** Windows' default multiprocessing start method is `spawn`, not `fork` — each worker
re-imports modules from scratch in a fresh interpreter rather than inheriting parent state. A
`sys.path` shim only reliably takes effect if it runs as unconditional top-level code in
whatever module `spawn` treats as `__main__`, which is fragile and easy to break by refactoring.
An editable install makes `mdm` a normal importable package regardless of process/start-method,
which is what actually makes `ProcessPoolExecutor.map(generate_shard_task, ...)` reliable here.

## Fixed shard size, not `num_identities / workers`, drives sharding

**Phase:** 1
**Decision:** `shard_ranges()` partitions identities into fixed-size chunks (`CHUNK_SIZE = 2000`
identities), independent of `--workers`. Shard `i`'s Faker instance and `random.Random` are both
seeded `seed_base + i`. `ProcessPoolExecutor.map` preserves input order, so shards are always
concatenated in the same order regardless of completion order or worker count.
**Alternatives considered:** Splitting `num_identities` into exactly `--workers` chunks.
**Why this:** P6 requires identical output regardless of core count. If chunk boundaries were a
function of `--workers`, running with 1 worker vs. 8 would hand different identity ranges to
different seeds, changing every downstream random draw. Fixing the chunk size decouples "how
the work is partitioned" from "how many processes execute it" — partitioning determinism and
parallelism become orthogonal. Verified directly:
`tests/integration/test_generator_cli.py::test_generator_output_is_identical_regardless_of_worker_count`
runs the real CLI with `--workers 1` and `--workers 3` and asserts byte-for-byte-equal logical
content.

## Appearance count per identity: 2 vendors always, a 3rd with probability 1/12

**Phase:** 1
**Decision:** Every identity appears in exactly 2 of the 3 vendors; with probability `1/12` it
also appears in the 3rd. A separate, independent `2%` chance adds a within-vendor duplicate
registration on top.
**Alternatives considered:** A 3-way weighted choice over {1, 2, 3} appearances (including
identities that appear only once, i.e. no duplicate to find at all).
**Why this:** `config/matching.yml`'s tiers all target `records / identities ≈ 2.0833` (`25/12`).
Solving `2·P(2) + 3·P(3) = 2.0833` with `P(2)+P(3)=1` gives `P(3) = 1/12` exactly when every
identity has a guaranteed 2-vendor floor. This project is explicitly about exercising the
matcher, not modeling realistic real-world overlap rates (which would be far lower) — every
identity having *something* to match against is deliberate stress, not a claim about real
vendor overlap. `target_records` in config is therefore a target, not an exact count: actual
totals land close (5,046 vs. 5,000 at `ci`; 50,486 vs. 50,000 at `dev`) but aren't forced equal.

## Ground truth records the noise actually applied, not the noise requested

**Phase:** 1
**Decision:** `apply_noise()` returns `(field_overrides, actual_noise_type)`. When a requested
noise type can't structurally apply — `nickname` on a name absent from the table, `missing_ssn`
against Vendor C (no SSN field at all), `dob_error` transposition on a day > 12 — it falls back
(to `typo_name`, or a bounded random day offset) and reports what it actually did.
**Alternatives considered:** Recording the requested noise type regardless of whether it applied.
**Why this:** P3 — every number has to be measured, not asserted. If `ground_truth.parquet` said
`nickname` for a row that was actually untouched (no table match), `evaluate.py`'s later
recall-by-noise-type breakdown would be quietly wrong in a way that's invisible without reading
generator internals. Reporting reality here, not intent, is what keeps that breakdown honest.

## Literal dbt schema names via `generate_schema_name`, not dbt's default prefix behavior

**Phase:** 2
**Decision:** `dbt/macros/generate_schema_name.sql` overrides dbt's default macro so a model's
`schema` config (`conformance`, `matching`, `serving`, `quality`) becomes the literal DuckDB
schema / BigQuery dataset name, instead of dbt's usual `{target_schema}_{custom_schema}`.
**Alternatives considered:** dbt's default behavior (`dev_conformance`, `dev_matching`, ...).
**Why this:** §9 of the constitution defines fixed dataset names — `conformance`,
`matching`, `serving`, `quality` — that must mean the same thing addressed the same way on
DuckDB and BigQuery. dbt's default prefixing is designed for multi-developer schema isolation,
which isn't a concern here; it would just make every dataset name target-dependent for no
benefit and break the "one codebase, two backends" parity this project is built around (P8).

## `raw_standard` is a dbt source, not a dbt model

**Phase:** 2
**Decision:** `scripts/load_local.py` / `mdm.backends.local.load_tier_to_duckdb()` loads
generated Parquet directly into `raw_standard.vendor_{a,b,c}` via DuckDB's `read_parquet()`,
with no transformation. dbt will declare these as `source()`s (Phase 3), never `CREATE`s them.
**Alternatives considered:** A dbt seed or an initial dbt model that reads the Parquet.
**Why this:** Mirrors the real architecture the constitution describes: "BigQuery load jobs,
free, no transform" (§7) populate `raw_standard` outside of dbt; dbt's job starts at
conformance. Keeping that boundary in the local backend too is what makes Phase 11 (swapping
the dbt target to BigQuery) a target change, not a re-architecture — `upload_to_gcs.py` +
BigQuery load jobs will do locally what `load_local.py` does now, and dbt's `sources.yml`
doesn't change either way.

## Hand-rolled SOUNDEX for DuckDB, without regex backreferences

**Phase:** 3
**Decision:** `dbt/macros/phonetic_key.sql` dispatches to native `SOUNDEX()` on BigQuery, and to
a hand-rolled equivalent on DuckDB (map letters to digit classes via `translate()`, collapse
adjacent duplicate codes, drop the first letter's own code, strip zeros, pad/truncate to 3
digits). The collapse step uses plain literal `replace()` calls (7 digits × 3 nested passes),
not `regexp_replace(..., '(.)\1+', '\1', 'g')`.
**Alternatives considered:** The doc's own sketch macro assumed DuckDB ships a `soundex()`
function (it doesn't, as of dbt-duckdb 1.10 / duckdb 1.5 — confirmed directly). The regex-
backreference version was tried first and is *arguably* more readable; it computes correct
results standalone but throws `Invalid Input Error: invalid escape sequence: \1` specifically
once nested inside dbt's compiled `CREATE TABLE AS`, reproduced independently of dbt via the
raw DuckDB Python driver. Not chasing the internal cause given a straightforward,
regex-free alternative exists.
**Why this:** Verified against the doc's own worked example — `soundex('SMITH')` and
`soundex('SMTIH')` both resolve to `'S530'` on the DuckDB branch, matching native BigQuery
SOUNDEX's behavior for the same inputs. That's what blocking (Phase 5) actually depends on:
the two branches don't need to be bit-identical in general, only to agree on the
transposition case they exist to survive.

## `try_strptime` / `SAFE.PARSE_DATE`, never a parse that can throw

**Phase:** 3
**Decision:** `dbt/macros/parse_vendor_date.sql` uses DuckDB's `try_strptime(...)::DATE` and
BigQuery's `SAFE.PARSE_DATE(...)` — both return `NULL` on a malformed date instead of failing
the query.
**Alternatives considered:** `strptime()` / `PARSE_DATE()` without the `try_`/`SAFE.` variants.
**Why this:** Directly the Layer 1 -> Layer 2 contract the constitution calls "the single most
useful property of the layering" (§5): a malformed date must become a data quality finding
(`dob IS NULL` downstream, catchable by a dbt test) rather than crash the whole conformance
build over one bad row.
