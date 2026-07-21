"""Streamlit dashboard (PROJECT_CONSTITUTION.md #5) -- makes results legible to a
non-technical reader. Reads exclusively from a tier's DuckDB (conformance/matching/
serving/quality schemas) and the generated docs/results.md + docs/img/pr_curve.png; never
recomputes matching itself, since scoring 300k+ candidate pairs live on every page load
would make the UI unusable.

    streamlit run dashboard/app.py -- --tier dev

Visual identity (Phase 23+ polish): a dark, analytics-tool theme configured in
.streamlit/config.toml. Chart series colors below are picked from the dataviz skill's
validated categorical slots -- GOLD (#b8860a) and BLUE (#3987e5) pass every CVD/contrast
gate against this dashboard's actual dark card surface (#141a26), checked with
scripts/validate_palette.js, not eyeballed. GOLD doubles as brand chrome (ties to "golden
record", the actual domain term) for non-data UI; BLUE is reserved for the second series
whenever a chart needs one (e.g. naive vs. Fellegi-Sunter), never reused as a third
category.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from mdm.api import DOMAIN_TABLES  # noqa: E402
from mdm.config import VALID_TIERS, load_config  # noqa: E402

# ---------------------------------------------------------------------------------------
# Design tokens -- kept in one place so chart chrome, table badges, and injected CSS never
# drift from each other or from .streamlit/config.toml's UI chrome.
# ---------------------------------------------------------------------------------------
BG = "#0d1119"
SURFACE = "#141a26"
SURFACE_2 = "#1a2233"
BORDER = "#262e42"
TEXT = "#e7eaf2"
TEXT_MUTED = "#9aa3ba"
TEXT_FAINT = "#6c7690"
GOLD = "#b8860a"
GOLD_BRIGHT = "#d9a93d"
BLUE = "#3987e5"
GOOD = "#0ca30c"
WARNING = "#fab219"
SERIOUS = "#ec835a"
CRITICAL = "#d03b3b"

_DOMAIN_LABELS = {
    "medical_history": "Medical history",
    "medical_claims": "Medical claims",
    "pharmacy_claims": "Pharmacy claims",
    "pharmacy_info": "Pharmacy info",
    "lab_results": "Lab results",
}

st.set_page_config(page_title="patient-identity-resolution", layout="wide", page_icon="🧬")


def _inject_css() -> None:
    st.markdown(
        f"""
        <style>
        .block-container {{ padding-top: 2rem; max-width: 1200px; }}
        h1, h2, h3 {{ letter-spacing: -0.01em; }}
        [data-testid="stSidebar"] {{ border-right: 1px solid {BORDER}; }}
        [data-testid="stSidebar"] .block-container {{ padding-top: 1.5rem; }}

        .app-brand {{
            display: flex; align-items: baseline; gap: 0.5rem; margin-bottom: 0.1rem;
        }}
        .app-brand .mark {{ color: {GOLD_BRIGHT}; font-size: 1.05rem; font-weight: 700; }}
        .app-brand .name {{ font-size: 1.05rem; font-weight: 700; color: {TEXT}; }}
        .app-sub {{
            font-size: 0.78rem; color: {TEXT_FAINT}; margin-bottom: 1.4rem; line-height: 1.4;
        }}

        .kpi-row {{ display: flex; gap: 0.7rem; flex-wrap: wrap; margin: 0.3rem 0 1.6rem; }}
        .kpi-tile {{
            flex: 1 1 150px; background: {SURFACE}; border: 1px solid {BORDER};
            border-radius: 10px; padding: 0.9rem 1.05rem;
            box-shadow: 0 1px 2px rgba(0,0,0,0.3);
        }}
        .kpi-tile .v {{
            font-size: 1.65rem; font-weight: 700; color: {TEXT}; line-height: 1.15;
            font-variant-numeric: tabular-nums;
        }}
        .kpi-tile .l {{
            font-size: 0.72rem; color: {TEXT_MUTED}; text-transform: uppercase;
            letter-spacing: 0.04em; margin-top: 0.3rem;
        }}
        .kpi-tile.accent {{ border-color: {GOLD}; }}
        .kpi-tile.accent .v {{ color: {GOLD_BRIGHT}; }}

        .section-label {{
            font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.06em; color: {TEXT_FAINT}; margin: 1.6rem 0 0.5rem;
        }}

        [data-testid="stMetric"] {{
            background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px;
            padding: 0.7rem 1rem;
        }}

        .stTabs [data-baseweb="tab-list"] {{ gap: 0.3rem; border-bottom: 1px solid {BORDER}; }}
        .stTabs [data-baseweb="tab"] {{ font-size: 0.88rem; }}

        div[data-testid="stExpander"], div[data-testid="stVerticalBlockBorderWrapper"] {{
            border-radius: 10px;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _kpi_row(tiles: list[tuple[str, str, bool]]) -> None:
    """tiles: (value, label, accent) -- rendered as a responsive row of stat cards, not
    st.metric's default (plain, low-contrast against a dark background without a card)."""
    cells = "".join(
        f'<div class="kpi-tile{" accent" if accent else ""}"><div class="v">{value}</div>'
        f'<div class="l">{label}</div></div>'
        for value, label, accent in tiles
    )
    st.markdown(f'<div class="kpi-row">{cells}</div>', unsafe_allow_html=True)


def _chart_theme(chart: alt.Chart) -> alt.Chart:
    """Dark chart chrome matching the dashboard's own surface tokens -- transparent
    background (sits on the card underneath it), recessive gridlines, muted axis text.
    Applied to every chart so none of them render with Vega-Lite's light-mode default and
    clash against the dark theme."""
    return (
        chart.configure_view(strokeWidth=0)
        .configure_axis(
            gridColor=BORDER,
            domainColor=BORDER,
            tickColor=BORDER,
            labelColor=TEXT_MUTED,
            titleColor=TEXT_MUTED,
            labelFontSize=11,
            titleFontSize=11,
        )
        .configure_legend(labelColor=TEXT_MUTED, titleColor=TEXT_MUTED)
        .properties(background="transparent")
    )


def _style_column(df: pd.DataFrame, col: str, colors: dict[str, str]):
    """A real colored badge per status value (pandas Styler -> st.dataframe renders actual
    cell background colors), not a plain-text column -- st.dataframe has no native badge
    column type, but does render a Styler's per-cell CSS directly."""

    def _paint(val):
        c = colors.get(val)
        if not c:
            return ""
        return f"background-color: {c}26; color: {c}; font-weight: 600; border-radius: 4px;"

    return df.style.map(_paint, subset=[col])


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
    _inject_css()

    with st.sidebar:
        st.markdown(
            '<div class="app-brand"><span class="mark">◆</span>'
            '<span class="name">patient-identity-resolution</span></div>'
            '<div class="app-sub">Multi-vendor patient MDM — probabilistic record linkage, '
            "six data domains, one patient_global_id.</div>",
            unsafe_allow_html=True,
        )

        tiers = _available_tiers()
        if not tiers:
            st.error(
                "No generated tier found under data/. Run `make pipeline` (or `make "
                "pipeline TIER=ci`) first."
            )
            return
        default_tier = _get_tier_from_args()
        default_index = tiers.index(default_tier) if default_tier in tiers else 0
        tier = st.selectbox("Tier", tiers, index=default_index)

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
    tab_icons = ["◈", "◎", "◫", "◑", "◆", "◇", "◷"]
    tab_labels = [f"{icon}  {name}" for icon, name in zip(tab_icons, tab_names, strict=True)]
    tabs = dict(zip(tab_names, st.tabs(tab_labels), strict=True))

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

    _kpi_row(
        [
            (f"{int(metrics['total_source_records']):,}", "Source records", False),
            (f"{int(metrics['total_golden_records']):,}", "Golden records", True),
            (f"{metrics['dedup_rate']:.1%}", "Dedup rate", False),
            (f"{int(metrics['create_events']):,}", "Create events", False),
            (f"{int(metrics['merge_events']):,}", "Merge events", False),
            (f"{int(metrics['split_events']):,}", "Split events", False),
        ]
    )
    st.caption(f"Computed at {metrics['computed_at']}")

    if _table_exists(db_path, "conformance", "patient_normalized"):
        by_vendor = _query(
            db_path,
            "SELECT source_vendor, count(*) AS records FROM conformance.patient_normalized "
            "GROUP BY 1 ORDER BY 1",
        )
        st.markdown('<div class="section-label">Records by vendor</div>', unsafe_allow_html=True)
        chart = (
            alt.Chart(by_vendor)
            .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=48, color=GOLD)
            .encode(
                x=alt.X("source_vendor:N", title=None, axis=alt.Axis(labelAngle=0)),
                y=alt.Y("records:Q", title="Records"),
                tooltip=[
                    alt.Tooltip("source_vendor:N", title="Vendor"),
                    alt.Tooltip("records:Q", title="Records", format=","),
                ],
            )
            .properties(height=230)
        )
        st.altair_chart(_chart_theme(chart), width="stretch")


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
        with st.container(border=True):
            st.image(str(pr_curve_path), caption="Precision-Recall: Fellegi-Sunter vs. naive")


def _render_blocking(db_path: str) -> None:
    sections = _load_results_sections()
    blocking = _section_for(sections, "Blocking")
    if blocking:
        st.markdown(blocking)

    if not _table_exists(db_path, "matching", "block_stats"):
        return

    block_stats = _query(db_path, "SELECT blocking_pass, record_count FROM matching.block_stats")
    summary = (
        block_stats.groupby("blocking_pass")["record_count"]
        .agg(blocks="count", largest="max", avg_size="mean")
        .reset_index()
        .sort_values("blocking_pass")
    )
    summary["avg_size"] = summary["avg_size"].round(1)

    st.markdown('<div class="section-label">Blocking pass comparison</div>', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        chart = (
            alt.Chart(summary)
            .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=GOLD)
            .encode(
                x=alt.X("blocking_pass:N", title=None, axis=alt.Axis(labelAngle=-20)),
                y=alt.Y("blocks:Q", title="Blocks"),
                tooltip=["blocking_pass", alt.Tooltip("blocks:Q", format=",")],
            )
            .properties(height=220, title="Block count")
        )
        st.altair_chart(_chart_theme(chart), width="stretch")
    with col2:
        chart = (
            alt.Chart(summary)
            .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=BLUE)
            .encode(
                x=alt.X("blocking_pass:N", title=None, axis=alt.Axis(labelAngle=-20)),
                y=alt.Y("largest:Q", title="Largest block"),
                tooltip=["blocking_pass", alt.Tooltip("largest:Q", format=",")],
            )
            .properties(height=220, title="Largest block (skew signal)")
        )
        st.altair_chart(_chart_theme(chart), width="stretch")

    st.dataframe(
        summary.rename(
            columns={
                "blocking_pass": "Pass",
                "blocks": "Blocks",
                "largest": "Largest block",
                "avg_size": "Avg. block size",
            }
        ),
        width="stretch",
        hide_index=True,
    )


