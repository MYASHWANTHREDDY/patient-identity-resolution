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
from uuid import uuid4

import pandas as pd

if TYPE_CHECKING:
    import duckdb

from mdm.clustering import build_clusters, finalize_cluster_membership
from mdm.crosswalk import CrosswalkEntry, mint_id, resolve_crosswalk
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


MATCHPATH_DOMAINS = ("pharmacy_info", "lab_identity")

# Same non-SSN passes as dbt/models/blocking/block_keys.sql, minus bp_ssn -- match-path
# records never carry an ssn (has_ssn_field=False in src/mdm/generator/matchpath.py), so
# that pass would never fire here anyway (docs/domain-linking-strategy.md). Kept as a
# Python dict rather than new dbt models per the Phase 20 design decision: this asymmetric
# join (match-path records against the already-built conformance.patient_normalized) only
# runs once, after run_matching has already produced serving.crosswalk, so it belongs in
# this script-level orchestration rather than dbt's regular build graph.
_MATCHPATH_BLOCKING_PASSES: dict[str, tuple[str, str]] = {
    "bp_dob_lname": (
        "dob is not null and last_name_phonetic is not null",
        "cast(dob as varchar) || '|' || last_name_phonetic",
    ),
    "bp_year_names": (
        "dob_year is not null and first_name_phonetic is not null "
        "and last_name_phonetic is not null",
        "cast(dob_year as varchar) || '|' || first_name_phonetic || '|' || last_name_phonetic",
    ),
    "bp_coarse": (
        "last_name_phonetic is not null and gender is not null and dob_year is not null",
        "last_name_phonetic || '|' || gender || '|' || cast(dob_year as varchar)",
    ),
}


def _matchpath_block_keys_cte(relation: str) -> str:
    parts = [
        f"select record_key, '{pass_name}' as blocking_pass, {key_expr} as block_key "
        f"from {relation} where {where_clause}"
        for pass_name, (where_clause, key_expr) in _MATCHPATH_BLOCKING_PASSES.items()
    ]
    return " union all ".join(parts)


def asymmetric_candidate_pairs(con, domains=MATCHPATH_DOMAINS, *, max_block_size: int):
    """Match-path records (one of `domains`) blocked against conformance.patient_normalized
    -- an asymmetric join, unlike matching.candidate_pairs.sql's self-join, since match-path
    records never merge with each other, only resolve against the core population. Oversized
    blocks (measured on the core side, since that's the population each match-path record's
    block search would otherwise flood) are excluded the same way block_stats.sql excludes
    them from the symmetric case."""
    import pandas as pd

    core_cte = _matchpath_block_keys_cte("conformance.patient_normalized")
    frames = []
    for domain in domains:
        matchpath_cte = _matchpath_block_keys_cte(f"conformance.{domain}_normalized")
        query = f"""
            with core_keys as ({core_cte}),
            matchpath_keys as ({matchpath_cte}),
            block_sizes as (
                select blocking_pass, block_key, count(*) as record_count
                from core_keys group by 1, 2
            )
            select distinct
                '{domain}' as domain,
                m.record_key as matchpath_record_key,
                c.record_key as core_record_key
            from matchpath_keys m
            join core_keys c on m.blocking_pass = c.blocking_pass and m.block_key = c.block_key
            join block_sizes s on s.blocking_pass = c.blocking_pass and s.block_key = c.block_key
            where s.record_count <= {max_block_size}
        """
        frames.append(con.execute(query).df())
    if not frames:
        return pd.DataFrame(columns=["domain", "matchpath_record_key", "core_record_key"])
    return pd.concat(frames, ignore_index=True)


