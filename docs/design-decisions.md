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

> **Later qualification (2026-08-23):** this holds only while `upper` is also the cutoff the
> auto-match decision actually uses. At the scale tier it wasn't -- clustering ran at 20.5
> while the band came from this file's 9.5203 -- and the same "zero-width is fine" reasoning
> then hid an 11-point gap where pairs were silently dropped. See "Thresholds were a single
> global pair for a per-tier quantity" below.

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

## Dashboard reads generated files, never recomputes scoring live

**Phase:** 9
**Decision:** The "Match quality" and "Blocking & skew" tabs render `docs/results.md`
(parsed by section heading) and `docs/img/pr_curve.png` directly, instead of recomputing
precision/recall/the PR curve from `matching.candidate_pairs` on every page load.
**Why:** `python -m mdm.evaluate` scoring all 345,976 dev-tier candidate pairs (Python-level
comparator calls, not vectorized SQL) takes over a minute end to end. A Streamlit page
load that re-ran that on every tab switch would make the dashboard unusable long before it
made the numbers any more current — `docs/results.md` is already the canonical, generated
source of truth for these numbers (P3), regenerated by re-running `evaluate`/`run_matching`,
not by opening a browser tab.
**How to apply:** Any dashboard tab that would need a similarly expensive live query should
default to reading a persisted artifact (a `serving.*`/`quality.*` table, or a generated
report) rather than recomputing. Tabs that read directly from DuckDB tables (Review queue,
Golden records, Quality history) are fine live — those are indexed lookups against tables
that already exist, not full rescoring.

## Dashboard verified headless via Streamlit's AppTest, not a manual browser session

**Phase:** 9
**Decision:** `tests/integration/test_dashboard.py` runs `dashboard/app.py` through
`streamlit.testing.v1.AppTest`, which executes the real script server-side and exposes every
rendered element (including exceptions per tab) without a browser — and is what actually
caught two real bugs before commit: a `use_container_width` deprecation warning on every
`st.dataframe` call, and would have caught a raw f-string SQL interpolation if the search/
lineage-lookup queries had raised (they didn't, but were hardened to parameterized queries on
review anyway, consistent with treating even a local single-user tool's SQL construction as
worth getting right).
**Why not skip verification entirely, or only check it imports:** A dashboard is exactly the
kind of code where "it imports fine" and "it renders correctly" diverge most — Streamlit
defers almost everything to runtime script execution. AppTest is the officially supported way
to exercise that in CI without a real browser and a display server; it caught real issues
here, not hypothetical ones.

## No "tier parity with Spark" in CI yet — Spark doesn't exist until Phase 12

**Phase:** 9
**Decision:** `.github/workflows/ci.yml` runs lint, unit tests, and the full ci-tier pipeline
(`make pipeline TIER=ci` + `pytest tests/integration`) — not the "tier parity with local
Spark" step the constitution's testing strategy (§17) describes for the finished CI workflow.
**Why:** P3 — don't claim a capability that doesn't exist. `src/mdm/backends/spark.py` and the
Spark scoring job are Phase 12 deliverables; there is no second backend yet to compare against,
so a tier-parity check would either be fake or silently vacuous. Add it in Phase 12, once
`backends/spark.py` exists and a real parity comparison is possible.

## GCP project creation, billing link, and budget alert are manual gcloud steps, not Terraform