REVIEW_STATUS_COLORS = {"pending": WARNING, "confirmed": GOOD, "rejected": CRITICAL}


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

    _kpi_row([(f"{len(review_df):,}", "Gray-zone pairs pending review", len(review_df) > 0)])

    if review_df.empty:
        st.caption(
            "Nothing in the review band at this tier's thresholds — every candidate pair "
            "resolved to auto-match or non-match."
        )
        return

    st.dataframe(_style_column(review_df, "status", REVIEW_STATUS_COLORS), width="stretch")


def _render_golden_records(db_path: str) -> None:
    if not _table_exists(db_path, "serving", "member_360"):
        st.info("No serving.member_360 yet -- run `make pipeline` to completion.")
        return

    # Search-and-select stays exactly what it already was (Phase 9); the only new entry
    # point is a direct patient_global_id lookup, for a caller arriving with an ID already
    # in hand (e.g. from Phase 22's API) rather than a name (PROJECT_CONSTITUTION.md Phase
    # 23).
    pgid_search = st.text_input(
        "Jump to a patient_global_id",
        help="A caller arriving from the Member 360 API already has this ID.",
    )
    if pgid_search.strip():
        member_df = _query(
            db_path,
            "SELECT * FROM serving.member_360 WHERE patient_global_id = ?",
            [pgid_search.strip()],
        )
    else:
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
    st.dataframe(member_df, width="stretch")

    if member_df.empty:
        if pgid_search.strip():
            st.warning(f"No member found for patient_global_id '{pgid_search.strip()}'.")
        return

    selected_pgid = st.selectbox("Inspect", member_df["patient_global_id"])
    _render_member_detail(db_path, selected_pgid)


