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
