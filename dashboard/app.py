"""Streamlit dashboard (PROJECT_CONSTITUTION.md #5) -- makes results legible to a
non-technical reader. Reads exclusively from a tier's DuckDB (conformance/matching/
serving/quality schemas) and the generated docs/results.md + docs/img/pr_curve.png; never
recomputes matching itself, since scoring 300k+ candidate pairs live on every page load
would make the UI unusable.

    streamlit run dashboard/app.py -- --tier dev
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from mdm.config import VALID_TIERS, load_config  # noqa: E402

st.set_page_config(page_title="patient-dedup-system", layout="wide")


def _available_tiers() -> list[str]:
    return [t for t in VALID_TIERS if (REPO_ROOT / "data" / t / "mdm.duckdb").exists()]


def _get_tier_from_args() -> str | None:
    if "--tier" in sys.argv:
        idx = sys.argv.index("--tier")
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


@st.cache_resource
def _connect(db_path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path, read_only=True)


@st.cache_data
def _query(db_path: str, sql: str, params: list | None = None) -> pd.DataFrame:
    con = _connect(db_path)
    return con.execute(sql, params or []).df()


@st.cache_data
def _table_exists(db_path: str, schema: str, table: str) -> bool:
    con = _connect(db_path)
    rows = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchall()
    return bool(rows)


@st.cache_data
def _load_results_sections() -> dict[str, str]:
    results_path = REPO_ROOT / "docs" / "results.md"
    if not results_path.exists():
        return {}
    text = results_path.read_text(encoding="utf-8")
    parts = re.split(r"\n(?=## )", text)
    sections: dict[str, str] = {}
    for part in parts:
        if part.startswith("## "):
            title = part.splitlines()[0][3:].strip()
            sections[title] = part
    return sections


def _section_for(sections: dict[str, str], prefix: str) -> str | None:
    for title, body in sections.items():
        if title.startswith(prefix):
            return body
    return None


def main() -> None:
    st.title("patient-dedup-system")
    st.caption(
        "Three-vendor patient MDM: conformance -> blocking -> Fellegi-Sunter scoring -> "
        "clustering -> golden records."
    )

    tiers = _available_tiers()
    if not tiers:
        st.error(
            "No generated tier found under data/. Run `make pipeline` (or `make pipeline "
            "TIER=ci`) first."
        )
        return

    default_tier = _get_tier_from_args()
    default_index = tiers.index(default_tier) if default_tier in tiers else 0
    tier = st.sidebar.selectbox("Tier", tiers, index=default_index)
    db_path = str(REPO_ROOT / "data" / tier / "mdm.duckdb")

    tab_names = [
        "Overview",
        "Match quality",
        "Blocking & skew",
        "Review queue",
        "Golden records",
        "Methodology",
        "Quality history",
    ]
    tabs = dict(zip(tab_names, st.tabs(tab_names), strict=True))

    with tabs["Overview"]:
        _render_overview(db_path)
    with tabs["Match quality"]:
        _render_match_quality(db_path)
    with tabs["Blocking & skew"]:
        _render_blocking(db_path)
    with tabs["Review queue"]:
        _render_review_queue(db_path)
    with tabs["Golden records"]:
        _render_golden_records(db_path)
    with tabs["Methodology"]:
        _render_methodology()
    with tabs["Quality history"]:
        _render_quality_history(db_path)


def _render_overview(db_path: str) -> None:
    if not _table_exists(db_path, "serving", "agg_dedup_metrics"):
        st.info("No serving.agg_dedup_metrics yet -- run `make pipeline` to completion.")
        return

    metrics = _query(db_path, "SELECT * FROM serving.agg_dedup_metrics").iloc[0]

    col1, col2, col3 = st.columns(3)
    col1.metric("Source records", f"{int(metrics['total_source_records']):,}")
    col2.metric("Golden records", f"{int(metrics['total_golden_records']):,}")
    col3.metric("Dedup rate", f"{metrics['dedup_rate']:.1%}")

    col4, col5, col6 = st.columns(3)
    col4.metric("Create events", int(metrics["create_events"]))
    col5.metric("Merge events", int(metrics["merge_events"]))
    col6.metric("Split events", int(metrics["split_events"]))

    st.caption(f"Computed at {metrics['computed_at']}")

    if _table_exists(db_path, "conformance", "patient_normalized"):
        by_vendor = _query(
            db_path,
            "SELECT source_vendor, count(*) AS records FROM conformance.patient_normalized "
            "GROUP BY 1 ORDER BY 1",
        )
        st.subheader("Records by vendor")
        st.bar_chart(by_vendor.set_index("source_vendor"))


def _render_match_quality(db_path: str) -> None:
    sections = _load_results_sections()
    baseline = _section_for(sections, "Deterministic baseline")
    scoring = _section_for(sections, "Scoring")

    if not baseline and not scoring:
        st.info("No docs/results.md yet -- run `python -m mdm.evaluate --tier <tier>`.")
        return

    if baseline:
        st.markdown(baseline)
    if scoring:
        st.markdown(scoring)

    pr_curve_path = REPO_ROOT / "docs" / "img" / "pr_curve.png"
    if pr_curve_path.exists():
        st.image(str(pr_curve_path), caption="Precision-Recall: Fellegi-Sunter vs. naive")


def _render_blocking(db_path: str) -> None:
    sections = _load_results_sections()
    blocking = _section_for(sections, "Blocking")
    if blocking:
        st.markdown(blocking)

    if _table_exists(db_path, "matching", "block_stats"):
        block_stats = _query(
            db_path, "SELECT blocking_pass, record_count FROM matching.block_stats"
        )
        st.subheader("Block size distribution")
        for pass_name in sorted(block_stats["blocking_pass"].unique()):
            subset = block_stats[block_stats["blocking_pass"] == pass_name]
            largest = subset["record_count"].max()
            st.caption(f"{pass_name} -- {len(subset)} blocks, largest {largest}")
            st.bar_chart(subset["record_count"].value_counts().sort_index())


def _render_review_queue(db_path: str) -> None:
    if not _table_exists(db_path, "serving", "review_queue"):
        st.info("No serving.review_queue yet -- run `make pipeline` to completion.")
        return

    review_df = _query(
        db_path,
        """
        SELECT
            rq.record_key_a, a.first_name AS a_first_name, a.last_name AS a_last_name,
            a.dob AS a_dob, a.source_vendor AS a_vendor,
            rq.record_key_b, b.first_name AS b_first_name, b.last_name AS b_last_name,
            b.dob AS b_dob, b.source_vendor AS b_vendor,
            rq.score, rq.status
        FROM serving.review_queue rq
        JOIN conformance.patient_normalized a ON a.record_key = rq.record_key_a
        JOIN conformance.patient_normalized b ON b.record_key = rq.record_key_b
        ORDER BY rq.score DESC
        """,
    )
    st.write(f"{len(review_df)} gray-zone pairs pending review.")
    st.dataframe(review_df, width='stretch')


def _render_golden_records(db_path: str) -> None:
    if not _table_exists(db_path, "serving", "member_360"):
        st.info("No serving.member_360 yet -- run `make pipeline` to completion.")
        return

    search = st.text_input("Search by last name")
    if search:
        query = (
            "SELECT * FROM serving.member_360 WHERE upper(last_name) LIKE upper(?) "
            "ORDER BY source_record_count DESC LIMIT 200"
        )
        member_df = _query(db_path, query, [f"%{search}%"])
    else:
        query = "SELECT * FROM serving.member_360 ORDER BY source_record_count DESC LIMIT 200"
        member_df = _query(db_path, query)
    st.dataframe(member_df, width='stretch')

    if not member_df.empty:
        selected_pgid = st.selectbox("Inspect field lineage for", member_df["patient_global_id"])
        lineage_df = _query(
            db_path,
            "SELECT field_name, winning_value, source_vendor, source_record_id, rule_applied "
            "FROM serving.field_lineage WHERE patient_global_id = ?",
            [selected_pgid],
        )
        st.dataframe(lineage_df, width='stretch')


def _render_methodology() -> None:
    st.subheader("Blocking passes")
    config = load_config()
    st.json(config["blocking"])

    st.subheader("Triage thresholds")
    st.json(config["thresholds"])

    st.subheader("Clustering guards")
    st.json(config["clustering"])

    st.subheader("Fellegi-Sunter weights")
    fs_params_path = REPO_ROOT / "config" / "fs_params.yml"
    if fs_params_path.exists():
        with fs_params_path.open("r", encoding="utf-8") as f:
            fs_params = yaml.safe_load(f)
        rows = [
            {"field": field, "level": level, **stats}
            for field, levels in fs_params.items()
            for level, stats in levels.items()
        ]
        st.dataframe(pd.DataFrame(rows), width='stretch')
    else:
        st.info("No config/fs_params.yml yet -- run `python scripts/estimate_fs_params.py`.")


def _render_quality_history(db_path: str) -> None:
    if not _table_exists(db_path, "quality", "validation_runs"):
        st.info("No quality.validation_runs yet -- run `python scripts/run_quality_checks.py`.")
        return

    validation_df = _query(
        db_path, "SELECT * FROM quality.validation_runs ORDER BY checked_at DESC"
    )
    st.dataframe(validation_df, width='stretch')


if __name__ == "__main__":
    main()
