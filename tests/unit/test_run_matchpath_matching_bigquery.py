import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from mdm.crosswalk import CrosswalkEntry  # noqa: E402
from run_matchpath_matching_bigquery import (  # noqa: E402
    domain_for_record_key,
    run_matchpath_matching_bigquery,
)

EXISTING_CROSSWALK = {
    "VENDOR_A:1": CrosswalkEntry("VENDOR_A:1", "PGID000000000001", "run0", "run0"),
    "VENDOR_A:2": CrosswalkEntry("VENDOR_A:2", "PGID000000000002", "run0", "run0"),
}


def test_domain_for_record_key_recognizes_both_match_path_domains():
    assert domain_for_record_key("VENDOR_B_PHARMACY:PHARM1-00000001") == "pharmacy_info"
    assert domain_for_record_key("VENDOR_D:LABD1-00000001") == "lab_identity"


def test_domain_for_record_key_rejects_a_core_record_key():
    with pytest.raises(ValueError, match="doesn't start with"):
        domain_for_record_key("VENDOR_A:00000001")


def _patched(**overrides):
    defaults = dict(
        read_best_matchpath_auto_matches=lambda client, project, upper: pd.DataFrame(
            columns=["record_key_a", "record_key_b", "score"]
        ),
        read_best_matchpath_review_candidates=lambda client, project, lower, upper: pd.DataFrame(
            columns=["record_key_a", "record_key_b", "score"]
        ),
        read_existing_crosswalk=lambda client, project: EXISTING_CROSSWALK,
    )
    defaults.update(overrides)
    return defaults


@pytest.fixture
def mock_bigquery_client():
    with patch("google.cloud.bigquery.Client") as mock_client_cls:
        yield mock_client_cls


def _run(patches):
    write_calls = []
    with (
        patch(
            "run_matchpath_matching_bigquery.read_best_matchpath_auto_matches",
            patches["read_best_matchpath_auto_matches"],
        ),
        patch(
            "run_matchpath_matching_bigquery.read_best_matchpath_review_candidates",
            patches["read_best_matchpath_review_candidates"],
        ),
        patch(
            "run_matchpath_matching_bigquery.read_existing_crosswalk",
            patches["read_existing_crosswalk"],
        ),
        patch(
            "run_matchpath_matching_bigquery.write_matchpath_tables",
            side_effect=lambda client, project, resolution_rows, review_rows: write_calls.append(
                (resolution_rows, review_rows)
            ),
        ),
    ):
        summary = run_matchpath_matching_bigquery("my-project", fs_params={}, nickname_index={})
    return summary, write_calls


def test_auto_matched_record_resolves_to_existing_pgid(mock_bigquery_client):
    auto = pd.DataFrame(
        [
            {
                "record_key_a": "VENDOR_B_PHARMACY:PHARM1-1",
                "record_key_b": "VENDOR_A:1",
                "score": 30.0,
            }
        ]
    )
    summary, write_calls = _run(
        _patched(read_best_matchpath_auto_matches=lambda client, project, upper: auto)
    )

    assert summary["num_auto_matched"] == 1
    assert summary["num_review"] == 0
    (resolution_rows, review_rows) = write_calls[0]
    assert resolution_rows == [
        {
            "domain": "pharmacy_info",
            "record_key": "VENDOR_B_PHARMACY:PHARM1-1",
            "source_record_id": "PHARM1-1",
            "patient_global_id": "PGID000000000001",
            "matched_core_record_key": "VENDOR_A:1",
            "match_score": 30.0,
        }
    ]
    assert review_rows == []


def test_review_candidate_excluded_once_the_same_record_auto_matches_elsewhere(
    mock_bigquery_client,
):
    """A match-path record can appear in both the auto-match and review query results (e.g.
    its best candidate cleared the upper threshold, but a second, lower-scoring candidate
    also happened to land in the review band) -- only the auto-match should survive, the
    same "auto-matched records never also need review" invariant
    mdm.pipeline.run_matchpath_matching enforces locally."""
    auto = pd.DataFrame(
        [{"record_key_a": "VENDOR_D:LABD1-1", "record_key_b": "VENDOR_A:1", "score": 30.0}]
    )
    review = pd.DataFrame(
        [{"record_key_a": "VENDOR_D:LABD1-1", "record_key_b": "VENDOR_A:2", "score": 10.0}]
    )
    summary, write_calls = _run(
        _patched(
            read_best_matchpath_auto_matches=lambda client, project, upper: auto,
            read_best_matchpath_review_candidates=lambda client, project, lower, upper: review,
        )
    )

    assert summary["num_auto_matched"] == 1
    assert summary["num_review"] == 0
    resolution_rows, review_rows = write_calls[0]
    assert review_rows == []


def test_review_only_record_is_written_to_the_review_queue(mock_bigquery_client):
    review = pd.DataFrame(
        [{"record_key_a": "VENDOR_D:LABD1-1", "record_key_b": "VENDOR_A:2", "score": 9.6}]
    )
    summary, write_calls = _run(
        _patched(read_best_matchpath_review_candidates=lambda client, project, lower, upper: review)
    )

    assert summary["num_auto_matched"] == 0
    assert summary["num_review"] == 1
    resolution_rows, review_rows = write_calls[0]
    assert resolution_rows == []
    assert review_rows == [
        {
            "domain": "lab_identity",
            "record_key": "VENDOR_D:LABD1-1",
            "candidate_core_record_key": "VENDOR_A:2",
            "score": 9.6,
            "status": "pending",
        }
    ]


def test_auto_matched_record_with_no_crosswalk_entry_is_skipped_defensively(mock_bigquery_client):
    auto = pd.DataFrame(
        [
            {
                "record_key_a": "VENDOR_B_PHARMACY:PHARM1-1",
                "record_key_b": "VENDOR_A:not_in_crosswalk",
                "score": 30.0,
            }
        ]
    )
    summary, write_calls = _run(
        _patched(read_best_matchpath_auto_matches=lambda client, project, upper: auto)
    )

    assert summary["num_auto_matched"] == 0
    resolution_rows, _review_rows = write_calls[0]
    assert resolution_rows == []


def test_empty_crosswalk_raises(mock_bigquery_client):
    with pytest.raises(RuntimeError, match="serving.crosswalk"):
        _run(_patched(read_existing_crosswalk=lambda client, project: {}))
