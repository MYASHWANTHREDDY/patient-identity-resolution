# Design decisions

Every significant, non-obvious choice gets an entry here, written at the time the decision is
made — not reconstructed later (P12). Alternatives considered and why they were rejected matter
more than the decision itself.

Entry template:

```
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
