"""Member 360 API (Phase 22, PROJECT_CONSTITUTION.md): write path and read path together
over the full data model built in Phases 0-21. A downstream caller never needs to know
about source vendors, domains, or table structure -- everything is addressed by
patient_global_id.

    POST /resolve                        -- resolve a new record to a patient_global_id
    GET  /members/{patient_global_id}    -- full cross-domain profile (summary + a first
                                             page of each domain)
    GET  /members/{patient_global_id}/{domain} -- paginated drill-down into one domain

    python -m mdm.api --tier dev
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Literal

import duckdb
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from mdm.config import REPO_ROOT, VALID_TIERS
from mdm.pipeline import resolve_new_record

# {domain name in the API} -> {serving.fct_* table} -- domain names match Phase 17-20's
# docs/domain-linking-strategy.md vocabulary, not the underlying table names, so a caller
# never has to know a table even exists.
DOMAIN_TABLES = {
    "medical_history": "fct_medical_history",
    "medical_claims": "fct_medical_claims",
    "pharmacy_claims": "fct_pharmacy_claims",
    "pharmacy_info": "fct_pharmacy_info",
    "lab_results": "fct_lab_results",
}
DEFAULT_PROFILE_DOMAIN_LIMIT = 20
DEFAULT_DRILLDOWN_LIMIT = 50


class MemberRecordIn(BaseModel):
    first_name: str
    last_name: str
    dob: date
    gender: str | None = None
    ssn: str | None = None


class ResolveResponse(BaseModel):
    patient_global_id: str
    status: Literal["matched", "created"]
    score: float | None
    matched_record_key: str | None


def _rows_as_dicts(con: duckdb.DuckDBPyConnection, query: str, params: list) -> list[dict]:
    """Deliberately not `.df()` -- a pandas round-trip hands back numpy/pandas dtypes
    (int64, Timestamp) that aren't natively JSON-serializable, the same pandas/dict boundary
    problem mdm.pipeline.sanitize_nan exists to paper over on the write side. Raw
    `.fetchall()` already returns native Python types (int, float, str, datetime.date,
    None), which FastAPI's response encoding handles directly."""
    cursor = con.execute(query, params)
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]


def create_app(db_path: Path) -> FastAPI:
    """Factory, not a module-level app instance -- tests point this at a temporary
    per-test database rather than sharing one global app/connection across the suite."""
    app = FastAPI(
        title="Member 360 API",
        description=(
            "Resolve a new record to a patient_global_id and fetch that person's full "
            "cross-domain profile -- no knowledge of source vendors, domains, or table "
            "structure required."
        ),
    )

    def _connect() -> duckdb.DuckDBPyConnection:
        if not Path(db_path).exists():
            raise HTTPException(
                status_code=503,
                detail=f"No database at {db_path} -- run the pipeline first (see Makefile).",
            )
        return duckdb.connect(str(db_path), read_only=False)

    @app.post("/resolve", response_model=ResolveResponse)
    def resolve(record: MemberRecordIn) -> ResolveResponse:
        try:
            result = resolve_new_record(str(db_path), record.model_dump())
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ResolveResponse(**result)

    @app.get("/members/{patient_global_id}")
    def get_member(
        patient_global_id: str,
        domain_limit: int = Query(DEFAULT_PROFILE_DOMAIN_LIMIT, ge=1, le=200),
    ) -> dict:
        con = _connect()
        try:
            summary_rows = _rows_as_dicts(
                con,
                "SELECT * FROM serving.member_360 WHERE patient_global_id = ?",
                [patient_global_id],
            )
            if not summary_rows:
                raise HTTPException(status_code=404, detail="patient_global_id not found")
            summary = summary_rows[0]

            domains = {}
            for domain, table in DOMAIN_TABLES.items():
                total = con.execute(
                    f"SELECT count(*) FROM serving.{table} WHERE patient_global_id = ?",
                    [patient_global_id],
                ).fetchone()[0]
                records = _rows_as_dicts(
                    con,
                    f"SELECT * FROM serving.{table} WHERE patient_global_id = ? LIMIT ?",
                    [patient_global_id, domain_limit],
                )
                domains[domain] = {"total": total, "returned": len(records), "records": records}
        finally:
            con.close()

        return {"patient_global_id": patient_global_id, "summary": summary, "domains": domains}

    @app.get("/members/{patient_global_id}/{domain}")
    def get_member_domain(
        patient_global_id: str,
        domain: str,
        limit: int = Query(DEFAULT_DRILLDOWN_LIMIT, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        if domain not in DOMAIN_TABLES:
            raise HTTPException(
                status_code=404,
                detail=f"unknown domain '{domain}' -- valid domains: {sorted(DOMAIN_TABLES)}",
            )
        table = DOMAIN_TABLES[domain]
        con = _connect()
        try:
            total = con.execute(
                f"SELECT count(*) FROM serving.{table} WHERE patient_global_id = ?",
                [patient_global_id],
            ).fetchone()[0]
            records = _rows_as_dicts(
                con,
                f"SELECT * FROM serving.{table} WHERE patient_global_id = ? LIMIT ? OFFSET ?",
                [patient_global_id, limit, offset],
            )
        finally:
            con.close()

        return {
            "patient_global_id": patient_global_id,
            "domain": domain,
            "total": total,
            "limit": limit,
            "offset": offset,
            "records": records,
        }

    return app


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, default="dev")
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    db_path = args.db_path or (REPO_ROOT / "data" / args.tier / "mdm.duckdb")
    app = create_app(db_path)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
