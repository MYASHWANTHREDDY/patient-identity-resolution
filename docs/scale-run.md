# Scale run: 5M records end to end

Phase 14 (PROJECT_CONSTITUTION.md): generate 5M records, load, run the full pipeline on
BigQuery + Dataproc Serverless, watch the block stats, capture whatever broke that didn't
break at 50K. Every number below comes from an actual command run against real data --
see `docs/design-decisions.md` for the full narrative behind each finding.

## Data

- 5,048,389 records generated (`scripts/generate.py --tier scale --seed 42 --workers 8`),
  target 5,000,000 across 2,400,000 identities.
- Loaded to `raw_standard.vendor_{a,b,c}` in BigQuery; `conformance.patient_normalized`:
  5,048,389 rows.

## Blocking: all-pairs vs. candidate pairs, and the skew fix

All-pairs at 5,048,389 records is ~12.7 trillion possible pairs -- blocking's entire job is
avoiding that. What it actually produced, before and after the fix described below:

| Stage | Candidate pairs | Dominant pass |
| --- | --- | --- |
| Before fix (`bp_coarse` on `dob_decade`) | 647.1M | `bp_coarse`, 98% |
| After fix (`bp_coarse` on `dob_year`) | 337.3M | `bp_coarse`, 97.4% |

`bp_coarse`'s block key (`last_name_phonetic \| gender \| dob_decade`) has roughly fixed
cardinality, so as population grows, the same number of blocks absorbs more records each --
candidate pairs from a block grow with the square of its size. Switching the DOB component
from `dob_decade` to `dob_year` (matching `bp_year_names`'s already-proven granularity) cut
candidate pairs 48% *and* improved dev-tier recall (0.5329 -> 0.5338) and blocking
pair-completeness (0.9315 -> 0.9555) simultaneously -- not a tradeoff, because the old key's
oversized blocks were hitting `max_block_size: 1000` and getting silently dropped from
candidate generation, actively losing true pairs. Full root cause and verification:
`docs/design-decisions.md`, "`bp_coarse`'s fixed-cardinality DOB key...".

### Block size distribution and skew, post-fix (`matching.block_stats`, scale tier)

| Blocking pass | Blocks | Avg size | Max size | Pairs contributed | Share |
| --- | --- | --- | --- | --- | --- |
| `bp_coarse` | 184,721 | 27.3 | 697 | 323,929,074 | 97.4% |
| `bp_year_names` | 2,029,035 | 2.5 | 76 | 8,640,669 | 2.6% |
| `bp_dob_lname` | 2,605,240 | 1.9 | 18 | 3,843,095 | 1.2% |
| `bp_ssn` | 2,324,096 | 1.4 | 6 | 880,780 | 0.3% |

`bp_coarse` is still the dominant pass by design (it's the catch-all, no first-name
requirement) -- the fix was eliminating the *runaway* tail (blocks in the thousands hitting
the exclusion cap), not making it stop being the largest contributor.

## Scoring (Dataproc Serverless)

`spark_jobs/score_pairs.py`, `score-pairs-scale-20260718-164335`:

| Metric | Value |
| --- | --- |
| Input | 337.3M candidate pairs (deduplicated) |
| Output | 327,366,916 distinct scored pairs |
| Runtime | ~33 minutes |
| Compute | 14.16 DCU-hours |
| Shuffle storage | 1,433.9 GB-hours |
| Cost | ~$16.62 |

Broadcasting `patient_normalized` (a few hundred MB even at 5M records) instead of letting
Spark shuffle-join it against the much larger `candidate_pairs`/`pair_scores` was the
single highest-leverage cost lever this phase -- verified correct via
`tests/unit/test_backends_spark.py` and a local parity harness (0/5000 mismatches) before
being trusted against real, billed compute.

## Clustering (Dataproc Serverless): what broke, and what it cost to find out

Five submission attempts were needed to get a correct, efficiently-configured result --
each one, and its real cost, below (`gcloud dataproc batches describe ... runtimeInfo.
approximateUsage`, converted at ~$0.060/DCU-hour + ~$0.011/GB-hour, the calibrated
Dataproc Serverless rates in effect for this run):

| Attempt | Issue | Runtime | Cost |
| --- | --- | --- | --- |
| 1 | Missing `--checkpoint-dir`; cancelled before real compute started | -- | ~$0 |
| 2 | Default 200 shuffle partitions vs. a 7-worker/28-core quota ceiling; cancelled, never converged | 76+ min | ~$45.14 |
| 3 | Resubmitted from a stale GCS copy of the edited script; failed on `unrecognized arguments` | <1 min | ~$0.26 |
| 4 | Correct script, but pre-fix (dev-tier-derived) threshold; cancelled once the precision problem below was found | ~6 min | ~$6.30 |
| 5 | Correct script, tuned shuffle config, correct scale-tier threshold | ~18 min | ~$9.25 |
| **Total** | | | **~$60.95** |

Attempt 5 succeeded: 2,244,989 clusters, 4,831,903 of 5,048,389 records touched by at least
one auto-match edge (216,486 untouched singletons added downstream), max cluster size 45,
25,447 clusters flagged (oversized or low-density -- the density guard working as intended,
not silently merging them), 2,830,681 total auto-match edges.

**What broke, root cause:** `gcloud logging read` against `resource.type=
"cloud_dataproc_batch"` on attempt 2 showed Spark's dynamic allocation requesting up to 492
executors (`"max-needed-executors": "492"`) but hard-capped at 7 primary workers by the
project's `CPUS_ALL_REGIONS` compute quota. Combined with `connected_components`'s default
200 shuffle partitions (Spark's cluster-sized default, vastly oversubscribed against the
real ~28 available cores), every iteration of the label-propagation algorithm scheduled
~7 sequential waves of mostly-idle executors instead of 1 -- and because this algorithm
shuffles every iteration (unlike the single-pass scoring job), the overhead compounded
across the whole run. The fix: `--shuffle-partitions 32 --max-executors 7` on
`spark_jobs/cluster_identities.py`, matching the real quota ceiling instead of Spark's
cluster-sized defaults. Runtime dropped 76+ minutes (never converged) to ~18 minutes
(converged, correct output); cost per attempt dropped from ~$45 to ~$9.25 despite
attempt 5 actually completing the job attempt 2 didn't.

**The lesson that generalizes:** Dataproc Serverless bills `shuffleStorageGbSeconds`, which
accumulates with wall-clock time, not just data volume. A job that's slow because of a
parallelism mismatch can cost *more* than a larger job that runs efficiently -- small data
does not imply small cost if the job runs long enough. Full narrative:
`docs/design-decisions.md`, "Dataproc Serverless autoscaling is quota-capped...".

## The threshold that didn't transfer: precision collapse at 5M

Before trusting the retuned clustering job, the already-computed `matching.pair_scores`
were checked directly against real scale-tier ground truth (`data/scale/ground_truth`,
joined into BigQuery as a scratch table) rather than assuming the dev-tier-derived
threshold still applied at scale:

| Threshold | Precision | Recall | F1 | Source |
| --- | --- | --- | --- | --- |
| 7.8924 (stale dev-tier value) | 0.494 | 0.955 | -- | pre-fix `matching.yml` |
| 9.0413 (current dev-tier value) | 0.505 | 0.955 | -- | re-derived dev-tier value |
| **20.5 (scale-tier F1-optimal)** | **0.965** | **0.942** | **0.954** | measured directly against scale-tier ground truth |
| Dev tier's own optimum (9.0413, measured *at dev tier*) | ~0.99+ | ~0.9997 | 0.9967 | `docs/results.md` |

The same Fellegi-Sunter score means something different at different population sizes:
`bp_coarse` doesn't require first-name agreement, so within a block, candidate pairs can be
two genuinely different people who happen to share `last_name_phonetic + gender + dob_year`.
The absolute count of such coincidental near-matches grows much faster than the count of
true duplicates as population grows (the same underlying mechanism as the blocking skew
above), so a cutoff calibrated on a 50K-record sample systematically under-estimates how
high the cutoff needs to be at 5M -- and even the best achievable scale-tier F1 (0.954) is
meaningfully below what the identical model achieves at dev tier (0.9967). `config/
matching.yml`'s `thresholds` is now documented as dev/ci-tier only; the scale tier gets its
own, separately-measured value passed directly to `cluster_identities.py
--upper-threshold`. Full narrative: `docs/design-decisions.md`, "The same FS score means
something different at 5M records than at 50K...".

A second, independent bug was found in the course of this investigation: `matching.yml`'s
committed threshold (7.8924) was already stale for dev/ci tier too, calibrated against a
`fs_params.yml` that had since been regenerated twice this phase with nobody re-running the
sweep. Fixed to 9.0413, re-verified via a fresh `run_matching` + `run_quality_checks` pass
at dev tier (all 5 checks pass).

## Memory, not compute: crosswalk/survivorship's ceiling and its fix

`scripts/run_matching_bigquery.py` (crosswalk resolution + golden-record survivorship) was
verified at Phase 13's ~50K-record scale and assumed cheap ("record-count-sized data...
plain Python is enough" per its original docstring). At 5M records / 2.24M clusters, its
single Python process grew past 11.2GB resident and was still climbing when stopped, on a
machine with 24GB total RAM and only ~6.5GB free before the run even started. This is a
different axis of "breaks at scale" than the pair-count-scaled problems above:
`records_by_key`, `golden_records`, `field_lineage_rows`, `alternate_ids`, and
`membership_rows` were all plain Python structures sized to the full record/cluster count
at once, with no batching -- fine at 50K, not at multi-million scale.

Fixed by batching the golden-record construction and write by `patient_global_id` (default
100,000 pgids/batch, ~26 batches at this scale) rather than materializing all 2.56M golden
records at once -- `resolve_crosswalk` and `build_serving_tables` stayed unchanged;
only the orchestration around them batches now. Re-run against the real scale tier with
memory sampled continuously: peak resident memory stabilized around **6.0-6.4GB**, comfortably
bounded, vs. 11.2GB+ and still climbing before the fix. Full narrative: `docs/
design-decisions.md`, "Batching `run_matching_bigquery.py` by patient_global_id...".

## A second bug the memory fix uncovered: stale cross-tier crosswalk state

Fixing the memory ceiling let the run reach far enough to expose a second, independent bug:
`serving.crosswalk`/`serving.identity_events` still held Phase 13's 25,013-row dev-tier
(~50K-record) result. Because `scripts/generate.py` builds every `record_key` as
`f"{vendor}:{identity_index:08d}"` starting at 0 regardless of tier, the dev tier's 24,000
identities and the scale tier's first 24,000 (of 2,400,000) produce **identical record_key
strings** for entirely different synthetic people. The first real attempt's
`resolve_crosswalk` call silently treated 36,740 of 5,048,389 scale-tier records (0.73%) as
continuing an identity from that unrelated earlier run. The same stale table also caused a
loud, unrelated failure: `identity_events.retired_id` had been inferred as `INTEGER` from
Phase 13's data (every event there was a `CREATE`, so `retired_id` was always null), and the
scale tier's real `MERGE`/`SPLIT` events (actual string IDs) failed to load against it
outright.

Fixed by dropping all seven `serving.*` tables written by this script and re-running clean.
Verified: 0 of 5,048,389 crosswalk rows show cross-run reuse afterward, and
`identity_events.retired_id` now correctly infers as `STRING`. Full narrative: `docs/
design-decisions.md`, "A stale, unrelated dev-tier crosswalk silently corrupted 0.73% of the
scale-tier run".

## Crosswalk/survivorship, serving, snapshot, quality gates: the completed run

With both fixes in place, the full chain ran end to end against the real 5M-record scale
tier:

| Metric | Value |
| --- | --- |
| Golden records | 2,559,287 |
| Identity events (all CREATE -- first-ever resolution) | 2,559,287 |
| dbt `serving` models | 3/3 (agg_dedup_metrics, dim_member, member_360) |
| dbt snapshot | 2.6M rows merged, 270.7 MiB scanned |
| Quality gates | 5/5 pass (dedup_rate=0.493, review_queue_rate=0.0 [**see correction below**], largest block share <0.01%, 0 implausible DOBs, largest *merged* cluster 6 members) |

`cluster_size_distribution`'s "largest cluster has 6 members" (not 45, the largest raw
cluster from the Dataproc clustering step) confirms the flagged-cluster guard works
correctly end to end at scale: the 45-member cluster was flagged, never merged, and each of
its members became its own singleton golden record instead -- exactly the P13 "when
uncertain, do not merge" design, verified against real 5M-scale output. Golden-record count
reconciles exactly: 2,219,542 from clean (unflagged) clusters, plus 216,486 untouched
singletons, plus 123,259 individual members of flagged clusters, totals 2,559,287.

