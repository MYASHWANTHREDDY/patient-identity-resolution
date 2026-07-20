"""Runs the real Streamlit script via AppTest (no browser needed) against whatever tier
data already exists on disk, and asserts every tab renders without raising."""

from pathlib import Path

import duckdb
import pytest
from streamlit.testing.v1 import AppTest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = REPO_ROOT / "dashboard" / "app.py"


def _any_tier_generated() -> bool:
    return any((REPO_ROOT / "data" / tier / "mdm.duckdb").exists() for tier in ("ci", "dev"))


def _first_generated_tier_db() -> Path | None:
    for tier in ("ci", "dev"):
        db_path = REPO_ROOT / "data" / tier / "mdm.duckdb"
        if db_path.exists():
            return db_path
    return None


def _golden_records_tab(at: AppTest):
    for tab in at.tabs:
        if tab.text_input:
            return tab
    return None


@pytest.mark.skipif(not _any_tier_generated(), reason="no generated tier data on disk")
def test_dashboard_renders_all_tabs_without_error():
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()

    assert not at.exception
    assert len(at.tabs) == 7
    for tab in at.tabs:
        assert not list(tab.exception)


@pytest.mark.skipif(not _any_tier_generated(), reason="no generated tier data on disk")
def test_dashboard_golden_records_search_does_not_raise():
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()

    tab = _golden_records_tab(at)
    if tab is None:
        pytest.skip("no text_input widget found -- golden records tab has no data yet")

    tab.text_input[0].set_value("smith").run()
    assert not at.exception
    assert not list(tab.exception)


@pytest.mark.skipif(not _any_tier_generated(), reason="no generated tier data on disk")
def test_dashboard_jump_to_patient_global_id_surfaces_every_domain_section():
    """Phase 23 exit criteria: searching for any member surfaces every domain's data for
    that person in one place, with lineage. Uses a real patient_global_id already present
    in whichever tier the dashboard defaults to (mirrors _available_tiers()' ordering)."""
    db_path = _first_generated_tier_db()
    con = duckdb.connect(str(db_path), read_only=True)
    pgid = con.execute("SELECT patient_global_id FROM serving.member_360 LIMIT 1").fetchone()
    con.close()
    if pgid is None:
        pytest.skip("serving.member_360 has no rows yet")
    pgid = pgid[0]

    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()

    tab = _golden_records_tab(at)
    if tab is None:
        pytest.skip("no text_input widget found -- golden records tab has no data yet")

    # The first text_input is the new "Jump to a patient_global_id" field (Phase 23),
    # ahead of the pre-existing "Search by last name" field in render order.
    tab.text_input[0].set_value(pgid).run()
    assert not at.exception

    # .run() reruns the whole script -- the pre-rerun `tab` reference is now stale (its
    # elements belong to the previous tree), so every element list it exposed needs
    # re-fetching from the fresh tree before asserting on rendered content.
    tab = _golden_records_tab(at)
    assert not list(tab.exception)

    subheaders = [s.value for s in tab.subheader]
    assert "Eligibility" in subheaders
    assert "Field lineage" in subheaders
    for label in (
        "Medical history",
        "Medical claims",
        "Pharmacy claims",
        "Pharmacy info",
        "Lab results",
    ):
        assert label in subheaders

    # A selectbox to inspect the matched row must appear once the direct-ID search returns
    # exactly the one member being searched for.
    assert tab.selectbox
    assert list(tab.selectbox[0].options) == [pgid]


@pytest.mark.skipif(not _any_tier_generated(), reason="no generated tier data on disk")
def test_dashboard_jump_to_unknown_patient_global_id_warns_gracefully():
    at = AppTest.from_file(str(APP_PATH), default_timeout=60)
    at.run()

    tab = _golden_records_tab(at)
    if tab is None:
        pytest.skip("no text_input widget found -- golden records tab has no data yet")

    tab.text_input[0].set_value("PGID999999999999_NOT_REAL").run()
    assert not at.exception

    tab = _golden_records_tab(at)
    assert not list(tab.exception)
    assert list(tab.warning)