**Phase:** 10
**Decision:** `gcloud projects create`, `gcloud billing projects link`, and
`gcloud billing budgets create` were run directly, once, outside of Terraform. Terraform
(`terraform/`) owns everything *inside* the project: the GCS bucket, BigQuery datasets,
service account, and IAM bindings.
**Why:** Terraform needs an authenticated provider pointed at a project that already has
billing enabled before it can create anything — there's a bootstrapping order dependency
that can't be expressed inside the same Terraform run cleanly. Budgets are also
billing-account-scoped, not project-scoped, which sits awkwardly in a `terraform destroy`
blast radius anyway: `terraform destroy` should tear down the project's *resources*, not the
account-level guardrail that's supposed to survive exactly that kind of accident. Real
production Terraform setups draw this same line (a bootstrap project/script creates the
project and billing link; the main IaC repo starts after that).
**What was verified before creating anything billed:** the target billing account
(`01C27B-B1CA16-5C3547`) was confirmed to still be in **free trial** status via the Console
(not derivable from `gcloud` — the CLI doesn't expose trial-vs-upgraded as a field) — $300
untouched, 81 days remaining. Free trial means GCP **stops resources rather than charging the
card** if the credit is exhausted; the one action that would ever expose the card is manually
clicking "Upgrade," which nothing in this project's automation does.
**How to apply:** Never call any GCP-provisioning command (`terraform apply`, `gcloud ... create`)
without first confirming a project + billing account are in the state you expect. This project's
$300 credit is shared across the whole Google account, not scoped to `patient-dedup-mdm` alone —
check the Console's "Top projects" cost breakdown before assuming this project's spend is the
only draw on it.

## `gcloud` needs `shutil.which()` resolution on Windows, not a bare name in subprocess.run

**Phase:** 10
**Decision:** `scripts/upload_to_gcs.py` resolves the `gcloud` executable via
`shutil.which("gcloud")` before passing it to `subprocess.run(..., shell=False)`, instead of
just using the literal string `"gcloud"`.
**What broke:** The first real (non-mocked) invocation of the script failed with
`FileNotFoundError: [WinError 2] The system cannot find the file specified` — on Windows,
`gcloud` resolves to `gcloud.cmd`, and `CreateProcess` (what `subprocess.run` uses under
`shell=False`) can't launch a `.cmd` wrapper by its bare, extension-less name the way a POSIX
shell's `PATH` lookup would. `shutil.which` performs the same `PATHEXT`-aware resolution the
shell does, and returns the full path either way (`gcloud.cmd` on Windows, `gcloud` on POSIX),
so the fix is portable rather than Windows-specific special-casing.
**How to apply:** Any future script that shells out to a CLI tool that might be a `.cmd`/`.bat`
wrapper on Windows (gcloud, npm, etc.) should resolve it via `shutil.which()` first rather than
relying on `subprocess.run`'s bare-name lookup — cheap insurance that costs nothing on POSIX.
Caught by actually running the script against the real bucket after `terraform apply`, not by
the mocked unit test alone (the mock happily accepted the untranslated `"gcloud"` string).

## `terraform destroy` verified via a real destroy-then-reapply round trip, not just `plan -destroy`

**Phase:** 10
**Decision:** Phase 10's exit criterion ("`terraform destroy` → clean") was verified by
actually running `terraform destroy` against the live project, confirming 0 resources
remained, then immediately re-running `terraform apply` to restore the infrastructure for
continued work.
**Why not just `terraform plan -destroy` (read-only, lower-risk):** All 11 resources here (an
empty GCS bucket, 6 empty BigQuery datasets, a service account, 3 IAM bindings) cost nothing
whether they exist or not — there's no financial reason to avoid the real round trip, and a
`-destroy` plan only proves Terraform *intends* a clean teardown, not that GCP actually
executes it cleanly (e.g. a dataset with a lingering table, an IAM binding that doesn't fully
detach). P3 — measured, not asserted.
**How to apply:** This round-trip pattern (destroy, verify empty, reapply) is a good template
for the "before the first 5M run" cost guardrail checklist in §7 — repeat it before Phase 14's
scale run specifically, since by then the datasets won't be empty and the round trip will
carry real signal about whether teardown actually reclaims billed storage.

## `dbt/profiles.yml.example`'s `prod` target defaulted to the wrong BigQuery location

**Phase:** 10
**Decision:** Changed the `location` env var default from `'US'` (multi-region) to
`'us-central1'` (the region Terraform actually provisions datasets in).
**Why it matters:** BigQuery queries fail (or, worse, silently create a second copy of a
dataset in a different location) when a query's location doesn't match where the referenced
dataset actually lives. `'US'` was a reasonable-looking placeholder written in Phase 2, before
any real infrastructure existed to check it against — Phase 10 is the first point this default
could actually be verified, and it was wrong. `GCP_REGION` still overrides it, so this only
matters for whoever runs `prod` without setting it explicitly.

## BigQuery's native SOUNDEX doesn't match the textbook algorithm — switched to one hand-rolled algorithm on both targets

**Phase:** 11
**Decision:** `phonetic_key()` no longer dispatches to BigQuery's native `SOUNDEX()`. The
same hand-rolled algorithm from Phase 3 (originally written only because DuckDB lacks a
native Soundex) now runs on **both** targets, verified byte-identical via `TRANSLATE`/
`SUBSTR`/`RPAD`/`REPLACE` — all four behave identically on DuckDB and BigQuery.
**What was found:** Phase 11's actual parity check (`scripts/verify_tier_parity.py`) against
the same 50k dev-tier input showed `conformance.patient_normalized` and
`matching.candidate_pairs` diverging — 398,507 candidate pairs on DuckDB vs. 421,049 on
BigQuery. Traced to a single root cause: `SOUNDEX('SANCHEZ')` returns `'S522'` from the
hand-rolled algorithm (and matches manually tracing the textbook American Soundex algorithm
by hand) but `'S520'` from BigQuery's native `SOUNDEX()`. BigQuery doesn't document its exact
Soundex implementation closely enough to reverse-engineer with confidence, so matching it
byte-for-byte wasn't a tractable goal — Phase 3's own note ("the two branches don't need to be
bit-identical in general") turned out to be too permissive once Phase 11 actually needed
identical blocking behavior across tiers, not just agreement on one hand-picked test case.
**Why this fix over accepting the divergence:** P8 states divergence between tiers is a
defect, and Phase 11's stated exit criterion is identical results, not "close enough" results.
Using the *same* logic on both engines — rather than each engine's own native function —
is the only way to *guarantee* identical output by construction instead of hoping two
different implementations happen to agree. This is a stronger version of the pattern already
used for `strip_non_digits`/`as_string`/`null_string`: prefer portable, explicitly-shared SQL
over relying on native functions whose exact behavior isn't fully specified.
**How to apply:** Re-verified end to end: rebuilt both targets, re-ran
`scripts/verify_tier_parity.py`, both `patient_normalized` (50,486 rows) and
`candidate_pairs` (398,507 rows) now match exactly. If a future native BigQuery function is
tempting for performance reasons, check whether it's precisely spec'd before trusting it to
agree with a hand-rolled or other-engine equivalent — "same name" does not imply "same
algorithm."

## pandas 3.0's new string dtype silently corrupts to the literal string "nan" through PySpark's legacy pandas-to-Spark conversion — fixed the test harness, not the backend

**Phase:** 12
**Decision:** The local Spark/pandas parity test (`spark_local_test.py`, not part of the
shipped codebase) now seeds Spark by writing pyarrow Parquet files and having Spark read
them with `spark.read.parquet(...)`, instead of calling `spark.createDataFrame(pandas_df)`
directly. `src/mdm/backends/spark.py` itself required no code change.
**What was found:** An initial parity run showed the Spark-scored and locally-scored
(pandas-loop) Fellegi-Sunter scores disagreeing on most pairs involving Vendor C (which has
no SSN field), with Spark's score consistently ~13.7 points lower — exactly the gap between
the `ssn` field's `missing` weight (+0.22) and `different` weight (-13.49) in
`config/fs_params.yml`. Debug prints traced this to the joined Spark row itself:
`'b_ssn': 'nan'` — a genuine, non-null, non-empty 4-character *string* `"nan"`, not a null.
`compare_ssn` correctly judged `"117326729" != "nan"` as `different`, since a literal string
`"nan"` is not covered by `is_missing()`'s NaN/None/empty-string checks (nor should it be —
treating any real string "nan" as missing would be a worse, more surprising rule than the bug
it works around). The corruption traced further back to `spark.createDataFrame(pandas_df)`:
pyspark 4.2.0 emits `FutureWarning: PySpark does not yet fully support pandas >= 3.0.0`, and
under pandas 3.0's new native string dtype, the missing-SSN sentinel gets `str()`-coerced to
the literal text `"nan"` during Spark's legacy (non-Arrow) fallback conversion path, rather
than being preserved as SQL NULL.
**Why this is a test-harness fix, not a backend fix:** The real Dataproc Serverless job never
calls `spark.createDataFrame(pandas_df)` — it reads `candidate_pairs`/`patient_normalized`
directly from BigQuery via the Spark BigQuery connector, which produces native Spark
DataFrames with correct null semantics from the start. The bug only existed in how the local
test constructed its **input** Spark DataFrame; `mapInPandas`'s own Arrow-based batch
boundary (`backends/spark.py`'s actual code path) was never implicated — re-running the
identical scoring logic against Parquet-sourced input produced 0/5,000 mismatches.
**How to apply:** Confirmed via `spark_local_test.py`: local pandas-loop and Spark
`mapInPandas` scores now match exactly on all 5,000 sampled dev-tier pairs. When testing
Spark code locally against pandas-seeded data on this pandas/pyspark version combination,
prefer writing to Parquet and reading it back over `spark.createDataFrame(pandas_df)` — it's
both closer to how the real job ingests data (from BigQuery, not from in-memory pandas) and
avoids this dtype-conversion trap entirely.

## Local dry-run Spark drivers write output via toPandas()/pandas, not Spark's own Parquet writer

**Phase:** 12
**Decision:** `spark_jobs/score_pairs.py` and `spark_jobs/cluster_identities.py`'s
`--local-parquet-dir` dry-run branch writes output with `df.toPandas().to_parquet(...)`
instead of `df.write.parquet(...)`. The real (BigQuery-writing) branch is unaffected.
**What was found:** Wiring up the actual submittable driver scripts (as opposed to the
scratchpad parity-test harness, which only ever used `.toPandas()` to compare results and
never wrote Spark output to disk) hit a new failure the moment it tried to *write* a Parquet
file locally: `RawLocalFileSystem.setPermission` → `Shell.getWinUtilsPath` →
`FileNotFoundException: HADOOP_HOME and hadoop.home.dir are unset`, thrown as a hard
`RuntimeException` inside the Hadoop output committer. This looked at first like a repeat of
the session's earlier `SparkSession` bootstrap issues (same exception text), but isolating it
with a series of shrinking repros showed `SparkSession.builder(...).getOrCreate()` alone
always succeeded -- the failure only appeared once a real `.write.parquet(...)` call ran.
Root cause: reading Parquet on Windows never touches Hadoop's local filesystem permission
code at all, but *writing* goes through Hadoop's `FileOutputCommitter`, which calls
`RawLocalFileSystem.setPermission()` to chmod the output directory -- and that call requires
`winutils.exe`, a Windows-only stub binary this project never installs (see the existing WARN
about missing `winutils.exe`, present but harmless in every prior Spark test in this phase
until something actually tried to write).
**Why this is a local-test-only fix:** The real Dataproc Serverless job runs Linux (no
winutils.exe concept exists there) and writes its actual output to BigQuery via the
spark-bigquery-connector, never to local disk via Hadoop's `FileOutputCommitter` -- so this
code path is never exercised in production regardless. Installing `winutils.exe` (a
third-party unsigned binary) purely to satisfy a Windows-only local dry-run path wasn't worth
the trade-off; routing local output through pandas' own (pyarrow-backed, Hadoop-free)
Parquet writer sidesteps the problem entirely while still exercising the real, shared
`score_candidate_pairs`/`build_clusters` logic end to end.
**How to apply:** Verified via `spark_jobs/score_pairs.py --local-parquet-dir`: writes 5,000
scored pairs successfully. If a future local Spark test needs to *write* files (not just
read), reach for `.toPandas().to_parquet(...)` rather than `.write.parquet(...)` on Windows
unless `HADOOP_HOME`/`winutils.exe` are set up.

## Real Dataproc Serverless submission: three real fixes before it ran, then exact parity

**Phase:** 12
**Decision:** Ship rapidfuzz as a pre-downloaded Linux wheel (`scripts/package_spark.py` /
`dist/rapidfuzz.zip`) rather than any Dataproc-side pip-install mechanism; grant the pipeline
service account `roles/bigquery.readSessionUser` in addition to its existing BigQuery roles;
stage all `--py-files`/`--files` inputs to GCS manually (`gsutil cp`) rather than passing
local paths to `gcloud dataproc batches submit`.
**What was found, in the order hit:**
1. `--py-files=dist/mdm.zip` (a local path) failed at driver startup with `Illegal character
   in path at index 43: gs://.../dependencies\mdm.zip` -- gcloud's Windows build joins its
   internal GCS staging path with a backslash, producing an invalid URI. Uploading the file
   directly with `gsutil cp` and passing the resulting `gs://` URI sidesteps gcloud's
   local-file staging path entirely.
2. `ModuleNotFoundError: No module named 'rapidfuzz'` -- the Dataproc Serverless base image
   doesn't include it (unlike PyYAML, which was already present and needed no extra step).
   Guessed two different `--properties` keys for a pip-install-at-startup mechanism
   (`spark.dataproc.driverEnv.PIP_PACKAGES`, then `dataproc.pip.packages`) -- both were
   silently accepted by the CLI and silently ignored by the runtime ("Ignoring non-Spark
   config property"), each guess costing a real (if small, since both failed within
   seconds) Dataproc bill. `dataproc:pip.packages` (colon, not dot) turned out to be a
   **persistent-cluster** property, not documented anywhere as working for serverless
   batches. Stopped guessing against a billed environment and instead downloaded rapidfuzz's
   actual Linux wheel locally (`pip download rapidfuzz==3.14.5 --platform
   manylinux_2_28_x86_64 --python-version 312 --abi cp312 --only-binary=:all:` -- Dataproc
   runtime 2.2 uses Python 3.12, and rapidfuzz 3.14.5 only ships a manylinux_2_27/2_28
   wheel, not the older manylinux2014/glibc-2.17 baseline pip defaults to expecting) and
   shipped the wheel file directly as a second `--py-files` entry -- a wheel's internal
   layout (package importable from its own zip root) is already exactly what `--py-files`
   wants from a `.zip`, no repackaging needed.
3. The job then read `conformance.patient_normalized` and `matching.candidate_pairs`, scored
   all pairs, and only failed at the very last step (`BigQueryWriteHelper.
   writeDataFrameToBigQuery`) with `PERMISSION_DENIED: ... 'bigquery.readsessions.create'`.
   The service account's existing `bigquery.dataEditor` + `bigquery.jobUser` roles cover
   table read/write and job execution, but not BigQuery Storage API read sessions, which the
   spark-bigquery-connector's indirect write path also needs for its read-back step.
   `roles/bigquery.readSessionUser` is the narrowest predefined role that grants exactly
   that permission.
**Real result, once all three were fixed:** `score-pairs-20260718-023905` succeeded in ~2
minutes, wrote 345,976 scored pairs to `matching.pair_scores` (the correct *distinct*
candidate-pair count -- `matching.candidate_pairs` itself has 398,507 raw rows because
multi-pass blocking can rediscover the same pair via more than one blocking key, and both
the local pipeline and this Spark job already deduplicate via `SELECT DISTINCT`/`.distinct()`
before scoring, exactly as they should). A full re-verification -- not a sample -- scored
all 345,976 pairs locally in pandas and diffed against every row BigQuery actually holds:
**0 mismatches, max absolute difference 0.0.** Approximate cost for the successful run:
~0.23 DCU-hours + ~23 GB-hours shuffle storage, on the order of $0.25; combined with the
earlier failed attempts (each failing within seconds, before real compute), total spend for
this phase's real Dataproc work was well under $1, drawn from the GCP free-trial credit.
**How to apply:** `make dataproc-score-pairs` reproduces the whole working flow end to end
(package → upload deps → submit). If a future package needs bundling the same way, check its
wheel's actual platform tag on PyPI first (`manylinux_2_17` a.k.a. manylinux2014 is not
guaranteed for newer package releases) rather than assuming the classic baseline tag.

## Airflow's GCP operators need an Airflow Connection, not just GOOGLE_APPLICATION_CREDENTIALS

**Phase:** 13
**Decision:** docker-compose.yml sets `AIRFLOW_CONN_GOOGLE_CLOUD_DEFAULT` (a JSON connection
definition with an empty extra plus a project id) alongside the existing
`GOOGLE_APPLICATION_CREDENTIALS` mount.
**What was found:** `GCSToBigQueryOperator`/`DataprocCreateBatchOperator` authenticate via an
Airflow *Connection* object (`gcp_conn_id`, defaulting to `google_cloud_default`) -- a
separate abstraction layer from the ADC file mounted at `/opt/airflow/gcloud/adc.json`. The
connection has to *exist* (`AirflowNotFoundException: The conn_id 'google_cloud_default'
isn't defined`) even though its own extras stay empty; an empty extra is what tells the hook
to fall back to ADC. Past that, the hook also needs a project set in the connection's extra,
or BigQuery load jobs fail with `ValueError: INTERNAL: No default project is specified`, even
though every operator call in the DAGs already passes an explicit project/table.
**How to apply:** Defining the connection via an `AIRFLOW_CONN_*` environment variable (not a
manual `airflow connections add` the user would have to remember to redo) keeps
`docker compose up` fully reproducible from a clean `.env`.

## dbt-core and Airflow have incompatible click pins -- dbt gets its own venv

**Phase:** 13
**Decision:** `airflow/Dockerfile` creates a second venv (`/home/airflow/dbt-venv`) and
installs `dbt-core`/`dbt-bigquery` into it, isolated from Airflow's own site-packages.
`conformance_dag`/`dedup_dag`'s dbt `BashOperator` tasks call the dbt-venv's `dbt` binary
directly rather than a bare `dbt` on PATH.
**What was found:** `dbt-core==1.12.0` requires `click>=8.3.0`; Airflow 2.10.4 requires
`click==8.1.7` (per its own published constraints file). These are genuinely incompatible in
one Python environment, not just an unpinned-version accident -- `pip install` fails with
`ResolutionImpossible` the moment both are requested together. Installing
`apache-airflow-providers-google` also requires pinning against Airflow's own constraints
file, or pip resolves an incompatible provider version (found the hard way: guessed
`10.26.0`, the constraints file wanted `11.0.0`) -- and once pinned to that file, `pandas`
also resolves to `2.1.4`, not this project's usual `3.0.3`, since pandas is itself a
transitive dependency of the Google provider. None of `mdm.*`'s BigQuery-path code needed
anything pandas-3.0-specific, so `airflow/requirements.txt` deliberately leaves
pandas/pyarrow/PyYAML unpinned rather than fighting Airflow's own constraint.

## spark-bigquery-connector's default (indirect) write method silently drops ARRAY column contents

**Phase:** 13
**Decision:** `spark_jobs/cluster_identities.py` writes to BigQuery with
`.option("writeMethod", "direct")` (the BigQuery Storage Write API) instead of the default
indirect method (stage as Parquet to GCS, then a BigQuery load job) that `score_pairs.py`
still uses.
**What was found:** The first real `dedup_dag` run produced a `matching.clusters` table
where every row's `size`/`scored_pairs`/`confidence`/`flagged` were correct but `members`
(an `ARRAY<STRING>`, built via `F.collect_list("record_key")` in
`mdm.backends.spark.build_clusters`) was `[]` on every single row -- confirmed via the raw
`bq` CLI, not just the Python client, so it wasn't a client-side `to_dataframe()` conversion
issue. Traced to the write, not the compute: a local Spark unit test
(`tests/unit/test_backends_spark.py`, which had checked `scored_pairs`/`confidence`/`flagged`
but never actually asserted on `members`'s contents -- a real gap in the existing test) was
extended to check members directly and confirmed `build_clusters()` produces correct arrays
entirely in-memory. Iterated on the write step directly on Dataproc (a tiny 8-row toy job,
~20 seconds and a fraction of a cent per attempt -- far faster than fighting local Windows
Spark with extra JARs, which hits the same class of winutils.exe/Ivy-resolution issues as
previous phases): the default `intermediateFormat=parquet` reproduced the empty-array bug;
`intermediateFormat=avro` isn't usable without also shipping the separate spark-avro package;
`writeMethod=direct` round-tripped the same array data correctly on the first try.
`score_pairs.py` doesn't need the same fix -- `matching.pair_scores` has no array/repeated
columns -- so it keeps the indirect method, better suited to its much larger
(hundreds-of-thousands-of-rows) output than the direct method would be.
**How to apply:** Re-verified end to end: `matching.clusters` now has 0 empty-array rows out
of 22,965, and `scripts/run_matching_bigquery.py` correctly merges 50,486 source records
into 25,013 golden records using this data. If a future job needs to write another
ARRAY/REPEATED column to BigQuery from Spark, default to `writeMethod=direct` rather than
assuming the indirect path preserves nested types.

## write_serving_tables isn't atomic -- an interrupted run can silently lose identity_events history

**Phase:** 13
**Decision:** No code change (see rationale below) -- documented as a known limitation.
`serving.*` was cleared and `scripts/run_matching_bigquery.py` re-run once, uninterrupted, to
recover a fully-consistent, fully-audited state after the incident described below.
**What was found:** `write_serving_tables` (both `mdm.pipeline`'s DuckDB version and
`mdm.backends.bigquery`'s counterpart) performs several independent sequential writes --
crosswalk, then identity_events, then demographics, field_lineage, alternate_ids,
membership, review_queue -- with no atomicity across them. Debugging the array-write bug
above involved firing two overlapping `airflow tasks test dedup_dag crosswalk_survivorship`
invocations against the same local Airflow LocalExecutor in quick succession; Airflow's own
scheduler detected the conflict and sent SIGTERM to the first, mid-write ("State of this
instance has been externally set to None. Terminating instance."). That run's
`crosswalk`/`member_demographics` writes had already completed (correctly reflecting the
real 50,486 to 25,013 merge) before the point where it was killed, but `identity_events` --
the permanent audit trail fixed to append-not-replace earlier this same phase -- never got
its corresponding MERGE/CREATE rows written. The next run found the crosswalk already fully
resolved and correctly logged zero *new* events, so the interrupted run's real merge history
was gone for good, silently, with no error anywhere pointing at the gap.
**Why no code fix (yet):** The trigger here was two overlapping *manual test invocations*
against the same ephemeral executor slot, not a failure mode a real, singly-triggered
Airflow DAG run would hit under normal operation. A proper fix (staging tables plus an
atomic swap, or accepting that identity_events can only be trusted when cross-checked
against crosswalk's own state) is a real, separate piece of engineering, not a quick patch,
and this project's threat model doesn't currently include arbitrary mid-write process
termination during normal orchestrated runs. Recording it here rather than silently shipping
around it, per P12.
**How to apply:** If `identity_events` and `crosswalk`/`member_demographics` are ever found
to disagree in row-count-implied history again, suspect an interrupted write before
suspecting the resolution logic itself -- diff crosswalk's distinct `patient_global_id`
count against identity_events' CREATE-minus-retired count for a cheap consistency check.

## agg_dedup_metrics.sql used DuckDB/Postgres-only syntax invalid on BigQuery -- uncaught until Phase 13's first real run

**Phase:** 13
**Decision:** `count(*) filter (where ...)` became `coalesce(sum(case when ... then 1 else 0
end), 0)`; `x::double` became a bare `x` (division is true division, never integer floor
division, in both DuckDB and BigQuery, so no cast was needed at all).
**A second regression this introduced, caught by `tests/integration/test_dashboard.py`:**
`sum(...)` over zero rows is SQL NULL, not 0 -- unlike `count(*) filter (where ...)`, which
returns 0 for an empty table. The first version of this fix (`sum(...)` with no `coalesce`)
passed the manual DuckDB/BigQuery checks below (both had real identity_events rows to sum)
but turned every one of `create_events`/`merge_events`/`split_events` into NULL the moment
`serving.identity_events` was empty, which then crashed the dashboard's
`int(metrics["create_events"])` on a NaN. `coalesce(..., 0)` restores the original
`count(*) filter`'s actual behavior (0 for nothing found) rather than just its BigQuery
compatibility.
**What was found:** `dedup_dag`'s `dbt_run_serving` task was the first time `models/serving`
was ever built against BigQuery -- `scripts/verify_tier_parity.py` (Phase 11) and every
`dbt-build-prod`/`make dbt-build-prod` invocation since deliberately exclude
`path:models/serving` (the two-phase dbt flow: those sources are tables
`run_matching`/`run_matching_bigquery` write, which don't exist on a fresh build). `FILTER
(WHERE ...)` is DuckDB/Postgres syntax with no BigQuery equivalent; BigQuery's parser
rejected it outright (`Syntax error: Expected ")" but got "("`). `::double` postfix casting
is likewise DuckDB/Postgres-only, not standard/BigQuery SQL (`CAST(x AS ...)` is), though it
never got the chance to fail since the FILTER error came first.
**How to apply:** Both fixes are dialect-neutral SQL rather than a `{% if target.type == %}`
branch -- simpler than the phonetic_key.sql-style dialect macros from Phase 3/11, and
preferable when a portable form already exists (P8's spirit: don't duplicate logic across
dialects when one expression already works on both). Verified on both targets: `dbt run
--select agg_dedup_metrics` against local DuckDB, and the real `dbt_run_serving` Airflow task
against BigQuery. Any `models/serving/*.sql` model is currently *only* exercised against
BigQuery via a real `dedup_dag` run -- there's no equivalent of `verify_tier_parity.py`
covering serving models yet, so a future dialect bug here would again go undetected until
the next real Airflow run.

## `bp_coarse`'s fixed-cardinality DOB key made block size grow with population, not stay bounded

**Phase:** 14
**Decision:** `bp_coarse` blocks on `dob_year`, not `dob_decade` (`dbt/models/blocking/block_keys.sql`, `config/matching.yml`).
**What was found:** At the 5M-record scale tier, `matching.candidate_pairs` came out to
647.1M rows, with 98% of them contributed by `bp_coarse` alone. Root cause: `dob_decade`
(`last_name_phonetic | gender | dob_decade`) has bounded cardinality -- roughly (distinct
phonetic last names) x 2 x ~12 decades -- so as population grows, the *same* number of
blocks has to hold *more* records each, and candidate pairs from a block grow with the
square of its size. `bp_year_names` (`dob_year | first_name_phonetic | last_name_phonetic`)
already proved this doesn't have to happen: at the same 5M scale its max observed block
size was 76, because adding a finer DOB key and a first-name key keeps block population
roughly constant as the record count grows, rather than concentrating into fewer, larger
buckets.
**Why this wasn't a recall/cost tradeoff:** `max_block_size: 1000` silently drops any block
over that size from candidate generation (P5's guard against exactly this kind of blowup).
`dob_decade` blocks were large enough that a meaningful number of them were hitting that cap
and getting excluded outright -- so the coarse key wasn't just expensive, it was actively
*dropping* true pairs it should have caught. Switching to `dob_year` cut candidate pairs
647.1M -> 337.3M (48%) at scale, while at dev tier recall improved 0.5329 -> 0.5338 and
blocking pair-completeness improved 0.9315 -> 0.9555 -- confirmed via
`python -m mdm.evaluate --tier dev` before and after. Post-fix scale-tier `block_stats`:
`bp_coarse` max block size 697 (184,721 blocks, avg 27.3), contributing 323.9M of the
337.3M total candidate pairs (97.4% -- still the dominant pass, just no longer a runaway
one).
**How to apply:** A blocking key's cardinality has to be checked against how it scales with
population, not just how it performs at whatever tier is currently being tested -- a key
that looks fine at 50K records (few enough people share `last_name x gender x decade` that
blocks stay small) can silently become the dominant cost driver at 5M just because the
*same* key space now has to absorb 100x the population. Prefer keys whose cardinality grows
with the data (a wider or finer field) over ones with a small fixed range.

## BigQuery's `maximum_bytes_billed` cap needs to scale with the tier's candidate-pair volume

**Phase:** 14
**Decision:** `dbt/profiles.yml`'s `prod` target raised `maximum_bytes_billed` from 20 GiB to
50 GiB (`dbt/profiles.yml.example` carries the same value with the rationale inline).
**What was found:** `assert_candidate_pairs_ordered_no_self_pairs` (a structural dbt test
that scans all of `matching.candidate_pairs`) failed with the query cancelled once
`candidate_pairs` reached 647M rows at the scale tier, needing to scan ~22.9 GiB against a
20 GiB cap set back in Phase 11 when this table was orders of magnitude smaller.
**Why 50 GiB and not just disabling the cap:** The cap exists to catch a genuinely
runaway/mistaken query (e.g. an accidental full cross join), not to throttle legitimate
scale-tier structural tests -- removing it entirely would lose that guard rail. The real
dollar cost of the query that hit the cap is trivial on BigQuery's on-demand pricing
(~$0.15), so the fix is sizing the cap to what scale-tier `dbt build` actually needs
(with headroom), not raising it indefinitely.
**How to apply:** Any safety cap tied to a tier-scaled table's full-scan cost needs to be
re-checked (not just re-guessed) whenever a new tier's data volume is exercised for the
first time -- the 647.3M-pair `candidate_pairs` table this cap now accommodates is itself
downstream of the `bp_coarse` fix above, and shrank to 337.3M rows once that landed.

## Dataproc Serverless autoscaling is quota-capped, and default shuffle partitioning doesn't know it

**Phase:** 14
**Decision:** `spark_jobs/cluster_identities.py` takes `--shuffle-partitions` (default 32) and
`--max-executors` (default 7), applied via `spark.sql.shuffle.partitions` and
`spark.dynamicAllocation.maxExecutors`. `Makefile`'s `dataproc-cluster-identities` target and
`airflow/dags/dedup_dag.py`'s `cluster_identities` task both pass `--checkpoint-dir` (missing
from both before this phase) alongside the new flags.
**What was found:** The first real clustering attempt at scale ran for 76+ minutes without
completing -- dramatically slower than the 327M-pair *scoring* job, which finished in 34
minutes on the same project. `gcloud logging read` against
`resource.type="cloud_dataproc_batch"` showed the autoscaler requesting up to 492 executors
(`"max-needed-executors": "492"`) but hard-capped at 7 primary workers by the project's
`CPUS_ALL_REGIONS` compute quota (`"Insufficient 'CPUS_ALL_REGIONS' quota. Requested 4.0,
available 0.0."`, `"constraintsReached": ["SCALING_CAPPED_DUE_TO_LACK_OF_QUOTA"]`). Combined
with `connected_components`'s default 200 shuffle partitions (Spark's cluster-sized default,
vastly oversubscribed against the real ~28 available cores at 7 workers x 4 cores), every one
of the algorithm's iterations scheduled roughly 7 sequential waves of mostly-idle executors
instead of 1 -- and because `connected_components` shuffles *every iteration*, this overhead
compounded across the whole run instead of being a one-time cost the way it would be for a
single-pass job like scoring.
**The real cost of getting this wrong:** Cancelling that first attempt still cost
approximately $45 (38.45 DCU-hours + 3,894 GB-hours) for a job that never converged --
*more* than the entire successful 337M-pair scoring run (~$16.60). The lesson generalizes:
Dataproc Serverless bills `shuffleStorageGbSeconds`, which accumulates with wall-clock time,
not just data volume -- an iterative algorithm that's *slow* because of a parallelism
mismatch can cost more than a one-shot job over *more* data that runs efficiently. Small
data does not imply small cost if the job runs long enough. A second attempt was submitted
without re-uploading the locally-edited `cluster_identities.py` to GCS first, and failed
immediately (~$0.26) on `unrecognized arguments: --shuffle-partitions --max-executors` --
Dataproc ran the stale script already on GCS, not the local one; the GCS copy is the only one
that matters once a job is submitted via `gs://`.
**How to apply:** `--shuffle-partitions 32` (close to, slightly above, the real 28-core
ceiling) and `--max-executors 7` (matching the observed quota ceiling exactly, so the
autoscaler stops repeatedly requesting executors quota will never grant) together brought
the real, correctly-configured run in at [FILL IN: runtime/cost once the corrected run
completes]. Before trusting a Dataproc Serverless job's parallelism settings, check the
project's actual `CPUS_ALL_REGIONS` quota (`gcloud compute regions describe <region>
--project <project>`) rather than assuming Spark's dynamic allocation will get whatever it
asks for -- and always re-run the upload step after editing a script that's submitted from
a `gs://` path, since gcloud submits whatever is already staged, silently.

## The same FS score means something different at 5M records than at 50K -- thresholds don't transfer across tiers

**Phase:** 14
**Decision:** `config/matching.yml`'s `thresholds` (now `upper: 9.0413`, `lower: 9.0413`) is
dev/ci-tier only. The scale tier gets its own, separately-measured threshold, passed
explicitly to `spark_jobs/cluster_identities.py --upper-threshold` (currently `20.5`) from
`Makefile` and `airflow/dags/dedup_dag.py` -- not read from `matching.yml`.

> **Superseded 2026-08-23 as to *where* the value lives** (the finding itself stands): the
> scale threshold is now `thresholds.scale.upper` in `config/matching.yml`, read by the
> Makefile and the DAG rather than hardcoded in each. Keeping it outside config is what let
> the review queue keep reading dev's band at scale -- see the entry below.
**What was found:** Before submitting the (re-tuned) clustering job, the already-computed
`matching.pair_scores` (327,366,916 rows, scale tier) were checked directly against real
scale-tier ground truth (`data/scale/ground_truth`, joined into BigQuery as a scratch table)
rather than assuming the dev-tier-derived threshold still applied. At `matching.yml`'s
threshold (then 7.8924, now known stale for an unrelated reason -- see below), scale-tier
precision measured only **49.4%**, against dev tier's own measured >=99% at its equivalent
cutoff. Sweeping thresholds directly against the real stored scores found the actual
scale-tier F1-optimal cutoff around **20.5** (precision 0.965, recall 0.942, F1 0.954) --
still meaningfully below dev tier's F1 of 0.9967 at its own optimum, and nowhere near the
dev-tier-derived value.
**Why the same score means something different at different scale:** `bp_coarse` doesn't
require first-name agreement (by design, to catch first-name typos/nicknames), so within a
`bp_coarse` block, candidate pairs can be two genuinely different people who happen to share
`last_name_phonetic + gender + dob_year`. The FS score for such a pair depends on whether
their *other* fields (first name, SSN) also happen to look similar -- and the absolute count
of coincidental "different person, but several fields agree" pairs grows much faster than
population (same mechanism as the `bp_coarse` block-size blowup above: a roughly
fixed-cardinality key absorbing more and more of a growing population) while the count of
*true* duplicate pairs grows only linearly with the corruption rate. The practical effect:
at any fixed score threshold, the base rate of true-vs-coincidental matches at that score
level shifts as population grows, so a cutoff calibrated on a 50K-record sample
systematically under-estimates how high the cutoff needs to be at 5M.
**A second, independent bug found along the way:** `config/matching.yml`'s committed
threshold (7.8924) turned out to already be stale before any of the above -- it was
calibrated against the fs_params.yml committed in Phase 6, but `config/fs_params.yml` had
since been regenerated twice this phase (`scripts/estimate_fs_params.py`, needed once to
refresh estimates and again after a stale local DuckDB schema forced a full dev-tier
rebuild), and nobody re-ran the sweep and copied the new number back into `matching.yml` --
exactly the gap its own comment warns about ("re-run the sweep... if fs_params.yml...
changes"). Re-running `python -m mdm.evaluate --tier dev` against the current
`fs_params.yml` gives `9.0413`, confirmed self-consistent by re-running `run_matching` and
`run_quality_checks` for dev tier against it (all 5 quality checks still pass).
**How to apply:** Never assume a threshold tuned at one tier is valid at another -- verify
directly against that tier's own ground truth before trusting it for anything that costs
money to compute against (this was checked before submitting the corrected clustering job,
not after). More generally: any threshold or cutoff derived from a `threshold_sweep`-style
process needs to be re-derived whenever its upstream inputs (here, `fs_params.yml`) change,
not copied once and assumed stable.

## `run_matching_bigquery.py`'s in-memory crosswalk/survivorship doesn't fit in memory at 5M records

**Superseded:** later this same phase, the decision below was revisited and the batching fix
described in "Batching `run_matching_bigquery.py` by patient_global_id..." was implemented and
verified against the real 5M-record scale tier. Left here as the original record of the
decision at the time it was made (P12: alternatives and reasoning matter, not just the final
state).

**Phase:** 14
**Decision:** No code change -- documented as a known limitation of the current
record-count-scaled local processing design. `scripts/run_matching_bigquery.py` remains
verified at the ~50K-record scale it was built and tested against (Phase 13); it was not
completed against the full 5M-record scale tier this phase.
**What was found:** After `spark_jobs/cluster_identities.py` (Dataproc-scale, and the one
step of this pipeline that's genuinely pair-count-scaled) succeeded, running
`scripts/run_matching_bigquery.py --project patient-dedup-mdm` against the resulting
5,048,389-record / 2,244,989-cluster scale-tier data was killed after its single Python
process grew past 11.2GB resident and was still climbing, on a machine with 24GB total RAM
and only ~6.5GB free *before* the run even started (other processes already held the rest).
Free physical memory bottomed out around 240MB and free virtual (pagefile) memory around
800MB before the process was stopped -- close enough to real exhaustion that continuing
risked an uncontrolled `MemoryError` mid-run rather than a clean stop. The kill happened
during the read/compute phase, before `write_serving_tables` issued any BigQuery writes, so
no partial-write risk (see the write-atomicity limitation from Phase 13) was involved here.
**Why this is a different problem from `bp_coarse` or the Dataproc quota ceiling above:**
Those were about *pair-count-scaled* data (candidate pairs, shuffle partitions) outgrowing
its handling at 5M records. This is about *record/cluster-count-scaled* data -- supposedly
the cheap side of this pipeline, per this script's own docstring ("only record-count-sized
data ... is handled here, so plain Python is enough") -- turning out not to be cheap either,
once record count and cluster count are both in the millions: `records_by_key` (a
dict-of-dicts built via `pandas.DataFrame.to_dict(orient="index")`, one of pandas' more
memory-hungry conversions), `golden_records`, `field_lineage_rows`, `alternate_ids`, and
`membership_rows` are all plain Python lists/dicts sized to the full record and cluster
count at once, with no batching -- something Phase 13's ~50K-record verification never
exercised at a scale where per-object Python overhead (a dict costs far more than its raw
field bytes) would matter.
**Why not just fix it now:** A correct fix -- restructuring `resolve_crosswalk`/
`build_serving_tables`/`write_serving_tables` to process and write in bounded-size batches
instead of materializing the full result set in memory -- is a real architectural change to
code shared with the local DuckDB path (`mdm.pipeline.run_matching`, per
PROJECT_CONSTITUTION.md's "one codebase, two backends" principle, #8), not a quick patch,
and warrants its own design/testing pass rather than a rushed change at the end of a long
session.
**How to apply:** Before running this script against a real multi-million-record scale tier,
either provision a machine with meaningfully more headroom than 24GB total (this run needed
more than the ~17.5GB that was actually available), or do the batching rewrite first. Either
way, verify free memory *before* starting, not just after something goes wrong -- this run's
6.5GB of pre-existing headroom was already a warning sign the process's peak footprint didn't
respect.

## Batching `run_matching_bigquery.py` by patient_global_id fixes the 5M-record memory ceiling

**Phase:** 14
**Decision:** `scripts/run_matching_bigquery.py` groups `new_crosswalk` by `patient_global_id`
and processes it in batches (`--batch-size`, default 100,000 pgids), slicing
`patient_normalized_df` per batch instead of converting the whole table to a dict-of-dicts
up front. `resolve_crosswalk` and `build_serving_tables` are both unchanged -- only the
orchestration around them is new. `mdm.backends.bigquery.write_serving_tables` is split into
`write_crosswalk_and_events` (crosswalk/identity_events/review_queue, still written once --
none of them scale with golden-record count) and `write_serving_batch`
(member_demographics/field_lineage/member_alternate_identifier/membership, WRITE_TRUNCATE on
the first batch, WRITE_APPEND after, reproducing the old single-write "replace wholesale"
semantics without ever holding all golden records in memory at once).
**Why `resolve_crosswalk` isn't batched too:** it needs every cluster's full membership in a
single pass to correctly detect splits across the whole run (a cluster processed early can
claim an id a later cluster used to share -- see `mdm.crosswalk`'s module docstring) --
batching it would mean carrying `claimed_ids` state across batch boundaries, real added risk
to already-correct, already-tested logic. It didn't need to: `clusters`
(record_key -> membership tuple) is far lighter than per-record demographic data, so it was
never what pushed memory past 11GB in the first place -- see the superseded decision above.
**Verified:** re-run against the real 5,048,389-record scale tier with memory sampled every
25-180s throughout. Peak resident memory stabilized around 6.0-6.4GB (vs. 11.2GB+ and still
climbing before the fix), on the same machine, comfortably within the available headroom.
Correctness verified two ways: a new unit test (`test_batching_is_invariant_to_batch_size`)
confirms batch_size=1 and batch_size=100,000 produce identical aggregate output on the same
input, and the real run's summary (2,830,681 auto-match edges, 2,244,989 clusters, 25,447
flagged, 2,559,287 golden records) is internally consistent: 2,244,989 clusters minus 25,447
flagged gives 2,219,542 golden records from clean clusters; adding 216,486 untouched
singletons and 123,259 individual golden records from flagged clusters' members (each
flagged cluster's members become singletons, per `finalize_cluster_membership` -- P13's
"when uncertain, do not merge") totals 2,559,287, exactly matching.
**How to apply:** `--batch-size` defaults to 100,000; lower it if memory is still tight on a
given machine, raise it for fewer, larger BigQuery load jobs when memory isn't the
constraint (e.g. Phase 13's ~50K-record scale needs none of this batching at all -- one
batch covers everything).

## A stale, unrelated dev-tier crosswalk silently corrupted 0.73% of the scale-tier run

**Phase:** 14
**Decision:** All `serving.*` tables written by `run_matching_bigquery.py` (`crosswalk`,
`identity_events`, `member_demographics`, `field_lineage`, `member_alternate_identifier`,
`membership`, `review_queue`) were dropped before the scale-tier run, rather than letting
`resolve_crosswalk` read whatever `serving.crosswalk` already contained.
**What was found:** The first real scale-tier `run_matching_bigquery.py` attempt (the one
that also hit the `identity_events` schema error below) had already written a new
`serving.crosswalk` before crashing. Checking it: 36,740 of 5,048,389 rows (0.73%) had
`first_seen_run != last_seen_run` -- meaning `resolve_crosswalk` treated them as *continuing*
an identity from an earlier run, despite this being the very first crosswalk resolution ever
run against the scale-tier dataset. Root cause: `scripts/generate.py` builds every
`record_key` as `f"{vendor}:{identity_index:08d}"`, with `identity_index` starting at 0
regardless of tier -- so the dev tier's 24,000 identities and the scale tier's first 24,000
(of 2,400,000) identities produce *identical record_key strings* for entirely different
synthetic people. `serving.crosswalk`/`serving.identity_events` still held Phase 13's
25,013-row dev-tier result (in `serving.identity_events`, confirmed: 25,013 rows, 0 with a
non-null `retired_id` -- all `CREATE` events, matching Phase 13's own dev-tier golden-record
count exactly) from when that dbt/Airflow work was verified against BigQuery. The local
DuckDB path never has this problem -- each tier gets its own `.duckdb` file, so there's no
shared state to collide across tiers. BigQuery is one project shared by every tier that
targets it, and nothing had ever cleared its serving layer between a dev-tier verification
run and a scale-tier real run.
**A second, independent bug the same stale table caused:** `serving.identity_events`' schema
had `retired_id` typed `INTEGER`, not `STRING` -- inferred from Phase 13's dev-tier write,
where every event happened to be a `CREATE` (`retired_id` always `None`, giving BigQuery
nothing to type-infer from except a default). The scale-tier run's real `MERGE`/`SPLIT`
events have actual string `retired_id` values, and the `WRITE_APPEND` load job failed
outright (`Parquet column 'retired_id' has type BYTE_ARRAY which does not match the target
cpp_type INT64`) rather than silently corrupting anything -- the loudest possible failure
mode for what could otherwise have been a much quieter bug.
**How to apply:** Dropped and let all seven tables recreate fresh on the next run --
verified clean afterward (`COUNTIF(first_seen_run != last_seen_run) = 0` across all
5,048,389 rows, `COUNT(DISTINCT last_seen_run) = 1`). Any BigQuery-backed pipeline step that
reads its own prior output as "existing state" (here, `read_existing_crosswalk`) needs that
state cleared -- not just the source tables reloaded -- whenever the underlying dataset it's
tracking changes identity, not just size. A tier switch is exactly that kind of change even
though it looks, superficially, like "the same tables, more rows."

## Domain linking classified into three cases (Path A / Path B / match-path), not one shared strategy

**Phase:** 17
**Decision:** Every (vendor, domain) pair in the expanded 6-domain model gets classified into
exactly one of three linking strategies before any code is written: **Path A** (the domain
carries the same ID as that vendor's own eligibility record -- a straight join through the
crosswalk), **Path B** (a *different* ID from the same vendor -- e.g. a separate PBM
relationship -- needing one extra hop through a per-vendor `vendor_id_map` first), or
**match-path** (no shared ID at all -- needs the real comparator/blocking/Fellegi-Sunter
pipeline, not a join). `docs/domain-linking-strategy.md` is the full matrix. `VENDOR_D` (a
lab) was added specifically to exercise match-path a second, independently-motivated way --
unlike `VENDOR_B`'s `pharmacy_info` (a benefit file, member-level, still tied to an insurer
relationship), a lab has no eligibility relationship to any member at all, so match-path
isn't validated against only one scenario shape.
**Alternatives considered:** One generic "linking adapter" interface all six domains
implement, dispatching internally; treating every domain as match-path for uniformity (skip
classifying, just match everything).
**Why this:** A generic adapter would hide the fact that these are three structurally
different problems with different costs -- Path A is free (a join), Path B is nearly free
(one more join), match-path is the expensive, error-prone one (real matching, real
precision/recall to measure and defend). Naming the distinction up front means each domain
gets exactly the amount of engineering its actual linking problem requires, and a reviewer
can see at a glance which domains were "hard" and why, rather than three joins and two
matching passes all looking like the same kind of code.

## HCPCS Level II over CPT for procedure codes

**Phase:** 18
**Decision:** Procedure codes use HCPCS Level II (CMS, free to use), not CPT.
**Alternatives considered:** CPT (the standard most real claims data actually carries in
practice).
**Why this:** CPT is AMA copyrighted and licensed -- reproducing real CPT codes in a public
portfolio repo with no license is a real risk, not a hypothetical one, for something built
purely to be shown to recruiters. HCPCS Level II is public-domain, CMS-published, and covers
the same kind of ground (procedures, supplies, services) closely enough that the matching
methodology being demonstrated doesn't depend on which code set it is. When the two
considerations are "slightly less industry-standard" vs. "copyright exposure for zero
benefit to the thing actually being demonstrated," there's no real tradeoff.

## Match-path ground truth in its own table, and asymmetric blocking as raw SQL, not new dbt models

**Phase:** 20
**Decision:** Two related choices for `pharmacy_info`/`lab_results` (no shared ID with
anything): (1) their ground truth goes into a separate `matchpath_ground_truth` table,
never unioned into the core `ground_truth.ground_truth` table; (2) the asymmetric blocking
that finds match-path candidates against `conformance.patient_normalized` is written
directly as SQL inside `scripts/run_matchpath_matching.py` (`mdm.pipeline`'s
`_MATCHPATH_BLOCKING_PASSES`), not as new `dbt` models the way every other blocking pass in
this project is.
**Alternatives considered:** One combined ground-truth table with a `domain` column
distinguishing core from match-path pairs; a proper `matching.matchpath_block_keys` /
`matchpath_candidate_pairs` dbt model pair, mirroring `block_keys.sql`/`candidate_pairs.sql`
exactly.
**Why this:** A combined ground-truth table would let `mdm.evaluate`'s existing pair-based
precision/recall logic silently treat a core record and a match-path record as a possible
"true pair" -- they're never actual blocking/scoring candidates against each other in the
real pipeline, so any such pair could only be a spurious label corrupting the
already-verified Phase 0-15 metrics, for a check that would never once reflect anything the
system actually does. Separate tables make that class of bug structurally impossible instead
of relying on careful query-writing to avoid it. On the blocking side: this join is
asymmetric (match-path row vs. core population) and runs once, after `run_matching` has
already built the crosswalk it resolves against -- it doesn't fit dbt's regular symmetric
self-join build graph the way `candidate_pairs.sql` does, and forcing it into that shape for
consistency's own sake would be more machinery than the one-off asymmetric lookup needs.

## Match-path generation initially shared the member domain's own `Faker` instance -- same bug class as Phase 19's shared-`random.Random` bug, different object

**Phase:** 20
**What was found:** `src/mdm/generator/matchpath.py`'s `generate_pharmacy_info_appearance`/
`generate_lab_identity_appearance` call `faker.address()`/`faker.phone_number()`. Passed the
*member domain's own* `Faker` instance (the same one `synthesize_identity` uses for names),
this silently advanced that instance's internal RNG state every time match-path generation
ran for an identity -- even though `matchpath_rng` (an independently-seeded
`random.Random`) was already separate, exactly the fix Phase 19 had already applied for its
own shared-state bug. Found by generating the same ci-tier shard with and without match-path
generation enabled and diffing: member-domain and Phase 19 fact-domain record counts
differed (5,043 records without, 5,040 with) even though neither domain's own logic had
changed.
**How to apply:** Gave match-path generation its own `Faker` instance
(`matchpath_faker = Faker(); matchpath_faker.seed_instance(shard_seed + 2_000_000)`),
matching `matchpath_rng`'s seed offset. Re-verified byte-identical member-domain and Phase 19
fact-domain output with match-path generation on vs. off. The general lesson from both
bugs now: *any* stateful generator object (not just `random.Random`) shared across
independently-designed generation paths is a determinism hazard -- each new generation
concern needs its own fully independent instance of every stateful object it touches, not
just the ones that were the problem last time.
**A related gap found afterward, not a generation bug:** once `serving.matchpath_resolution`
existed, the full `dbt build` (which `fct_pharmacy_info.sql`/`fct_lab_results.sql` are part
of) started depending on `run_matchpath_matching` having already run -- but `make pipeline`
and three pre-existing quality-gate/snapshot tests only ran `run_matching` before a full
`dbt build`, since nothing in `models/serving` had depended on match-path output before this
phase. Added a `match-path` Makefile target between `match` and `dbt-build`, and updated the
three tests to call `run_matchpath_matching` in the same place the real pipeline now does --
a reminder that adding a new consumer of an existing artifact can silently change what
"finished" means for every caller upstream of it, not just the one being written.

## `member_360`'s cross-domain summary assumed one `fct_pharmacy_info` row per person -- untrue at dev-tier scale

**Phase:** 21
**What was found:** `member_360.sql`'s `pharmacy_info_summary` CTE selected straight from
`fct_pharmacy_info` with no `GROUP BY`, on the reasoning "one source record maps to at most
one identity at generation time." True, but not the same claim as "at most one
`fct_pharmacy_info` row resolves to the same `patient_global_id`" -- two *different* source
records can each independently auto-match to the same person, a real precision limit of
probabilistic matching (two similar-enough people, or a core cluster that split into more
than one golden record with each half separately attracting a match), not a bug in the
matcher itself. Passed cleanly at `ci` tier's small population; failed dev tier's
`unique_member_360_patient_global_id` dbt test with 17 real duplicate `patient_global_id`s.
**How to apply:** Grouped `pharmacy_info_summary` by `patient_global_id` like every other
domain CTE in the view, and added a `pharmacy_info_match_count` column so the collision is
visible in the data instead of silently resolved away -- the same instinct behind
`serving.review_queue` existing at all: real uncertainty in probabilistic matching gets
recorded, not hidden behind a `LIMIT 1`. General lesson: an assumption phrased as "true at
the source" doesn't automatically survive a join through a matching step with imperfect
precision -- it has to be re-verified downstream, not inherited.
**A second, unrelated finding the same debugging session turned up:** DuckDB has no
`+(DATE, BIGINT)` overload (`fill_date + days_supply`, where `days_supply` is `int64` in the
pyarrow schema) -- only `+(DATE, INTEGER)`. Needed an explicit `cast(days_supply as integer)`
before the addition. A small, easy-to-hit cross-type gotcha worth remembering anywhere else
this project does date-plus-integer arithmetic against a `BIGINT`-typed column.

## The Member 360 API writes new identities to the serving layer only -- a speed-layer overlay, not an ingestion path

**Phase:** 22
**Decision:** `POST /resolve`'s "no match found" path mints a new `patient_global_id` and
writes it directly to `serving.crosswalk`/`member_demographics`/`membership`/
`member_alternate_identifier` -- never to `raw_standard`/`conformance`. The new identity is
visible immediately (`member_360` is a live view), but isn't durable past the next full
batch `run_matching()`, which rebuilds `serving.crosswalk` from
`conformance.patient_normalized`'s record keys only -- an API-minted `record_key` was never
added to a vendor feed, so it has no representation there and isn't carried forward.
**Alternatives considered:** Writing the new record into `raw_standard` (as if from a
synthetic "API vendor") and triggering an incremental `dbt run` + `run_matching` so the
identity becomes fully durable and re-discoverable by future resolve calls immediately.
**Why this:** Making an API call synchronously trigger a `dbt build` is the wrong shape for a
request/response API regardless of how fast the build happens to be at demo-tier volumes --
it doesn't survive to real ingestion volumes, and it conflates two different
consistency guarantees (a live-view read that reflects this call sits differently than a
resolve that requires a build to complete first). This is a deliberate, documented
speed-layer/batch-layer split, made explicit in a test
(`test_resolving_the_same_new_person_twice_mints_two_ids`) rather than left as a surprise for
whoever notices it first.
**A real bug found by manually curling a running server, not by the automated tests:**
`member_360`'s `alternate_identifiers` array reconstructs
`source_vendor || ':' || source_record_id`. The new-identity write path passed the
*already-prefixed* `record_key` (`f"API:{uuid4().hex}"`) as `source_record_id`, rendering as
`"API:API:9bc943fa..."` in a live response. No automated test asserted on
`alternate_identifiers`' exact string content, only its presence -- only reading real JSON
off a real running server caught it. Fixed by separating `source_record_id` (the bare uuid)
from `record_key` (built *from* it), and treating this as a standing reminder that shape-only
assertions ("the field exists," "the list is non-empty") don't substitute for reading actual
rendered output at least once per feature.

## Streamlit's `AppTest` element references go stale across a `.run()` rerun

**Phase:** 23
**What was found:** `AppTest.tabs[i].set_value(x).run()` reruns the *entire* script from
scratch. Continuing to read `.subheader`/`.selectbox`/`.exception` off the `tab` object
captured *before* that `.run()` call silently returns the previous render's tree -- it
doesn't raise, and a weak assertion (`assert not tab.exception`, checking only for absence)
passes vacuously against a stale, empty-looking snapshot just as easily as against a correct
one. The pre-existing Phase 9 dashboard test only ever made assertions of that weak shape,
so it never surfaced this; a new Phase 23 test asserting on specific rendered content
(exact `subheader` labels, `selectbox` options) failed in a way that traced back to reading
the wrong tree, not a real application bug.
**How to apply:** Re-fetch the tab (`_golden_records_tab(at)`) from `at.tabs` after every
`.run()` before asserting on anything rendered, not just on `at.exception`/`tab.exception`.
Worth remembering for any future `AppTest`-based test that checks more than "did it crash."

## Match-path at the scale tier: reuse score_pairs.py unmodified via a union table, not a new Spark job

**Phase:** 20 (extended to BigQuery after the fact, costed out and built on request)
**Decision:** Match-path scoring at scale reuses `spark_jobs/score_pairs.py` completely
unmodified -- pointed at new tables via its existing `--candidate-pairs-table`/
`--patient-normalized-table`/`--output-table` flags, rather than writing a second Spark job.
The trick: `mdm.backends.spark.join_pairs_with_records` joins *both* sides of a candidate
pair against one broadcast `patient_normalized`-shaped table, so a match-path candidate pair
(`record_key_a` = match-path, `record_key_b` = core) needs a table containing *both*
populations. `conformance/patient_normalized_with_matchpath.sql` (a new dbt model, a plain
`UNION ALL` of `patient_normalized` + the two match-path domains, minimal columns) is that
table. Blocking is likewise new dbt models
(`matchpath_block_keys.sql`/`matchpath_block_stats.sql`/`matchpath_candidate_pairs.sql`,
in `models/blocking/` alongside the core ones) rather than the raw-SQL-in-Python approach
the local/DuckDB tier uses (`mdm.pipeline._MATCHPATH_BLOCKING_PASSES`) -- at scale, blocking
naturally happens through the regular dbt build graph the same way core blocking already
does; only *resolution* (after scoring) needs a standalone script, since it depends on
`serving.crosswalk` already existing.
**Alternatives considered:** A second Spark job specific to match-path scoring; keeping
blocking as an ad-hoc Python/BigQuery-client script at scale too, for symmetry with the
local tier.
**Why this:** `score_candidate_pairs`/`join_pairs_with_records` don't know or care what
population a `record_key` belongs to -- they just need it findable in the broadcast table.
Writing a second, nearly-identical Spark job to score a different table shape would
duplicate real logic (comparators, Jaro-Winkler, Fellegi-Sunter weights) purely because the
*inputs* differ, which is exactly the kind of two-implementations drift
PROJECT_CONSTITUTION.md #8 exists to prevent. The local-tier vs. scale-tier blocking
asymmetry (raw SQL vs. dbt models) is deliberate, not an inconsistency: the *shape* of
orchestration is allowed to differ by backend when the backend's own cost/build-graph model
calls for it (block_keys.sql/candidate_pairs.sql already establish that dbt is where
blocking lives at scale); the blocking *keys and passes themselves* are identical everywhere
either way.
**A real memory lesson applied proactively, not rediscovered:** the naive way to resolve
match-path matches -- download all of `matching.matchpath_pair_scores` client-side and pick
the max score per match-path record in pandas -- is exactly Phase 14's `read_pair_scores`
memory problem (pair-count-scaled data, hundreds of millions of rows at full scale) applied
to a new table. `mdm.backends.bigquery._read_best_matchpath_candidate` does the
`ROW_NUMBER() OVER (PARTITION BY record_key_a ORDER BY score DESC) = 1` aggregation
server-side instead, so the downloaded result is bounded by the number of *distinct
match-path records*, never the pair count. Verified for real against a live 10%-scale
sample (30.6M scored candidate pairs): the query returned exactly one row per distinct
`record_key_a` (234,377 candidates for 234,377 distinct records), confirming the
aggregation logic before it was ever exercised against the full pair count.
**Also verified live**, not just compiled: `dbt compile --target prod` plus real `bq query`
dry-runs and executions against a 10%-scale sample joined against the actual, full
5,048,389-row `conformance.patient_normalized` -- the new dbt-model-based
`matchpath_candidate_pairs` produced byte-identical candidate-pair counts per blocking pass
(170,741 / 651,153 / 29,823,680) to an independently hand-written equivalent query run
earlier during cost estimation, a real cross-check rather than trusting one code path.
`spark_jobs/score_pairs.py` itself could not be exercised end-to-end locally against real
data -- this machine hits the same pre-existing Windows/PySpark "Python worker" launch
failure documented since Phase 12 (`tests/unit/test_backends_spark.py`), unrelated to this
work -- so its compatibility rests on the schema/argument-contract check above (verified)
plus its already-existing test coverage for the unchanged scoring logic itself, not a fresh
end-to-end local run.

## `synthesize_identity`'s DOB silently depended on `datetime.now()` since Phase 1 -- found only when two real-world days separated two regenerations

**Phase:** 1 (bug present since), found and fixed while preparing to run the Phase 20
match-path scale test for real
**What was found:** `src/mdm/generator/identity.py` called
`faker.date_of_birth(minimum_age=1, maximum_age=95)`. Faker computes that internally as
`datetime.now().date()` minus a random age -- meaning, despite every other input
(`identity_index`, seed, tier) being identical, the exact calendar date it returns silently
depends on *which real day the generator happened to run on*. Invisible for the entire
project's life so far because no test or workflow had ever regenerated the same tier twice
with enough real elapsed time between the two runs to see it move. Found by comparing a
fresh regeneration against the already-on-disk dev-tier data while costing out the
Phase 20 scale test: the *same* identity (`ID00000000`, an "exact" appearance with zero
noise applied) had DOB `1933-12-13` in data generated two days earlier vs. `1933-12-14`
fresh -- a one-day drift matching the real time elapsed between the two runs almost
exactly, not random noise (which never touches an exact appearance) and not a code change
(confirmed by `git stash`-ing back to the exact committed version and reproducing the same
drift with zero diff against `HEAD`).
**Why this matters more than a cosmetic date shift:** the 5,048,389-row core population
already loaded in BigQuery (Phase 14, generated July 18) has DOB values baked in relative
to *that* day. Match-path matching's DOB comparator does exact/transposed/year-only
comparison -- generating fresh match-path data today against that population would have
scored genuinely-matching pairs as DOB mismatches purely from clock drift, artificially
depressing recall on a test that would look like it was measuring match-path quality but
was actually measuring how many days had passed since Phase 14's original run.
**How to apply:** Added `DOB_REFERENCE_DATE = date(2026, 1, 1)` (matching
`facts.py`/`matchpath.py`'s existing `DEFAULT_WINDOW_END`) and a `_synthesize_dob(rng)`
helper that computes the age-appropriate date range against that fixed anchor and draws a
uniform offset via `rng` (the already-seeded `random.Random`, not Faker's internal state) --
the same "offset within a fixed window" pattern `mdm.generator.matchpath._random_date_within`
already uses. Verified by regenerating dev tier twice in immediate succession
post-fix (byte-identical) and re-running the full unit + integration suite (261 passed, only
the 3 pre-existing Windows/PySpark failures, no new regressions). Downstream consequence,
expected and accepted: every identity's DOB (and everything generated after it in the same
function/appearance, since removing a `faker.*` call shifts Faker's own internal stream)
changes from what was previously committed to disk/BigQuery -- fixing a `datetime.now()`
dependency necessarily invalidates any previously-generated output that depended on it, the
same way fixing Phase 19's shared-RNG bug did. The already-loaded Phase 14 core population
needs a full fresh regeneration and reload to be consistent with this fix, not just the
new match-path domains -- see the scale-run re-verification this fix triggered.

## Generator peak memory scaled with total tier size, not shard size -- fixed by writing each shard as it completes

**Phase:** 1 (pattern present since), found generating a full 5,048,389-record, six-domain
scale-tier run for real
**What was found:** `scripts/generate.py`'s `run()` accumulated every shard's rows, for
every domain, into `vendor_shards`/`fact_shards`/`matchpath_shards` dicts *before* writing
any of them to Parquet -- fine at Phase 1's three-vendor-only scope (Phase 14's real 5M
core-only run succeeded under this pattern), but Phase 19/20 added six more domains' worth
of per-identity data to what's held per shard, and holding *all* of it for the *entire*
2,400,000-identity population at once raised a `MemoryError` on this machine (17.9 GB
available) before a single Parquet file had been written.
**How to apply:** Restructured `run()` to write each shard's output immediately as it comes
back from `executor.map` (still processed in task-submission order, so `shard_index`
still lines up exactly the same way), accumulating only small per-domain integer counts
across the run rather than row data. Peak memory is now one shard's worth of rows
(`chunk_size` identities, currently 2,000) regardless of how many identities the tier has in
total. Verified via the existing generator determinism/worker-independence tests (all still
pass -- output layout and content are unchanged, only *when* each shard gets written) and
a direct dev-tier byte-comparison against the pre-refactor code path.

## Thresholds were a single global pair for a per-tier quantity, and the scale tier silently dropped its review band

**Phase:** post-23, found 2026-08-23 while investigating an unrelated ci-tier recall figure
**What was found:** `config/matching.yml` declared one global `thresholds: {upper, lower}`,
both `9.5203` (dev-measured). But the threshold is a property of the *population*, not the
scorer -- Phase 14 established that and set the scale tier's cutoff to `20.5`. That value
lived in `Makefile` and `airflow/dags/dedup_dag.py` as a `--upper-threshold` CLI argument,
not in config. So at the scale tier two different consumers read two different numbers:
`spark_jobs/cluster_identities.py` auto-matched at 20.5, while
`scripts/run_matching_bigquery.py` -- which builds `serving.review_queue`, and had no
`--tier` flag -- queried `WHERE score >= 9.5203 AND score < 9.5203`, an always-empty band.
Every pair scoring between 9.5203 and 20.5 was therefore neither auto-matched nor reviewed:
classified `non_match` and dropped with no record. The direction is safety-favourable (false
splits, not false merges) but the two-threshold triage design was inert at scale.
**Why it went unseen:** `docs/scale-run.md` reported `review_queue_rate=0.0` as a *passing*
quality gate, and a zero-width review band was already documented above as a legitimate dev-
tier outcome. A correct decision at one tier became camouflage for a defect at another. A
metric that reads as good news deserves confirming it means what it appears to.
**Decision:** `thresholds` is keyed by tier (`ci`/`dev`/`scale`); `load_thresholds(tier)`
takes a required `tier` with no default, so an unknown tier raises rather than borrowing
another tier's cutoff; both BigQuery scripts gained `--tier`; and `20.5` moved out of the
Makefile and DAG into config, which now feeds every consumer (P5).
**On the `ci` entry:** it deliberately holds dev's 9.5203 rather than a ci-specific sweep.
`python -m mdm.evaluate --tier ci` does produce one (1.7929), but `threshold_sweep`'s 0.99
precision target is set on *core* member matching while the same cutoff is reused for
match-path resolution against a different candidate population. At 1.7929 two distinct people
merged into one `patient_global_id`, caught by
`test_member_360_domain_counts_match_fact_tables` reconciling `member_360`'s plan-tier count
against `fct_pharmacy_info` (a member-level file: one record per person, so a collision is a
false merge). A false merge is a safety incident; a low recall number on a 5,050-record smoke
tier is not.
**Still open:** the scale tier's `lower` has never been independently measured. It is held
equal to `upper` so the band is zero-width and nothing is dropped silently -- honest, but
derived is better. It needs `threshold_sweep` against the 5M ground truth, which means a real
Dataproc/BigQuery run.
**How to apply:** when a config key holds a value that varies along a dimension the system
already models (here, tier), key it by that dimension rather than documenting the exception in
a comment. The comment version had been correct and prominently placed for two phases; it
still produced two consumers reading different numbers, because a comment cannot be read by
`run_matching_bigquery.py`.
