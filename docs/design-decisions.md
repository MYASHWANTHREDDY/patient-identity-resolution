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

## Deterministic baseline as vectorized self-merges, not O(n^2) pairwise comparison

**Phase:** 4
**Decision:** `deterministic_match_pairs()` computes the SSN-exact and name+dob-exact rules as
`pandas.merge` self-joins on the key columns, not a pairwise scan or the Phase 5 blocking
infrastructure.
**Alternatives considered:** Reusing blocking (not built yet at Phase 4); `itertools.combinations`
over grouped records.
**Why this:** Both deterministic rules are literally equi-joins — group by the key, pair up
everyone in the same group. That's what `merge` does natively and vectorized; no candidate-pair
generation or blocking is needed for exact-match rules at all, at any scale, which is also why
Stage 1 (§11.1) is listed before Stage 2 (blocking) in the matching methodology.

## Measured baseline recall is ~33% on corrupted fields, not near-zero, because of SSN

**Phase:** 4
**Decision:** No code decision here — a measured result worth recording so it isn't
re-derived from scratch later, and so the number doesn't look like a bug on first read.
**What was measured:** At `dev` tier, deterministic recall is 100% for `exact` and
`missing_ssn` pairs (expected — neither noise type touches name/DOB, and `missing_ssn` still
leaves name+DOB exactly matching), but ~33% — not ~0% — for `typo_name`, `dob_error`, and
`nickname` pairs, where only the name or DOB field was corrupted.
**Why:** A noise type corrupts exactly one field per record; SSN survives untouched on a
`typo_name`/`dob_error`/`nickname` record unless that appearance happens to be on
`VENDOR_C` (which carries no SSN field at all). Each identity's appearances land on 2 of 3
vendors uniformly; a pair avoids `VENDOR_C` on both sides in exactly 1 of the 3 possible vendor
pairs `{A,B}, {A,C}, {B,C}` — 1/3 ≈ 33%, matching the measured ~32–33% almost exactly. Overall
precision is 0.9999 (2 false positives out of 15,463 predicted pairs at `dev` tier) —
coincidental SSN or name+DOB collisions between different synthetic identities, plausible at
this cardinality and not investigated further at this phase.
**How to apply:** Don't be surprised if a future re-read of `docs/results.md` shows this
pattern again — it's SSN coverage, not a scoring bug. This is exactly the kind of thing
Fellegi-Sunter (Phase 6) is supposed to improve on by using near/similar name and DOB
agreement instead of requiring exact string equality.

## Measured Pair Completeness is 0.9533 at dev tier, short of the 0.98 target — and why