def run_matchpath_matching(
    db_path: str,
    *,
    fs_params: dict | None = None,
    nickname_index: dict[str, str] | None = None,
    max_block_size: int | None = None,
) -> dict:
    """Resolves pharmacy_info/lab_identity records (Phase 20, PROJECT_CONSTITUTION.md) to an
    *existing* patient_global_id via real matching -- unlike run_matching above, this never
    creates new identities or merges clusters, since a match-path record either belongs to
    someone already known to the core population or it doesn't resolve at all (not every
    record is guaranteed a match here, unlike Phase 19's join-path fact domains). Requires
    run_matching to have already populated serving.crosswalk."""
    import duckdb

    from mdm.config import load_config

    fs_params = fs_params if fs_params is not None else load_fs_params()
    nickname_index = nickname_index if nickname_index is not None else load_nickname_index()
    thresholds = load_thresholds()
    if max_block_size is None:
        max_block_size = int(load_config()["blocking"]["max_block_size"])

    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS serving")

        crosswalk_rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'serving' AND table_name = 'crosswalk'"
        ).fetchall()
        if not crosswalk_rows:
            raise RuntimeError(
                "serving.crosswalk does not exist -- run scripts/run_matching.py before "
                "scripts/run_matchpath_matching.py (Phase 20 resolves match-path records "
                "against an already-built core crosswalk, never before it)."
            )
        crosswalk_df = con.execute(
            "SELECT record_key, patient_global_id FROM serving.crosswalk"
        ).df()
        record_to_pgid = dict(
            zip(crosswalk_df["record_key"], crosswalk_df["patient_global_id"], strict=False)
        )

        core_df = con.execute(
            "SELECT record_key, first_name, last_name, dob, gender, ssn FROM "
            "conformance.patient_normalized"
        ).df()
        core_records = core_df.set_index("record_key").to_dict(orient="index")
        for key, record in core_records.items():
            record["record_key"] = key
        sanitize_nan(core_records)

        matchpath_records: dict[str, dict] = {}
        record_domain: dict[str, str] = {}
        for domain in MATCHPATH_DOMAINS:
            df = con.execute(
                f"SELECT record_key, source_record_id, first_name, last_name, dob, gender, "
                f"ssn FROM conformance.{domain}_normalized"
            ).df()
            recs = df.set_index("record_key").to_dict(orient="index")
            for key, record in recs.items():
                record["record_key"] = key
                matchpath_records[key] = record
                record_domain[key] = domain
        sanitize_nan(matchpath_records)

        candidate_pairs = asymmetric_candidate_pairs(con, max_block_size=max_block_size)

        best_auto_match: dict[str, tuple[float, str]] = {}
        best_review: dict[str, tuple[float, str]] = {}
        for row in candidate_pairs.itertuples(index=False):
            mp_key = row.matchpath_record_key
            core_key = row.core_record_key
            agreement = compare_record_pair(
                matchpath_records[mp_key], core_records[core_key], nickname_index=nickname_index
            )
            score = score_fs(agreement, fs_params)
            decision = decide(score, upper=thresholds["upper"], lower=thresholds["lower"])
            if decision == AUTO_MATCH:
                current = best_auto_match.get(mp_key)
                if current is None or score > current[0]:
                    best_auto_match[mp_key] = (score, core_key)
            elif decision == REVIEW:
                current = best_review.get(mp_key)
                if current is None or score > current[0]:
                    best_review[mp_key] = (score, core_key)

        resolution_rows = []
        for mp_key, (score, core_key) in best_auto_match.items():
            pgid = record_to_pgid.get(core_key)
            if pgid is None:
                continue  # defensive: every core record should have a crosswalk entry
            resolution_rows.append(
                {
                    "domain": record_domain[mp_key],
                    "record_key": mp_key,
                    "source_record_id": matchpath_records[mp_key]["source_record_id"],
                    "patient_global_id": pgid,
                    "matched_core_record_key": core_key,
                    "match_score": score,
                }
            )

        # Only records that never cleared auto_match go to review -- an auto-matched record
        # doesn't also need human review (mirrors run_matching's review_pairs, which likewise
        # only ever holds pairs decide() called REVIEW, never AUTO_MATCH pairs).
        review_rows = [
            {
                "domain": record_domain[mp_key],
                "record_key": mp_key,
                "candidate_core_record_key": core_key,
                "score": score,
                "status": "pending",
            }
            for mp_key, (score, core_key) in best_review.items()
            if mp_key not in best_auto_match
        ]

        _write_matchpath_tables(con, resolution_rows, review_rows)

        num_matchpath_records = len(matchpath_records)
        return {
            "num_matchpath_records": num_matchpath_records,
            "num_candidate_pairs": len(candidate_pairs),
            "num_auto_matched": len(resolution_rows),
            "num_review": len(review_rows),
            "num_unmatched": num_matchpath_records - len(resolution_rows) - len(review_rows),
        }
    finally:
        con.close()


def _write_matchpath_tables(con, resolution_rows: list[dict], review_rows: list[dict]) -> None:
    resolution_columns = [
        "domain",
        "record_key",
        "source_record_id",
        "patient_global_id",
        "matched_core_record_key",
        "match_score",
    ]
    resolution_df = (
        pd.DataFrame(resolution_rows)[resolution_columns]
        if resolution_rows
        else pd.DataFrame(columns=resolution_columns)
    )
    con.register("matchpath_resolution_df", resolution_df)
    con.execute(
        "CREATE OR REPLACE TABLE serving.matchpath_resolution AS "
        "SELECT * FROM matchpath_resolution_df"
    )

    review_columns = ["domain", "record_key", "candidate_core_record_key", "score", "status"]
    review_df = (
        pd.DataFrame(review_rows)[review_columns]
        if review_rows
        else pd.DataFrame(columns=review_columns)
    )
    con.register("matchpath_review_df", review_df)
    con.execute(
        "CREATE OR REPLACE TABLE serving.matchpath_review_queue AS "
        "SELECT * FROM matchpath_review_df"
    )


