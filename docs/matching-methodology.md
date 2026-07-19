# Matching methodology

How this system decides two records describe the same person. Built and evaluated in the
order below — each stage is independently testable, and the progression is itself the
argument for why probabilistic matching is necessary rather than a stylistic choice. For the
surrounding system (data layers, tiered backends, storage), see
[architecture.md](architecture.md). Every number below is real, from this project's own
dev-tier run — see [results.md](results.md) to regenerate them yourself
(`python -m mdm.evaluate --tier dev`).

## Stage 0 — the deterministic baseline, measured first

Before any fuzzy matching: exact agreement on SSN, or exact agreement on first name + last
name + DOB together. This is asserted, not scored, and its recall is measured *before*
building anything more sophisticated, specifically so every later stage can be reported as
improvement over it rather than compared to nothing. It catches only clean duplicates —
exactly the identities that were never corrupted — and misses everything else by
construction.

## Stage 1 — blocking

Comparing every record against every other record is quadratic: at 5,048,389 records, that's
~12.7 trillion pairs. Blocking avoids the comparison entirely for record pairs that couldn't
plausibly match, by grouping records that agree on a cheap key first and only scoring pairs
that share a group ("block"). Four blocking passes run in parallel and get unioned, each
catching a different corruption pattern the others would miss:

| Pass | Key | Catches |
| --- | --- | --- |
| `bp_ssn` | `ssn` | Any pair sharing an SSN, regardless of name/DOB noise |
| `bp_dob_lname` | `dob` + `last_name_phonetic` | Exact DOB survives most name corruption |
| `bp_year_names` | `dob_year` + `first_name_phonetic` + `last_name_phonetic` | Both names roughly right, DOB day/month noisy |
| `bp_coarse` | `last_name_phonetic` + `gender` + `dob_year` | Catch-all — no first-name requirement, so first-name typos/nicknames don't fall through every other pass |

A block that grows past `max_block_size` (1,000 records) is excluded from candidate
generation rather than scored — one oversized block can otherwise generate millions of pairs
and stall the whole job. The cost of that exclusion is measured, not assumed: **pair
completeness** (the fraction of real duplicate pairs blocking actually surfaces) is tracked
per pass and unioned, currently **0.9561** at dev tier, alongside **reduction ratio**
(`1 - candidate_pairs / all_possible_pairs`), currently **0.999952** — blocking discards
99.9952% of all possible pairs while still surfacing 95.6% of the real matches among what's
left.

**The skew problem is where blocking actually gets hard, and it doesn't show up at small
scale.** `bp_coarse`'s key space is bounded — roughly (distinct phonetic last names) × 2
genders × (DOB years in range) — so as the underlying population grows, the *same* number of
blocks has to absorb more records each, and pairs from a block grow with the square of its
size. At this project's 50K dev tier that's invisible (the largest block share is 0.01% of
records). At the 5M scale tier it became the dominant cost driver: an earlier, coarser DOB
granularity (`dob_decade`) produced 647.1M candidate pairs, 98% of them from `bp_coarse`
alone, with individual blocks large enough to hit the size cap and start silently dropping
true pairs. Tightening the DOB component to `dob_year` cut candidate pairs 48% *and*
improved pair completeness — full incident writeup in
[design-decisions.md](design-decisions.md) and [scale-run.md](scale-run.md).

## Stage 2 — comparators

Every candidate pair gets compared field by field. Each comparator returns a **discrete
agreement level**, never a raw similarity float — the distinction matters because Fellegi-
Sunter scoring (next stage) needs a countable, finite set of outcomes to estimate
probabilities against, not a continuous score to threshold ad hoc.

**Name** (`compare_name`, used for both first and last name):

| Level | Condition |
| --- | --- |
| `exact` | Identical after normalization |
| `nickname` | Same canonical form via a lookup table (Robert ↔ Bob) |
| `near` | Jaro-Winkler similarity ≥ 0.90 |
| `similar` | Jaro-Winkler similarity ≥ 0.80 |
| `different` | Otherwise |
| `missing` | Either side absent |