## Correction (2026-08-23): `review_queue_rate=0.0` was a defect, not a result

The `review_queue_rate=0.0` reported above is left as written -- it is what the run
produced -- but it did **not** mean "nothing needed review at 5M records". It meant the
review band was structurally empty, and the pairs that should have populated it were
silently discarded.

`config/matching.yml` carried a single global `thresholds` pair, `upper = lower = 9.5203`,
measured at dev tier. The scale run's auto-match decision did not use it: the correct,
separately-measured scale cutoff of 20.5 was passed straight to
`spark_jobs/cluster_identities.py` as `--upper-threshold` from the Makefile and the Airflow
DAG. But `scripts/run_matching_bigquery.py` -- which builds `serving.review_queue` -- read
the config values, and had no `--tier` flag to tell it otherwise. Its query was therefore:

```sql
WHERE score >= 9.5203 AND score < 9.5203   -- always empty
```

So at the scale tier every pair scored between **9.5203 and 20.5** was neither auto-matched
(below the 20.5 clustering cutoff) nor routed to review (the band was empty). Those pairs --
precisely the uncertain middle the two-threshold design exists to protect -- were classified
`non_match` and dropped without a record. In safety terms the direction is the favourable
one (false splits, not false merges), but the review mechanism this project describes as
non-negotiable was inert at the tier it was showcased on.

