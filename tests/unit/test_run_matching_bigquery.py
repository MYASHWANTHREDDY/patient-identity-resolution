import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_matching_bigquery import run_matching_bigquery  # noqa: E402

PATIENT_NORMALIZED = pd.DataFrame(
    [
        {
            "record_key": "A",
            "source_vendor": "VENDOR_A",
            "source_record_id": "1",
            "first_name": "ROBERT",
            "last_name": "SMITH",
            "dob": datetime(1980, 1, 1),
            "gender": "M",
            "ssn": "123456789",
            "normalized_at": datetime(2024, 1, 1),
        },
        {
            "record_key": "B",
            "source_vendor": "VENDOR_B",
            "source_record_id": "2",
            "first_name": "ROBERT",
            "last_name": "SMITH",
            "dob": datetime(1980, 1, 1),
            "gender": "M",
            "ssn": "123456789",
            "normalized_at": datetime(2024, 1, 2),
        },
        {
            "record_key": "C",
            "source_vendor": "VENDOR_C",
            "source_record_id": "3",
            "first_name": "ZELMIRA",
            "last_name": "QUINTANILLA",
            "dob": datetime(1990, 6, 6),
            "gender": "F",
            "ssn": None,
            "normalized_at": datetime(2024, 1, 3),
        },
    ]
)

# A and B auto-matched into one cluster; C never touched by any auto-match edge, so it
# never appears in the clusters table at all -- run_matching_bigquery must still give it
# its own singleton cluster, exactly like mdm.pipeline.run_matching does locally.
CLUSTERS = pd.DataFrame(
    [
        {
            "component": "A",
            "members": ["A", "B"],
            "size": 2,
            "scored_pairs": 1,
            "possible_pairs": 1,
            "confidence": 1.0,
            "flagged": False,
        }
    ]
)

PAIR_SCORES = pd.DataFrame(
    [{"record_key_a": "A", "record_key_b": "B", "score": 50.0}]
)


def _patched(**overrides):
    defaults = dict(
        read_patient_normalized=lambda client, project: PATIENT_NORMALIZED,
        read_clusters=lambda client, project: CLUSTERS,
        read_pair_scores=lambda client, project, lower, upper: PAIR_SCORES,
        read_existing_crosswalk=lambda client, project: {},
    )
    defaults.update(overrides)
    return defaults


@pytest.fixture
def mock_bigquery_client():
    with patch("google.cloud.bigquery.Client") as mock_client_cls:
        yield mock_client_cls


def test_run_matching_bigquery_merges_matched_pair_and_keeps_singleton(mock_bigquery_client):
    write_calls = []
    patches = _patched()
    with (
        patch("run_matching_bigquery.read_patient_normalized", patches["read_patient_normalized"]),
        patch("run_matching_bigquery.read_clusters", patches["read_clusters"]),
        patch("run_matching_bigquery.read_pair_scores", patches["read_pair_scores"]),
        patch("run_matching_bigquery.read_existing_crosswalk", patches["read_existing_crosswalk"]),
        patch(
            "run_matching_bigquery.write_serving_tables",
            side_effect=lambda *args, **kwargs: write_calls.append((args, kwargs)),
        ),
    ):
        summary = run_matching_bigquery("my-project", run_id="run1")

    assert summary["num_records"] == 3
    assert summary["num_clusters"] == 1  # only the A-B cluster; C is a bare singleton
    assert summary["num_golden_records"] == 2  # one for {A, B}, one for {C}
    assert summary["num_auto_match_edges"] == 1
    assert summary["num_identity_events"] == 2  # two CREATE events: {A,B} and {C}

    assert len(write_calls) == 1
    args, _ = write_calls[0]
    new_crosswalk = args[2]
    assert set(new_crosswalk) == {"A", "B", "C"}
    assert new_crosswalk["A"].patient_global_id == new_crosswalk["B"].patient_global_id
    assert new_crosswalk["C"].patient_global_id != new_crosswalk["A"].patient_global_id


def test_run_matching_bigquery_reuses_existing_crosswalk_id(mock_bigquery_client):
    from mdm.crosswalk import CrosswalkEntry

    existing = {
        "A": CrosswalkEntry("A", "PGID000000000042", "run0", "run0"),
        "B": CrosswalkEntry("B", "PGID000000000042", "run0", "run0"),
        "C": CrosswalkEntry("C", "PGID000000000099", "run0", "run0"),
    }
    write_calls = []
    patches = _patched(read_existing_crosswalk=lambda client, project: existing)
    with (
        patch("run_matching_bigquery.read_patient_normalized", patches["read_patient_normalized"]),
        patch("run_matching_bigquery.read_clusters", patches["read_clusters"]),
        patch("run_matching_bigquery.read_pair_scores", patches["read_pair_scores"]),
        patch("run_matching_bigquery.read_existing_crosswalk", patches["read_existing_crosswalk"]),
        patch(
            "run_matching_bigquery.write_serving_tables",
            side_effect=lambda *args, **kwargs: write_calls.append((args, kwargs)),
        ),
    ):
        summary = run_matching_bigquery("my-project", run_id="run1")

    # nothing changed vs. the existing crosswalk -- no churn, matching mdm.pipeline's own
    # idempotency guarantee (PROJECT_CONSTITUTION.md #7)
    assert summary["num_identity_events"] == 0
    args, _ = write_calls[0]
    new_crosswalk = args[2]
    assert new_crosswalk["A"].patient_global_id == "PGID000000000042"
    assert new_crosswalk["C"].patient_global_id == "PGID000000000099"