Jaro-Winkler, not Levenshtein, because it weights agreement at the *start* of the string more
heavily — people mistype the ends of names far more than the beginnings, and truncation is
common in legacy feeds. `nickname` is its own level, checked *before* falling back to
similarity, because "Robert" vs. "Bob" scores ≈0.5 on Jaro-Winkler — indistinguishable from a
genuinely different name. No threshold recovers that; it needs the lookup table. This is the
single clearest proof in the whole project that string-similarity matching alone is
insufficient for human names.

**Date of birth** (`compare_dob`):

| Level | Condition |
| --- | --- |
| `exact` | Identical |
| `transposed` | Day and month swapped (`03-04` vs. `04-03`) |
| `one_component_off` | Exactly one of year/month/day differs |
| `year_only` | Years match, rest doesn't |
| `different` | Otherwise |
| `missing` | Either side absent |

`transposed` is its own level because day/month transposition is a **systematic** error (it's
what happens when a date crosses a US/international format boundary), not random noise —
collapsing it into "different" would throw away a strong, structured signal.

**SSN and gender** (`compare_ssn`, `compare_gender`): `exact` / `different` / `missing`.
Gender additionally treats `U` (unknown) as `missing` rather than a comparable value.

## Stage 3 — Fellegi-Sunter scoring

Hand-tuned field weights ("40% name, 30% DOB, 20% gender, 10% SSN") invite one honest
follow-up question — "where did those numbers come from?" — with only one honest answer: "I
made them up." Fellegi-Sunter (1969) is the foundational framework for probabilistic record
linkage precisely because it replaces that guess with numbers estimated from data.