def _render_member_detail(db_path: str, patient_global_id: str) -> None:
    """One section per domain -- eligibility, field lineage, then every fact/match-path
    domain from Phases 19-20 -- each gracefully empty (not an error) rather than skipped,
    so a member with data in only one or two domains still shows the full shape of what
    *could* be known about them (PROJECT_CONSTITUTION.md Phase 23). Each domain is its own
    bordered container instead of a bare subheader, so the detail view reads as distinct
    cards rather than one long unbroken scroll. Labels stay real st.subheader() calls
    (not styled markdown) since tests/integration/test_dashboard.py asserts on
    tab.subheader values -- AppTest walks the tree through st.container() fine, so the
    card styling and the test contract aren't in tension."""
    with st.container(border=True):
        st.subheader("Eligibility")
        eligibility_df = _query(
            db_path,
            "SELECT a.source_vendor, a.source_record_id, p.first_name, p.last_name, p.dob, "
            "p.gender, p.ssn FROM serving.member_alternate_identifier a "
            "LEFT JOIN conformance.patient_normalized p "
            "ON p.source_vendor = a.source_vendor AND p.source_record_id = a.source_record_id "
            "WHERE a.patient_global_id = ? ORDER BY a.source_vendor",
            [patient_global_id],
        )
        if eligibility_df.empty:
            st.caption("No eligibility records for this person.")
        else:
            st.dataframe(eligibility_df, width="stretch", hide_index=True)

    with st.container(border=True):
        st.subheader("Field lineage")
        lineage_df = _query(
            db_path,
            "SELECT field_name, winning_value, source_vendor, source_record_id, rule_applied "
            "FROM serving.field_lineage WHERE patient_global_id = ?",
            [patient_global_id],
        )
        if lineage_df.empty:
            st.caption("No field lineage for this person.")
        else:
            st.dataframe(lineage_df, width="stretch", hide_index=True)

    for domain, table in DOMAIN_TABLES.items():
        with st.container(border=True):
            st.subheader(_DOMAIN_LABELS[domain])
            if not _table_exists(db_path, "serving", table):
                st.caption(f"serving.{table} not built yet -- run `make pipeline` to completion.")
                continue
            # table comes from DOMAIN_TABLES, a fixed internal dict, never user input.
            domain_df = _query(
                db_path,
                f"SELECT * FROM serving.{table} WHERE patient_global_id = ?",
                [patient_global_id],
            )
            if domain_df.empty:
                st.caption("No records in this domain.")
            else:
                st.dataframe(domain_df, width="stretch", hide_index=True)


