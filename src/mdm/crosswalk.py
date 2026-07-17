"""Crosswalk resolution (PROJECT_CONSTITUTION.md #13.4) -- the answer to "what happens if
you run this twice." record_key -> patient_global_id, stable across runs:

    zero existing IDs among a cluster's members -> mint a new patient_global_id
    exactly one existing ID                     -> reuse it (identity persists)
    more than one existing ID                    -> MERGE: oldest ID survives, rest retired
    a record's former cluster-mates end up under a different ID -> SPLIT, logged

Retired IDs are marked, never deleted.

An id can belong to at most one cluster per run. Clusters are resolved in a deterministic
order (by their smallest member record_key); once an id is claimed by the first cluster
that references it, any *other* cluster that used to share that id can no longer reuse it --
that's exactly what a split is: a subset of an id's former members is no longer connected to
the rest, so it needs its own identity going forward, and losing the id gets logged.
"""

from __future__ import annotations

from dataclasses import dataclass

CREATE = "create"
MERGE = "merge"
SPLIT = "split"


@dataclass(frozen=True)
class CrosswalkEntry:
    record_key: str
    patient_global_id: str
    first_seen_run: str
    last_seen_run: str


@dataclass(frozen=True)
class IdentityEvent:
    event_type: str
    surviving_id: str
    retired_id: str | None
    run_id: str
    reason: str


def mint_id(sequence: int) -> str:
    return f"PGID{sequence:012d}"


def resolve_crosswalk(
    existing: dict[str, CrosswalkEntry],
    clusters: dict[str, tuple[str, ...]],
    *,
    run_id: str,
    next_sequence: int = 0,
) -> tuple[dict[str, CrosswalkEntry], list[IdentityEvent]]:
    """`clusters` maps every record_key present in this run to its finalized cluster
    membership tuple (see clustering.finalize_cluster_membership) -- singletons included.
    Every record_key must appear; a record absent from `clusters` keeps no crosswalk entry."""
    events: list[IdentityEvent] = []
    new_crosswalk: dict[str, CrosswalkEntry] = {}
    sequence = next_sequence
    claimed_ids: set[str] = set()

    unique_clusters = sorted(set(clusters.values()), key=min)

    for members in unique_clusters:
        raw_existing_ids = {existing[m].patient_global_id for m in members if m in existing}
        # ids another, earlier-processed cluster already claimed this run aren't available
        # -- a subset of that id's former members has moved on; that's a split.
        available_ids = raw_existing_ids - claimed_ids
        orphaned_ids = raw_existing_ids & claimed_ids

        if not available_ids:
            surviving_id = mint_id(sequence)
            sequence += 1
            reason = (
                "all prior ids already claimed elsewhere this run"
                if raw_existing_ids
                else "new cluster, no prior crosswalk entry"
            )
            events.append(IdentityEvent(CREATE, surviving_id, None, run_id, reason))
        elif len(available_ids) == 1:
            surviving_id = next(iter(available_ids))
        else:
            first_seen_by_id = {
                eid: min(e.first_seen_run for e in existing.values() if e.patient_global_id == eid)
                for eid in available_ids
            }
            surviving_id = min(available_ids, key=lambda eid: (first_seen_by_id[eid], eid))
            for retired_id in sorted(available_ids - {surviving_id}):
                events.append(
                    IdentityEvent(
                        MERGE,
                        surviving_id,
                        retired_id,
                        run_id,
                        f"cluster unified existing ids {sorted(available_ids)}",
                    )
                )

        for orphaned_id in sorted(orphaned_ids):
            events.append(
                IdentityEvent(
                    SPLIT,
                    orphaned_id,
                    surviving_id,
                    run_id,
                    f"a subset of {orphaned_id}'s former members moved to {surviving_id}",
                )
            )

        claimed_ids.add(surviving_id)
        claimed_ids.update(available_ids)

        for member in members:
            first_seen = existing[member].first_seen_run if member in existing else run_id
            new_crosswalk[member] = CrosswalkEntry(member, surviving_id, first_seen, run_id)

    return new_crosswalk, events