For every field and agreement level, two probabilities are estimated from labeled ground
truth (`scripts/estimate_fs_params.py`, dev tier only — these are properties of the noise
model, not the row count, so they're estimated once and reused unchanged at scale):

- **m** = P(this agreement level | the pair is a true match) — estimated from labeled
  true-match pairs
- **u** = P(this agreement level | the pair is *not* a match) — estimated from a random
  sample of non-match pairs

Each field/level's evidence is `log2(m / u)` bits; a pair's total score is the sum across
fields. A missing field contributes exactly **0** — it falls out of the math automatically,
rather than needing a special case, which is exactly the behavior Vendor C's missing-SSN
records require (see [architecture.md](architecture.md)'s data contracts).

**Real, current weights from this project's own dev-tier estimation** (`config/fs_params.yml`)
show the framework correcting intuition, not just confirming it:

| Field | Agreement | u (chance two random records agree) | Weight (bits) |
| --- | --- | --- | --- |
| `dob` | `exact` | 0.00005 | **+13.94** — the single strongest signal in the model |
| `ssn` | `exact` | 0.00005 | **+12.57** |
| `last_name` | `exact` | 0.0025 | +8.30 |
| `first_name` | `exact` | 0.0061 | +6.87 |
| `gender` | `exact` | **0.498** | **+1.00** — barely more than a coin flip's worth of evidence |
| `dob` | `different` | 0.988 | −10.56 |

Gender agrees roughly half the time between two *random* people, so the model derives that it
carries almost no evidence — about 1 bit, versus the ~14 bits DOB agreement carries. A naive
hand-tuned scheme assigning gender a flat 10-20% of the decision would be overweighting it by
roughly an order of magnitude; Fellegi-Sunter arrives at the right answer from the data
instead of needing that corrected by hand.

A deliberately naive scorer (`score_naive`, hand-tuned weights, no estimation) is kept
alongside the real one specifically so this comparison can be run and reported rather than
asserted: at dev tier, the Fellegi-Sunter scorer reaches **F1 = 0.9972** against labeled
ground truth versus the naive scorer's **F1 = 0.9962** — both good, because this synthetic
noise model is fairly separable, but only one of them can say why its weights are what they
are.

## Stage 4 — threshold triage

```
score >= upper          → auto_match  (merged without human review)
lower <= score < upper  → review      (routed to a human)
score <  lower           → non_match   (discarded)
```

Two thresholds, not one, because a false merge and a false split don't cost the same: wrongly
combining two different patients' records is a safety incident, wrongly leaving one person as
two is a data-quality inconvenience. A single cutoff silently asserts these are equivalent.
`upper` is set to the lowest score meeting a precision target; `lower` to the highest score
meeting a recall target — both swept from the real precision/recall curve
(`python -m mdm.evaluate`), never chosen by eye. In this project's synthetic noise model at
dev tier the two targets turn out to be satisfiable at nearly the same cutoff, collapsing the
review band to zero width — a genuine, measured finding, not a design shortcut (the
computation that produces this, and the one time it needed clamping to stay well-formed, is
documented in [design-decisions.md](design-decisions.md)).

**This threshold does not transfer across scale, and re-deriving it per tier is required, not
optional.** The same Fellegi-Sunter score means a different thing at 5,048,389 records than
at 50,000: `bp_coarse` doesn't require first-name agreement, so within a block, two genuinely
different people who happen to share `last_name_phonetic + gender + dob_year` become far more
numerous as the underlying population grows — the same fixed score threshold lets more of
them through. Measured directly against real 5M-scale ground truth: the dev-tier threshold
gave only **~50% precision** at scale; the correct, separately-measured scale-tier threshold
recovers **96.5% precision** at 94.2% recall. Full measurement and root cause in
[scale-run.md](scale-run.md).

## Stage 5 — clustering

Auto-match edges form an undirected graph; connected components become clusters of records
believed to be one person. At the scale tier this runs as iterative min-label propagation on
Spark (`mdm.backends.spark.connected_components`), since a single-process union-find doesn't
parallelize; locally it's a plain union-find.

**The transitivity problem is the most dangerous failure mode in the whole system.** A~B
scoring above threshold and B~C scoring above threshold does not imply A~C — a chain of
individually-plausible weak links can produce an over-merged "monster cluster" combining
several real people. Two guards catch this instead of trusting transitivity:

- **Size guard** — a cluster larger than `max_cluster_size` (6) is flagged.
- **Density guard** — `cluster_confidence = scored_pairs / possible_pairs`; a cluster whose
  actual internal agreement is thin relative to its size (below `min_cluster_density`, 0.6)
  is flagged.

A flagged cluster is never silently merged — every member instead becomes its own singleton
identity, and the cluster is routed for review. This is "when uncertain, do not merge" made
concrete: verified at real 5M-scale output, the largest *raw* cluster the graph produced was
45 members (correctly flagged), while the largest cluster actually allowed to merge into one
golden record was exactly 6 — the configured cap, never exceeded, because nothing over it is
ever silently let through.

## Stage 6 — crosswalk resolution

`patient_global_id` has to stay stable across repeated runs, not just be self-consistent
within one. For every cluster, on every run:

| Existing IDs found among the cluster's members | Outcome |
| --- | --- |
| Zero | Mint a new `patient_global_id` (`CREATE` event) |
| Exactly one | Reuse it — the identity persists |
| More than one | **`MERGE`** — the oldest ID survives, the others are retired (never deleted) |
| A record's former cluster-mates are now under a different ID | **`SPLIT`** — logged |

IDs are claimed within a run in a deterministic order (by each cluster's smallest member
`record_key`), so a run's own merge/split decisions are reproducible from the same input. What
happens on the second run — and the third, after new data arrives — is a designed, tested
behavior (`resolve_crosswalk`), not an afterthought.

## Stage 7 — survivorship

Once a cluster is resolved to one `patient_global_id`, each field of the golden record is
built independently by a rule chain, evaluated in order until one rule breaks the tie:
`vendor_trust → plurality → completeness → recency → deterministic tiebreak`. `vendor_trust`
is a per-field ranking (e.g. Vendor A is trusted first for `dob`, but Vendor C for SSN
availability); `deterministic tiebreak` — lexicographically smallest `record_key` — exists
because without a final deterministic rule, two runs over identical data could produce
different golden records depending on row ordering, and Spark partition ordering is
explicitly *not* guaranteed stable. Every rule chain has to terminate in something
deterministic, or reproducibility (a non-negotiable project principle) breaks silently. Every
winning value writes a `field_lineage` row recording which source contributed it and which
rule decided.