# Phase 22 (Member 360 API): a hand-rolled DuckDB expression identical to
# dbt/macros/phonetic_key.sql's DuckDB branch -- computing a phonetic key for a single ad-hoc
# input record (not yet a table row) is exactly the kind of one-off, non-batch SQL the Phase
# 20 design decision already accepted doing directly in Python rather than through dbt (see
# _MATCHPATH_BLOCKING_PASSES above). Only the DuckDB branch is needed: this API only ever
# runs against the local ci/dev tier backend, never BigQuery.
def _phonetic_key_expr(col: str) -> str:
    cleaned = f"regexp_replace(upper({col}), '[^A-Z]', '', 'g')"
    coded = f"translate({cleaned}, 'AEIOUHWYBFPVCGJKQSXZDTLMNR', '00000000111122222222334556')"
    expr = coded
    for _pass in range(3):
        for digit in "0123456":
            expr = f"replace({expr}, '{digit}{digit}', '{digit}')"
    digits_only = f"replace(substr({expr}, 2), '0', '')"
    return (
        f"case when {cleaned} is null or {cleaned} = '' then null "
        f"else substr({cleaned}, 1, 1) || rpad(substr({digits_only}, 1, 3), 3, '0') end"
    )


def compute_phonetic_key(con, value: str | None) -> str | None:
    if not value:
        return None
    row = con.execute(
        f"SELECT {_phonetic_key_expr('val')} FROM (SELECT ? AS val) t", [value]
    ).fetchone()
    return row[0] if row else None


def _normalize_input_record(record: dict) -> dict:
    """Mirrors dbt/models/staging/stg_vendor_*.sql's Layer 1 -> Layer 2 normalization
    (uppercase/trim names, collapse gender to M/F/U, digits-only ssn) for a record that
    never passed through a staging model -- it arrived directly via the API, not a vendor
    feed."""
    first_name = (record.get("first_name") or "").strip().upper() or None
    last_name = (record.get("last_name") or "").strip().upper() or None
    gender_raw = (record.get("gender") or "").strip().upper()
    gender = gender_raw if gender_raw in ("M", "F") else "U"
    ssn_raw = record.get("ssn")
    ssn = re.sub(r"\D", "", ssn_raw) if ssn_raw else None
    return {
        "first_name": first_name,
        "last_name": last_name,
        "dob": record.get("dob"),
        "gender": gender,
        "ssn": ssn or None,
    }


