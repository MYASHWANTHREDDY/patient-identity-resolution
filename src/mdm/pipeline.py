"""Orchestrates scoring -> triage -> clustering -> crosswalk -> survivorship into the
serving tables (PROJECT_CONSTITUTION.md #5, the "SERVING" stage of the architecture).
Pure functions live in scoring.py/triage.py/clustering.py/crosswalk.py/survivorship.py;
this module is the only place that sequences them and touches DuckDB.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import duckdb
import pandas as pd

from mdm.clustering import build_clusters, finalize_cluster_membership
from mdm.crosswalk import CrosswalkEntry, resolve_crosswalk
from mdm.scoring import compare_record_pair, score_fs
from mdm.survivorship import FIELDS as SURVIVORSHIP_FIELDS
from mdm.survivorship import build_golden_record
from mdm.triage import AUTO_MATCH, decide

_PGID_PATTERN = re.compile(r"^PGID(\d+)$")


def _sanitize_nan(records_by_key: dict[str, dict]) -> None:
    """pandas' DataFrame.to_dict silently turns a SQL NULL into float('nan') (or NaT)
    instead of leaving it None -- the same failure mode fixed once already in
    comparators.is_missing. Sanitizing once here, at the pandas/dict boundary, means
    every pure function downstream (comparators, survivorship) can trust `None` as the
    only "missing" representation instead of each needing its own NaN guard."""
    for record in records_by_key.values():
        for key, value in record.items():
            if value != value:  # True only for NaN/NaT, the self-inequality trick
                record[key] = None


def _next_sequence(existing: dict[str, CrosswalkEntry]) -> int:
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
    run_id = run_id or datetime.now(UTC).isoformat()
    fs_params = fs_params if fs_params is not None else _load_fs_params()
    nickname_index = nickname_index if nickname_index is not None else _load_nickname_index()

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
        thresholds = _load_thresholds()

        records_by_key = patient_normalized.set_index("record_key").to_dict(orient="index")
        for key, record in records_by_key.items():
            record["record_key"] = key
        _sanitize_nan(records_by_key)

        auto_match_edges = []
        for record_key_a, record_key_b in zip(
            candidate_pairs["record_key_a"], candidate_pairs["record_key_b"], strict=False
        ):
            agreement = compare_record_pair(
                records_by_key[record_key_a],
                records_by_key[record_key_b],
                nickname_index=nickname_index,
            )
            score = score_fs(agreement, fs_params)
            if decide(score, upper=thresholds["upper"], lower=thresholds["lower"]) == AUTO_MATCH:
                auto_match_edges.append((record_key_a, record_key_b))

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
        next_sequence = _next_sequence(existing_crosswalk)
        new_crosswalk, identity_events = resolve_crosswalk(
            existing_crosswalk, membership, run_id=run_id, next_sequence=next_sequence
        )

        golden_records, field_lineage_rows, alternate_ids, membership_rows = _build_serving_tables(
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
        )

        return {
            "run_id": run_id,
            "num_records": len(records_by_key),
            "num_auto_match_edges": len(auto_match_edges),
            "num_clusters": len(clusters),
            "num_flagged_clusters": sum(1 for c in clusters if c.flagged),
            "num_golden_records": len(golden_records),
            "num_identity_events": len(identity_events),
        }
    finally:
        con.close()


def _build_serving_tables(new_crosswalk, records_by_key, thresholds):
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
    con.register("events_df", events_df)
    con.execute("CREATE OR REPLACE TABLE serving.identity_events AS SELECT * FROM events_df")

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


def _load_fs_params() -> dict:
    import yaml

    from mdm.config import REPO_ROOT

    with (REPO_ROOT / "config" / "fs_params.yml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_nickname_index() -> dict[str, str]:
    import yaml

    from mdm.comparators import build_nickname_index
    from mdm.config import REPO_ROOT

    with (REPO_ROOT / "config" / "nicknames.yml").open("r", encoding="utf-8") as f:
        table = yaml.safe_load(f) or {}
    return build_nickname_index(table)


def _load_thresholds() -> dict:
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
