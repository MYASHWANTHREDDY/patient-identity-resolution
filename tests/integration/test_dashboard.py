"""Runs the real Streamlit script via AppTest (no browser needed) against whatever tier
data already exists on disk, and asserts every tab renders without raising."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_PATH = REPO_ROOT / "dashboard" / "app.py"


def _any_tier_generated() -> bool:
    return any((REPO_ROOT / "data" / tier / "mdm.duckdb").exists() for tier in ("ci", "dev"))


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

    golden_records_tab_index = [
        i for i, t in enumerate(at.tabs) if t.text_input
    ]
    if not golden_records_tab_index:
        pytest.skip("no text_input widget found -- golden records tab has no data yet")

    tab = at.tabs[golden_records_tab_index[0]]
    tab.text_input[0].set_value("smith").run()
    assert not at.exception
    assert not list(tab.exception)