def _find_candidate_records(con, normalized: dict) -> list[dict]:
    """The same four blocking passes as dbt/models/blocking/block_keys.sql
    (config/matching.yml's blocking.passes), run directly against
    conformance.patient_normalized for one ad-hoc record instead of a self-join -- an
    asymmetric lookup, same idea as Phase 20's asymmetric_candidate_pairs, just for a single
    record instead of a batch of match-path domains."""
    first_name_phonetic = compute_phonetic_key(con, normalized["first_name"])
    last_name_phonetic = compute_phonetic_key(con, normalized["last_name"])
    dob_year = normalized["dob"].year if normalized["dob"] else None

    cursor = con.execute(
        "SELECT record_key, first_name, last_name, dob, gender, ssn "
        "FROM conformance.patient_normalized "
        "WHERE (? IS NOT NULL AND ssn IS NOT NULL AND ssn = ?) "
        "OR (dob is not null AND dob = ? AND last_name_phonetic = ?) "
        "OR (dob_year = ? AND first_name_phonetic = ? AND last_name_phonetic = ?) "
        "OR (last_name_phonetic = ? AND gender = ? AND dob_year = ?)",
        [
            normalized["ssn"],
            normalized["ssn"],
            normalized["dob"],
            last_name_phonetic,
            dob_year,
            first_name_phonetic,
            last_name_phonetic,
            last_name_phonetic,
            normalized["gender"],
            dob_year,
        ],
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


def resolve_new_record(
    db_path: str,
    record: dict,
    *,
    run_id: str | None = None,
    fs_params: dict | None = None,
    nickname_index: dict[str, str] | None = None,
) -> dict:
    """Phase 22: the API's write path. `record` needs first_name/last_name/dob/gender, ssn
    optional -- blocked and scored against conformance.patient_normalized with the same
    comparator/Fellegi-Sunter/triage pipeline every other matching path in this project
    reuses. An AUTO_MATCH resolves to that existing person's patient_global_id via
    serving.crosswalk; anything less confident (REVIEW or no candidates at all) mints a
    brand-new identity -- the same conservative default finalize_cluster_membership gives
    every record untouched by an auto-match edge in the batch pipeline (never merge without
    confidence).

    Writes only to the serving layer (crosswalk, member_demographics, membership,
    member_alternate_identifier) -- this is a speed-layer overlay on top of the
    batch-computed golden population, not a write into raw_standard/conformance. A genuinely
    new person created here is visible immediately (member_360 is a live view), but is only
    as durable as the *next* full run_matching(): that function rebuilds serving.crosswalk
    from conformance.patient_normalized's record_keys only, so an API-minted
    record_key (which was never added to a vendor feed) has no representation there and
    won't be carried forward. A real system would feed the record back through the batch
    layer to make it permanent; this API is the resolve/read layer, not the ingestion path.
    """
    import duckdb

    run_id = run_id or datetime.now(UTC).isoformat()
    fs_params = fs_params if fs_params is not None else load_fs_params()
    nickname_index = nickname_index if nickname_index is not None else load_nickname_index()
    thresholds = load_thresholds()

    normalized = _normalize_input_record(record)

    con = duckdb.connect(db_path)
    try:
        crosswalk_rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'serving' AND table_name = 'crosswalk'"
        ).fetchall()
        if not crosswalk_rows:
            raise RuntimeError(
                "serving.crosswalk does not exist -- run scripts/run_matching.py before "
                "resolving records through the API."
            )

        candidates = _find_candidate_records(con, normalized)
        best_score: float | None = None
        best_record_key: str | None = None
        for candidate in candidates:
            agreement = compare_record_pair(normalized, candidate, nickname_index=nickname_index)
            score = score_fs(agreement, fs_params)
            if best_score is None or score > best_score:
                best_score, best_record_key = score, candidate["record_key"]

        decision = (
            decide(best_score, upper=thresholds["upper"], lower=thresholds["lower"])
            if best_score is not None
            else None
        )

        if decision == AUTO_MATCH:
            pgid_row = con.execute(
                "SELECT patient_global_id FROM serving.crosswalk WHERE record_key = ?",
                [best_record_key],
            ).fetchone()
            if pgid_row is None:
                raise RuntimeError(
                    f"matched core record {best_record_key} has no crosswalk entry -- "
                    "run scripts/run_matching.py first"
                )
            return {
                "patient_global_id": pgid_row[0],
                "status": "matched",
                "score": best_score,
                "matched_record_key": best_record_key,
            }

        existing_ids = con.execute("SELECT patient_global_id FROM serving.crosswalk").fetchall()
        next_sequence = next_crosswalk_sequence(
            {
                f"seed{i}": CrosswalkEntry(f"seed{i}", pgid, run_id, run_id)
                for i, (pgid,) in enumerate(existing_ids)
            }
        )
        pgid = mint_id(next_sequence)
        source_record_id = uuid4().hex
        # "API:{uuid}", the same {vendor}:{source_record_id} shape every other record_key in
        # this project uses (e.g. "VENDOR_A:00000000") -- built from source_record_id below,
        # not duplicated into it, so member_360's alternate_identifiers array (which
        # reconstructs source_vendor || ':' || source_record_id) doesn't show "API:API:...".
        record_key = f"API:{source_record_id}"
        ssn_last4 = normalized["ssn"][-4:] if normalized["ssn"] else None

        con.execute(
            "INSERT INTO serving.crosswalk (record_key, patient_global_id, first_seen_run, "
            "last_seen_run) VALUES (?, ?, ?, ?)",
            [record_key, pgid, run_id, run_id],
        )
        con.execute(
            "INSERT INTO serving.member_demographics (patient_global_id, first_name, "
            "last_name, dob, gender, ssn_last4) VALUES (?, ?, ?, ?, ?, ?)",
            [
                pgid,
                normalized["first_name"],
                normalized["last_name"],
                normalized["dob"],
                normalized["gender"],
                ssn_last4,
            ],
        )
        con.execute(
            "INSERT INTO serving.membership (patient_global_id, source_record_count) "
            "VALUES (?, ?)",
            [pgid, 1],
        )
        con.execute(
            "INSERT INTO serving.member_alternate_identifier (patient_global_id, "
            "source_vendor, source_record_id) VALUES (?, ?, ?)",
            [pgid, "API", source_record_id],
        )

        return {
            "patient_global_id": pgid,
            "status": "created",
            "score": best_score,
            "matched_record_key": None,
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