**Phase:** 5
**Decision:** No code decision — a measured shortfall against §12's target, worth recording
precisely so it isn't mistaken for a bug on a future read, and so the explanation survives
follow-up (P4).
**What was measured:** At `dev` tier, unioned blocking gets RR = 0.999729 and PC = 0.9533
against the four passes specified in `config/matching.yml` / PROJECT_CONSTITUTION.md #5 (all
four exactly as documented — `bp_ssn`, `bp_dob_lname`, `bp_year_names`, `bp_coarse`). 1,355 of
29,012 true pairs are found by none of the four passes; 88% of those (1,193) are `typo_name`
pairs, the rest mostly `multiple` (both sides corrupted).
**Why:** Three of the four passes — `bp_dob_lname`, `bp_year_names`, `bp_coarse` — all key
partly on `last_name_phonetic`. When a `typo_name` corruption lands on the last name and
happens to shift its Soundex code (confirmed directly: one missed pair has
`last_name_phonetic` `Y620` on one side and `Y260` on the other), all three fail
*simultaneously* — they don't fail independently, so the multi-pass union doesn't help here
the way it does for other corruption types. The fourth pass, `bp_ssn`, could rescue it, but
only when SSN is present on both sides — and Vendor C never has an SSN field at all
(PROJECT_CONSTITUTION.md #8). A last-name typo landing on any pair that includes a Vendor C
appearance is structurally unblockable by this exact 4-pass design.
**How to apply:** This is a real, explainable ceiling on this specific pass design, not a bug —
don't "fix" it by quietly padding results. The clean fix would be a 5th pass keyed on
`first_name_phonetic` + `dob` (independent of last name entirely), which would rescue exactly
this failure mode; not added here because it would diverge from the 4-pass architecture as
specifically diagrammed in PROJECT_CONSTITUTION.md #5 and #16. Tracked as future work, in the
same spirit as Double Metaphone and address matching (§23). Re-measure at the Phase 14 scale
run — pass selectivity and skew both change at 5M in ways that could shift this number either
direction.

## Bug: NaN silently misread as "different" instead of "missing" in every comparator

**Phase:** 6
**Decision:** Added `_is_missing()` to `comparators.py` — checks `value is None`, then
`value != value` (true only for NaN and pandas `NaT`, the "not equal to itself" trick, chosen
so the comparator module stays pandas-free per P8), then `value == ""`. All four comparators
now call it instead of a bare `not value` truthiness check.
**How it was found:** `scripts/estimate_fs_params.py`'s first real run produced nonsensical
m/u estimates — `ssn.different` at m≈0.70 (SSN essentially never repeats between two records
of the same person, so this should be near-zero) and `ssn.missing` at m≈0.00003 (should be
the dominant level, since Vendor C never has SSN at all). Traced to
`DataFrame.set_index(...).to_dict(orient="index")`: pandas silently turns a SQL NULL into
`float('nan')` during that row-wise conversion instead of leaving it `None`. `not float('nan')`
evaluates to `False` in Python (NaN is truthy), so `compare_ssn`'s `if not a or not b` never
caught it — execution fell through to `a == b`, which is `False` for NaN against anything
(including itself), landing on `"different"`. A missing field was scored as active
disagreement instead of contributing zero weight — exactly backwards from the Fellegi-Sunter
design in #11.4 ("an uncomparable field contributes exactly zero weight rather than a
disagreement penalty").
**Why the fix lives in the comparators, not the caller:** P8 requires the comparators to be
pure and backend-agnostic. Fixing only `load_records_by_key`'s pandas conversion would leave
every other caller (Spark, a future match API) exposed to the same class of bug if it hands
comparators a different null sentinel. Regression tests cover NaN and a duck-typed `NaT`-like
object (`test_comparators.py`).
**Re-verified after the fix:** `ssn.different` weight is now **-13.49 bits** (strong evidence
*against* a match — SSN disagreement is close to disqualifying) and `ssn.missing` is **+0.22
bits** (near-neutral, as expected: missingness is driven mostly by Vendor C coverage, which is
only slightly more common among true-match pairs than random ones in this noise model). Both
numbers are now explainable; before the fix, neither was.

## m/u for structurally-impossible levels land at ≈ -0.54 bits, not exactly 0

**Phase:** 6
**Decision:** No code decision — correcting an earlier (wrong) prediction in this document,
now that real numbers exist.
**What was predicted (Phase 4 entry, before this data existed):** That "missing" agreement
would land at ≈0 bits across the board because missingness should be equally likely under
both the true-match and non-match hypotheses.
**What was actually measured:** True for genuinely-populated levels (`ssn.missing` = +0.22
bits, see above). But levels that structurally *never occur* in either sample — `last_name`
`nickname` (nicknames only ever land on first names in this generator), any field's `missing`
level where the field is never actually absent — settle at **exactly -0.5365 to -0.5366
bits**, not 0. Cause: `m` is Laplace-smoothed over 29,012 true pairs, `u` over a 20,000-pair
sample — different denominators. A level with zero observed count in *both* samples gets
`m_floor = 1/(29012+6)` and `u_floor = 1/(20000+6)`, and `log2(m_floor/u_floor) ≈ -0.54`
regardless of which field or level it is. It's a fixed artifact of the smoothing floors, not
signal.
**How to apply:** A weight near -0.54 bits with `m` and `u` both at or near their smoothing
floor means "never observed," not "weak negative evidence" — read the `m`/`u` values in
`config/fs_params.yml` alongside the weight before interpreting it. Equalizing `m`/`u` sample
sizes would remove the artifact but isn't worth the complexity here — no candidate pair should
realistically land on one of these levels anyway.

## Validated: gender agreement really is worth "about one bit," as predicted

**Phase:** 6
**Decision:** No code decision — recording a clean validation of the constitution's own
prediction (§22 talking points: "gender agreement is worth about one bit").
**What was measured:** `gender.exact` weight = **1.026 bits** at `dev` tier. `gender.different`
= -13.85 bits (strong negative — two records disagreeing on gender essentially rules out a
match in this data, since gender is generated deterministically per identity and never
independently corrupted).
**How to apply:** Good evidence the estimation pipeline is producing sane numbers — a
prediction made before any code existed came out almost exactly right once real data ran
through it.

## `find_thresholds` clamps `lower` to never exceed `upper`

**Phase:** 6
**Decision:** `threshold_sweep.find_thresholds()` computes `upper` (lowest score meeting the
precision target) and `lower` (highest score meeting the recall target) independently, then
clamps `lower = min(lower, upper)` before returning.
**What was found:** On the real dev-tier candidate-pair population, the independent
computation produced `upper=7.8924` and `lower=21.0092` — `lower > upper`. Root cause isn't a
bug in either target individually: this scorer separates true from non-matches so cleanly
that precision stays >= 0.99 down to a fairly low score (few false positives creep in until
then), while recall already hits >= 0.99 at a much higher score (almost all true matches
score very high, so excluding everything below 21 barely costs any recall). Both facts are
correct; naively combining them crosses. Left unclamped, this silently breaks
`triage.decide()`, which checks `score >= upper` first — a `lower` above `upper` becomes dead
code and the review band silently evaporates (confirmed: review queue count was exactly 0
before understanding why, and remained 0 after the clamp — but now for an explained reason
instead of a hidden one).
**Why this fix, not e.g. swapping or averaging:** Clamping preserves `upper`'s meaning exactly
(the precision-driven auto-match line doesn't move) and collapses the review band to
zero-width exactly where the two targets stop overlapping — the mathematically minimal
correction. A regression test (`test_find_thresholds_never_returns_lower_above_upper`)
guards `lower <= upper` as an invariant regardless of input.
**How to apply:** A zero-width review band is a legitimate outcome when a scorer separates
classes cleanly on the candidate-pair population — not evidence of a bug by itself. Check
`upper` vs `lower` before assuming something's wrong; they should differ only when there's a
genuine reachable middle ground between the two targets.

## Fellegi-Sunter and the naive scorer land within 0.0001 F1 of each other

**Phase:** 6
**Decision:** No code decision — a measured result that complicates the "F-S beats naive"
narrative the framework would predict, worth recording precisely.
**What was measured:** Best achievable F1 across all thresholds, over the full candidate-pair
population (345,976 pairs from blocking) at dev tier: Fellegi-Sunter 0.9963, naive
(hand-tuned weights) 0.9962. The PR curves (`docs/img/pr_curve.png`) are visually
indistinguishable.
**Why they tie:** Blocking already did most of the discriminating work before either scorer
sees a pair. Within the candidate-pair population specifically (not all possible pairs),
agreement patterns cluster at the extremes — mostly-everything-agrees or
mostly-everything-disagrees — with little genuinely ambiguous middle ground. When the
population is that separable, almost any reasonable monotonic combination of comparator
agreement levels ranks pairs correctly, so a hand-tuned weighting and a data-derived one
converge to nearly the same ranking. The two scorers would very likely diverge more on a
harder population — pairs deliberately sampled to be ambiguous, or blocking passes that
admit more borderline candidates.
**How to apply:** Don't claim F-S "wins" here — it doesn't, measurably. The honest framing
(consistent with §11.4's own argument) is that F-S's real advantage isn't raw F1 on this
population, it's that its weights are *derived*, not guessed — defensible without hand-waving
even when performance is a statistical tie. Worth revisiting at the Phase 14 scale run, where
the candidate population is ~1000x larger and may include more genuinely ambiguous pairs.

## Crosswalk: an id can belong to at most one cluster per run

**Phase:** 7
**Decision:** `crosswalk.resolve_crosswalk()` processes clusters in a deterministic order
(sorted by each cluster's smallest member record_key) and tracks which existing
`patient_global_id`s have already been claimed this run. If a later cluster's only
overlapping existing id was already claimed by an earlier cluster, that id is treated as
unavailable — the later cluster mints fresh (or merges among whatever other ids remain
available), and the departure is logged as a `split`.
**What was wrong with the first version:** The initial implementation resolved each cluster
independently — "does this cluster's members have an existing id? reuse it." Two *different*,
now-disconnected clusters that both used to share one old id would each independently see
"exactly one existing id" and both reuse it, silently applying the same `patient_global_id`
to two disconnected groups of records in the same run. That's not a split — a split becoming
invisible is arguably worse than not detecting it, since it would `LEFT JOIN` in the serving
layer as if nothing happened. Caught by actually reasoning through a "record moved" idempotency
scenario before writing the test, not by a failing test after the fact.
**Why claiming-order determinism, not e.g. largest-subgroup-wins:** Ties back to the same
principle used everywhere else in this codebase (dbt clustering by phonetic key, generator
sharding, survivorship's tiebreak): pick the rule that's cheap to state and impossible to get
inconsistent results from. Sorting by smallest member `record_key` is arbitrary but total and
stable — two runs over identical input always claim ids in the same order, satisfying P6.
**Merge and split are mutually exclusive per id, per run:** A cluster that gains an id through
merging never also logs a split for the ids it retired (those are exactly the `MERGE` events);
split is reserved for ids that lose members without a merge explaining where they went. An
earlier draft double-logged both for the same transition — pure noise, since the merge event
already says everything the split event would have.

## Pipeline-level NaN sanitization, not another per-field guard

**Phase:** 7
**Decision:** `pipeline.py` added `_sanitize_nan()`, converting every `float('nan')`/`NaT`
value to `None` immediately after `DataFrame.to_dict()`, once, at the point pandas data enters
the matching pipeline. `comparators.is_missing()` (Phase 6 fix, now exported instead of
private) is reused directly by `survivorship.py`'s candidate filter.
**What happened:** The exact same pandas-silently-produces-NaN failure mode fixed once in
`comparators.py` (Phase 6) reappeared immediately in `pipeline.py` — `golden_record["ssn_last4"]
= ssn[-4:] if ssn else None` crashed with `TypeError: 'float' object is not subscriptable`
the first time `run_matching` actually ran against real data, because `survive_field`'s own
null-filter (`m.get(field_name) not in (None, "")`) had the identical blind spot to `compare_ssn`'s
original bug.
**Why sanitize at the boundary this time, instead of only hardening each function again:**
Chasing this class of bug function-by-function doesn't converge — every new pure function that
touches pandas-sourced data is a new place for it to hide. Fixing it once where pandas data
*enters* the system (immediately after `to_dict`) means every downstream pure function
(comparators, survivorship, and whatever Phase 8+ adds) can simply trust `None` as the only
"missing" representation. `survivorship.py` was also hardened directly with `is_missing()` as
defense-in-depth — belt and suspenders, since a future caller that skips `_sanitize_nan` should
still get correct behavior, not a silent miscount.

## A real false merge, found by inspection: two different people, one Faker name collision

**Phase:** 7 (found while spot-checking `serving.member_360` in Phase 8)
**Decision:** No code decision — a genuine false merge in the dev-tier output, worth
recording precisely rather than quietly re-running with a different seed until it disappears.
**What was found:** `PGID000000008946` merged 5 records into one golden record
("ANGELA RICE", 1970), but ground truth shows they're two different people:
`ID00005001` (3 records, DOB 1970-01-28/30) and `ID00012826` (2 records, DOB 1970-11-13) --
a Faker first+last name collision across two distinct synthetic identities, plus a shared
birth year.
**How it happened:** `VENDOR_A:00012826`/`VENDOR_B:00012826` carry a real SSN, present and
different from `ID00005001`'s SSN -- that disagreement is strong evidence against a match
(-13.49 bits, Phase 6) and should have kept the two identities apart directly. The bridge was
`VENDOR_C:00005001`, which has no SSN at all (Vendor C structurally never does). Against that
record, SSN disagreement can't fire -- it scores `missing` (+0.22 bits, near-neutral) instead
of `different` (-13.49 bits), while name (`near`, "ANELA" vs "ANGELA") and DOB (`year_only`,
same year) both contribute positive weight. That's enough alone to auto-match
`VENDOR_C:00005001` to the `ID00012826` side, chaining two otherwise well-separated identities
into one cluster through the one connecting record that couldn't contradict it.
**Why the density guard didn't catch it:** This is exactly the transitive-closure failure mode
§13.2 names as "the most dangerous failure mode" -- but a 5-member cluster with several direct
edges among the true sub-groups still clears `min_cluster_density = 0.6` even with one bridging
edge doing the work of connecting two real clusters. The guard catches sparse chains; this one
wasn't sparse enough.
**How to apply:** This is the honest, unforced version of the "worst false positive" the
constitution's own talking points ask for (§22) -- found by reading `member_360` output, not
manufactured. Don't paper over it: a real MDM system would route a bridging record with no
SSN and only `near`/`year_only` agreement to clerical review rather than auto-match, which
argues for a per-edge confidence check in clustering (not just per-cluster density) as future
work, alongside the already-noted first_name+dob blocking pass (Phase 5 entry above).

## dbt runs in two passes, not one, once the serving layer exists

**Phase:** 8
**Decision:** The pipeline invokes `dbt build` twice: once for conformance + blocking
(`--exclude path:models/serving snap_member_demographics`) *before*
`scripts/run_matching.py`, and again, unrestricted, *after* it. The Makefile's `pipeline`
target encodes this as `data -> dbt-build-pre -> estimate-params -> match -> dbt-build ->
evaluate -> quality-checks`.
**What broke first:** Adding `dbt/models/serving/sources.yml` (declaring `serving.crosswalk`,
`serving.member_demographics`, etc. as sources with schema tests) made every *existing*
integration test's single `dbt build` call start failing — those tables don't exist until
Python's `run_matching()` creates them, and dbt's source tests run against whatever the
source config says exists, so `dbt build` on a fresh database errors on `Catalog Error: Table
... does not exist` before it ever reaches `run_matching`. The snapshot
(`snap_member_demographics`, a distinct dbt resource type from `models/`) has the identical
dependency and needed its own exclusion.
**Why this is the correct shape, not a workaround:** It matches the architecture as diagrammed
in PROJECT_CONSTITUTION.md #5 — `SURV --> SRV` (survivorship writes serving tables) happens
*before* `SRV --> DBT4[dbt: serving views + snapshots]`. dbt genuinely owns two disjoint
phases of this pipeline (conformance/blocking before matching; serving views/tests/snapshot
after), not one. Trying to force a single `dbt build` invocation to cover both was the actual
bug — the fix is sequencing, not loosening a test.
**How to apply:** Any new dbt source or model that reads a Python-written table needs to land
in the *second* pass. If a future model needs both a Python-written table and a dbt-only
upstream (like `patient_normalized`), it belongs in `models/serving/` and gets excluded from
the pre-matching build alongside everything else there.