def _render_methodology() -> None:
    config = load_config()

    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.markdown("**Blocking passes**")
            passes = pd.DataFrame(config["blocking"]["passes"])
            st.dataframe(passes, width="stretch", hide_index=True)
            st.caption(f"Max block size: {config['blocking']['max_block_size']:,}")

        with st.container(border=True):
            st.markdown("**Clustering guards**")
            _kpi_row(
                [
                    (str(config["clustering"]["max_cluster_size"]), "Max cluster size", False),
                    (f"{config['clustering']['min_cluster_density']:.2f}", "Min density", False),
                ]
            )

    with col2:
        with st.container(border=True):
            st.markdown("**Triage thresholds**")
            _kpi_row(
                [
                    (f"{config['thresholds']['upper']:.4f}", "Upper (auto-match)", True),
                    (f"{config['thresholds']['lower']:.4f}", "Lower (review floor)", False),
                ]
            )

        with st.container(border=True):
            st.markdown("**Survivorship rule chain**")
            st.write(" → ".join(config["survivorship"]["rule_chain"]))

    st.markdown('<div class="section-label">Fellegi-Sunter weights</div>', unsafe_allow_html=True)
    fs_params_path = REPO_ROOT / "config" / "fs_params.yml"
    if fs_params_path.exists():
        with fs_params_path.open("r", encoding="utf-8") as f:
            fs_params = yaml.safe_load(f)
        rows = [
            {"field": field, "level": level, **stats}
            for field, levels in fs_params.items()
            for level, stats in levels.items()
        ]
        weights_df = pd.DataFrame(rows)
        st.dataframe(
            weights_df,
            width="stretch",
            hide_index=True,
            column_config={
                "weight": st.column_config.NumberColumn("weight", format="%.3f"),
            },
        )
    else:
        st.info("No config/fs_params.yml yet -- run `python scripts/estimate_fs_params.py`.")


QUALITY_STATUS_COLORS = {"pass": GOOD, "warn": WARNING, "fail": CRITICAL}


def _render_quality_history(db_path: str) -> None:
    if not _table_exists(db_path, "quality", "validation_runs"):
        st.info("No quality.validation_runs yet -- run `python scripts/run_quality_checks.py`.")
        return

    validation_df = _query(
        db_path, "SELECT * FROM quality.validation_runs ORDER BY checked_at DESC"
    )
    st.dataframe(_style_column(validation_df, "status", QUALITY_STATUS_COLORS), width="stretch")


if __name__ == "__main__":
    main()