**Why it stayed hidden:** an empty review band is a *documented, legitimate* outcome at dev
tier (see design-decisions.md, "a zero-width review band is a legitimate outcome when a
scorer separates classes cleanly"). That reasoning is sound where `upper == lower` and the
auto-match line is the same value -- no gap can exist. It stops being sound once the
auto-match line moves to 20.5 and the band does not move with it. The same observation,
`review_queue_rate=0.0`, means opposite things in the two cases, and the existing
documentation made the broken one read as expected.

**Fixed by** keying `thresholds` per tier in `config/matching.yml`, requiring a `tier`
argument in `load_thresholds()` (no default -- an unknown tier raises rather than borrowing
another tier's cutoff), adding `--tier` to both BigQuery scripts, and moving the 20.5 out of
the Makefile and the DAG into config so a single source of truth feeds every consumer.

**Still open:** the scale tier's `lower` has never been independently measured. It is
currently held equal to `upper` (20.5), which makes the band zero-width and drops nothing
silently -- honest, but not the same as derived. A real value needs `threshold_sweep` run
against the 5M ground truth. The numbers above were not re-measured after this fix; a
re-run would be needed to report a non-zero `review_queue_rate`.

## Cost summary

| Stage | Cost |
| --- | --- |
| Blocking / dbt build (BigQuery, on-demand) | negligible (a few queries, largest ~23 GiB scanned) |
| Scoring (Dataproc Serverless) | ~$16.62 |
| Clustering (Dataproc Serverless, all 5 attempts) | ~$60.95 |
| Threshold verification queries (BigQuery, on-demand) | negligible (cents) |
| Crosswalk/survivorship + serving + snapshot + quality gates (BigQuery, on-demand) | negligible (largest single query 270.7 MiB scanned) |
| **Total, this phase** | **~$77.57** |

All drawn from the GCP free-trial credit balance ($300 total); no real card was charged.
The clustering total is dominated by the two misconfigured attempts (2 and 4) made while
diagnosing genuine, real problems -- not wasted exploration for its own sake, but a real
cost of finding out what breaks at this scale empirically rather than guessing. No Dataproc
(billed) compute was needed to find or fix either the memory ceiling or the stale-crosswalk
bug -- both were diagnosed and fixed entirely locally and via on-demand BigQuery queries.

## What broke at 5M that didn't at 50K -- summary

1. **`bp_coarse`'s blocking key** had fixed cardinality; candidate pairs grew
   superlinearly with population until the block-size cap started silently dropping true
   pairs. Fixed by tightening the DOB component.
2. **BigQuery's `maximum_bytes_billed` safety cap** (20 GiB, set at Phase 11's much
   smaller scale) was too conservative for legitimate scale-tier structural tests
   (~22.9 GiB needed). Raised to 50 GiB with the real per-query dollar cost documented.
3. **Dataproc Serverless's autoscaler is capped by a real GCP compute quota**
   (`CPUS_ALL_REGIONS`, 7 workers) that Spark's dynamic allocation doesn't know about --
   combined with default shuffle partitioning tuned for a much larger cluster, this turned
   an iterative algorithm's per-iteration overhead into the single largest cost driver
   this phase.
4. **The same FS score threshold means a different thing at 5M than at 50K** -- precision
   at a fixed cutoff degrades as the population of coincidental near-matches grows faster
   than the population of true duplicates. Thresholds must be re-measured per tier, not
   assumed to transfer.
5. **Pure-Python, unbatched record-count-scaled processing** (crosswalk/survivorship) hit
   a real memory ceiling at 5M records / 2.24M clusters -- the "cheap" side of this
   pipeline turned out not to be cheap at this scale either. Fixed by batching golden-record
   construction/writes by `patient_global_id`.
6. **A shared BigQuery project doesn't isolate tiers the way per-tier local DuckDB files
   do** -- a dev-tier verification run and a scale-tier real run left overlapping
   record_keys (both start `identity_index` at 0), and stale serving-layer state from the
   former silently corrupted 0.73% of the latter until the tables were cleared.
7. **One config key held a per-tier quantity** -- `thresholds` was a single global pair, so
   the scale tier auto-matched at its own measured 20.5 while building its review queue from
   dev's 9.5203, and everything scoring in between was silently dropped. Found 2026-08-23,
   after this run; see the correction section above. The tell was `review_queue_rate=0.0`
   passing as a quality gate -- a metric that looks like good news is worth confirming
   *means* what it looks like.
