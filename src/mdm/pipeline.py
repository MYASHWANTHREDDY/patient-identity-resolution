"""Orchestrates scoring -> triage -> clustering -> crosswalk -> survivorship into the
serving tables (PROJECT_CONSTITUTION.md #5, the "SERVING" stage of the architecture).
Pure functions live in scoring.py/triage.py/clustering.py/crosswalk.py/survivorship.py;
this module sequences them and touches DuckDB for the local (ci/dev tier) backend. Several
of its own helpers (build_serving_tables, next_crosswalk_sequence, sanitize_nan,
load_fs_params/load_nickname_index/load_thresholds) are public rather than private because
scripts/run_matching_bigquery.py -- the Dataproc/BigQuery-backed sibling used from
airflow/dags/dedup_dag.py -- reuses them directly rather than re-implementing the same
crosswalk/survivorship assembly logic against a different data source (PROJECT_CONSTITUTION.md
#8: one codebase, two backends).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    import duckdb

from mdm.clustering import build_clusters, finalize_cluster_membership
from mdm.crosswalk import CrosswalkEntry, resolve_crosswalk
from mdm.scoring import compare_record_pair, score_fs
from mdm.survivorship import FIELDS as SURVIVORSHIP_FIELDS
from mdm.survivorship import build_golden_record
from mdm.triage import AUTO_MATCH, REVIEW, decide

_PGID_PATTERN = re.compile(r"^PGID(\d+)$")


def sanitize_nan(records_by_key: dict[str, dict]) -> None:
    """pandas' DataFrame.to_dict silently turns a SQL NULL into float('nan') (or NaT)
    instead of leaving it None -- the same failure mode fixed once already in
    comparators.is_missing. Sanitizing once here, at the pandas/dict boundary, means
    every pure function downstream (comparators, survivorship) can trust `None` as the
    only "missing" representation instead of each needing its own NaN guard."""
    for record in records_by_key.values():
        for key, value in record.items():
            if value != value:  # True only for NaN/NaT, the self-inequality trick
                record[key] = None


def next_crosswalk_sequence(existing: dict[str, CrosswalkEntry]) -> int:
    """The next unused PGID sequence number given the current crosswalk. Pure and
    backend-agnostic, like build_serving_tables -- reused by scripts/run_matching_bigquery.py."""
    if not existing:
        return 0
    numbers = []
    for entry in existing.values():
        match = _PGID_PATTERN.match(entry.patient_global_id)
        if match:
            numbers.append(int(match.group(1)))
    return (max(numbers) + 1) if numbers else 0


def _load_existing_crosswalk(con: duckdb.DuckDBPyConnection) -> dict[str, CrosswalkEntry]:
    tables = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'serving' AND table_name = 'crosswalk'"
    ).fetchall()
    if not tables:
        return {}
    df = con.execute("SELECT * FROM serving.crosswalk").df()
    return {
        row.record_key: CrosswalkEntry(
            row.record_key, row.patient_global_id, row.first_seen_run, row.last_seen_run
        )
        for row in df.itertuples()
    }


def run_matching(
    db_path: str,
    *,
    run_id: str | None = None,
    fs_params: dict | None = None,
    nickname_index: dict[str, str] | None = None,
) -> dict:
    """`fs_params`/`nickname_index` default to loading from config/ if not passed
    explicitly -- tests pass them in directly so a test run never touches the real,
    committed config/fs_params.yml."""
    import duckdb  # local: keeps this module importable without duckdb installed (see the
    # module docstring) for callers -- like the Airflow image -- that only need the
    # backend-agnostic helpers below, not this DuckDB-specific orchestration function.

    run_id = run_id or datetime.now(UTC).isoformat()
    fs_params = fs_params if fs_params is not None else load_fs_params()
    nickname_index = nickname_index if nickname_index is not None else load_nickname_index()

    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS serving")

        patient_normalized = con.execute(
            "SELECT record_key, source_vendor, source_record_id, first_name, last_name, "
            "dob, gender, ssn, normalized_at FROM conformance.patient_normalized"
        ).df()
        candidate_pairs = con.execute(
            "SELECT DISTINCT record_key_a, record_key_b FROM matching.candidate_pairs"
        ).df()
        thresholds = load_thresholds()

        records_by_key = patient_normalized.set_index("record_key").to_dict(orient="index")
        for key, record in records_by_key.items():
            record["record_key"] = key
        sanitize_nan(records_by_key)

        auto_match_edges = []
        review_pairs = []
        for record_key_a, record_key_b in zip(
            candidate_pairs["record_key_a"], candidate_pairs["record_key_b"], strict=False
        ):
            agreement = compare_record_pair(
                records_by_key[record_key_a],
                records_by_key[record_key_b],
                nickname_index=nickname_index,
            )
            score = score_fs(agreement, fs_params)
            decision = decide(score, upper=thresholds["upper"], lower=thresholds["lower"])
            if decision == AUTO_MATCH:
                auto_match_edges.append((record_key_a, record_key_b))
            elif decision == REVIEW:
                review_pairs.append((record_key_a, record_key_b, score))

        clusters = build_clusters(
            auto_match_edges,
            max_cluster_size=thresholds["max_cluster_size"],
            min_cluster_density=thresholds["min_cluster_density"],
        )
        membership = finalize_cluster_membership(clusters)
        # every record gets a cluster, even ones touched by no auto-match edge at all
        for record_key in records_by_key:
            membership.setdefault(record_key, (record_key,))

        existing_crosswalk = _load_existing_crosswalk(con)
        next_sequence = next_crosswalk_sequence(existing_crosswalk)
        new_crosswalk, identity_events = resolve_crosswalk(
            existing_crosswalk, membership, run_id=run_id, next_sequence=next_sequence
        )

        golden_records, field_lineage_rows, alternate_ids, membership_rows = build_serving_tables(
            new_crosswalk, records_by_key, thresholds
        )

        _write_tables(
            con,
            new_crosswalk,
            identity_events,
            golden_records,
            field_lineage_rows,
            alternate_ids,
            membership_rows,
            review_pairs,
            run_id,
        )

        return {
            "run_id": run_id,
            "num_records": len(records_by_key),
            "num_auto_match_edges": len(auto_match_edges),
            "num_review_pairs": len(review_pairs),
            "num_clusters": len(clusters),
            "num_flagged_clusters": sum(1 for c in clusters if c.flagged),
            "num_golden_records": len(golden_records),
            "num_identity_events": len(identity_events),
        }
    finally:
        con.close()


def build_serving_tables(new_crosswalk, records_by_key, thresholds):
    """Cluster membership + record data -> golden records/lineage/alternate-ids/membership
    rows. Pure and backend-agnostic (PROJECT_CONSTITUTION.md #8): shared by run_matching
    (DuckDB, computes crosswalk locally) and scripts/run_matching_bigquery.py (crosswalk
    resolved against Dataproc-precomputed clusters read from BigQuery)."""
    members_by_pgid: dict[str, list[str]] = {}
    for record_key, entry in new_crosswalk.items():
        members_by_pgid.setdefault(entry.patient_global_id, []).append(record_key)

    golden_records = []
    field_lineage_rows = []
    alternate_ids = []
    membership_rows = []

    for pgid, record_keys in members_by_pgid.items():
        members = [records_by_key[rk] for rk in record_keys]
        golden_record, lineage = build_golden_record(
            pgid,
            members,
            rule_chain=thresholds["survivorship_rule_chain"],
            vendor_trust=thresholds["vendor_trust"],
        )
        ssn = golden_record.get("ssn")
        golden_record["ssn_last4"] = ssn[-4:] if ssn else None
        del golden_record["ssn"]  # minimization -- full SSN never reaches serving (#9)
        golden_records.append(golden_record)
        field_lineage_rows.extend(lineage)

        for record_key in record_keys:
            record = records_by_key[record_key]
            alternate_ids.append(
                {
                    "patient_global_id": pgid,
                    "source_vendor": record["source_vendor"],
                    "source_record_id": record["source_record_id"],
                }
            )

        membership_rows.append(
            {
                "patient_global_id": pgid,
                "source_record_count": len(record_keys),
            }
        )

    return golden_records, field_lineage_rows, alternate_ids, membership_rows


def _write_tables(
    con,
    new_crosswalk,
    identity_events,
    golden_records,
    field_lineage_rows,
    alternate_ids,
    membership_rows,
    review_pairs,
    run_id,
):
    crosswalk_df = pd.DataFrame(
        [
            {
                "record_key": e.record_key,
                "patient_global_id": e.patient_global_id,
                "first_seen_run": e.first_seen_run,
                "last_seen_run": e.last_seen_run,
            }
            for e in new_crosswalk.values()
        ]
    )
    con.register("crosswalk_df", crosswalk_df)
    con.execute("CREATE OR REPLACE TABLE serving.crosswalk AS SELECT * FROM crosswalk_df")

    # Append, not replace: identity_events is a permanent audit trail of create/merge/split
    # history across every run (PROJECT_CONSTITUTION.md #9's storage layout lists it with no
    # expiration, and its whole purpose is tracking identity changes *over time*) -- unlike
    # crosswalk, which is legitimately a current-state table safe to replace wholesale each
    # run because resolve_crosswalk's `new_crosswalk` already carries every prior entry
    # forward. Replacing this table each run silently discarded every earlier run's history;
    # the existing idempotency test only checked that a same-data re-run produced 0 *new*
    # events, which never exercised whether old events survived the second write.
    event_columns = ["event_type", "surviving_id", "retired_id", "run_id", "reason"]
    if identity_events:
        events_df = pd.DataFrame(
            [
                {
                    "event_type": e.event_type,
                    "surviving_id": e.surviving_id,
                    "retired_id": e.retired_id,
                    "run_id": e.run_id,
                    "reason": e.reason,
                }
                for e in identity_events
            ]
        )
    else:
        events_df = pd.DataFrame(columns=event_columns)
    con.execute(
        "CREATE TABLE IF NOT EXISTS serving.identity_events ("
        "event_type VARCHAR, surviving_id VARCHAR, retired_id VARCHAR, "
        "run_id VARCHAR, reason VARCHAR)"
    )
    con.register("events_df", events_df)
    con.execute("INSERT INTO serving.identity_events SELECT * FROM events_df")

    demographics_columns = [
        "patient_global_id",
        *[f for f in SURVIVORSHIP_FIELDS if f != "ssn"],
        "ssn_last4",
    ]
    if golden_records:
        demographics_df = pd.DataFrame(golden_records)[demographics_columns]
    else:
        demographics_df = pd.DataFrame(columns=demographics_columns)
    con.register("demographics_df", demographics_df)
    con.execute(
        "CREATE OR REPLACE TABLE serving.member_demographics AS SELECT * FROM demographics_df"
    )

    lineage_df = pd.DataFrame(
        [
            {
                "patient_global_id": row.patient_global_id,
                "field_name": row.field_name,
                "winning_value": str(row.winning_value) if row.winning_value is not None else None,
                "source_vendor": row.source_vendor,
                "source_record_id": row.source_record_id,
                "record_key": row.record_key,
                "rule_applied": row.rule_applied,
            }
            for row in field_lineage_rows
        ]
    )
    con.register("lineage_df", lineage_df)
    con.execute("CREATE OR REPLACE TABLE serving.field_lineage AS SELECT * FROM lineage_df")

    alt_ids_df = pd.DataFrame(alternate_ids)
    con.register("alt_ids_df", alt_ids_df)
    con.execute(
        "CREATE OR REPLACE TABLE serving.member_alternate_identifier AS SELECT * FROM alt_ids_df"
    )

    membership_df = pd.DataFrame(membership_rows)
    con.register("membership_df", membership_df)
    con.execute("CREATE OR REPLACE TABLE serving.membership AS SELECT * FROM membership_df")

    review_columns = ["record_key_a", "record_key_b", "score", "status", "run_id"]
    if review_pairs:
        review_df = pd.DataFrame(
            [
                {
                    "record_key_a": a,
                    "record_key_b": b,
                    "score": score,
                    "status": "pending",
                    "run_id": run_id,
                }
                for a, b, score in review_pairs
            ]
        )
    else:
        review_df = pd.DataFrame(columns=review_columns)
    con.register("review_df", review_df)
    con.execute("CREATE OR REPLACE TABLE serving.review_queue AS SELECT * FROM review_df")


def load_fs_params() -> dict:
    import yaml

    from mdm.config import REPO_ROOT

    with (REPO_ROOT / "config" / "fs_params.yml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_nickname_index() -> dict[str, str]:
    import yaml

    from mdm.comparators import build_nickname_index
    from mdm.config import REPO_ROOT

    with (REPO_ROOT / "config" / "nicknames.yml").open("r", encoding="utf-8") as f:
        table = yaml.safe_load(f) or {}
    return build_nickname_index(table)


def load_thresholds() -> dict:
    from mdm.config import load_config

    config = load_config()
    return {
        "upper": config["thresholds"]["upper"],
        "lower": config["thresholds"]["lower"],
        "max_cluster_size": config["clustering"]["max_cluster_size"],
        "min_cluster_density": config["clustering"]["min_cluster_density"],
        "survivorship_rule_chain": config["survivorship"]["rule_chain"],
        "vendor_trust": config["survivorship"]["vendor_trust"],
    }
