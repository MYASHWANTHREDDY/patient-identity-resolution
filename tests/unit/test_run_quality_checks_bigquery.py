import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_quality_checks_bigquery import run_quality_checks_bigquery  # noqa: E402


class _ScalarRow:
    """Stands in for a single row of a bigquery.QueryJob.result() row iterator -- real
    code does `next(client.query(...).result()).n`."""

    def __init__(self, n: int) -> None:
        self.n = n


class _FakeQueryJob:
    def __init__(self, sql: str, scalars: dict, dataframes: dict) -> None:
        self._sql = sql
        self._scalars = scalars
        self._dataframes = dataframes

    def result(self):
        for needle, n in self._scalars.items():
            if needle in self._sql:
                return iter([_ScalarRow(n)])
        raise AssertionError(f"no scalar stub matched query: {self._sql}")

    def to_dataframe(self):
        for needle, df in self._dataframes.items():
            if needle in self._sql:
                return df
        raise AssertionError(f"no dataframe stub matched query: {self._sql}")


class _FakeClient:
    def __init__(self, scalars: dict, dataframes: dict) -> None:
        self._scalars = scalars
        self._dataframes = dataframes
        self.loaded: list[tuple[str, pd.DataFrame]] = []

    def query(self, sql: str) -> _FakeQueryJob:
        return _FakeQueryJob(sql, self._scalars, self._dataframes)

    def load_table_from_dataframe(self, df, table_id, job_config=None):
        self.loaded.append((table_id, df))
        job = MagicMock()
        job.result.return_value = None
        return job


@pytest.fixture
def fake_client():
    scalars = {
        "conformance.patient_normalized": 100,
        "serving.member_demographics": 60,
        "serving.review_queue": 5,
        "matching.candidate_pairs": 200,
    }
    dataframes = {
        "matching.block_stats": pd.DataFrame({"blocking_pass": ["p1"], "record_count": [100]}),
        "dob_year": pd.DataFrame({"dob_year": [1980, 1990, 1975]}),
        "serving.membership": pd.DataFrame({"source_record_count": [1, 2, 1]}),
    }
    client = _FakeClient(scalars, dataframes)
    with patch("google.cloud.bigquery.Client", return_value=client):
        yield client


def test_run_quality_checks_bigquery_writes_all_five_checks(fake_client):
    results_df = run_quality_checks_bigquery("my-project", run_id="run1")

    assert len(results_df) == 5
    assert set(results_df["check_name"]) == {
        "dedup_rate",
        "review_queue_rate",
        "block_skew",
        "dob_plausibility",
        "cluster_size_distribution",
    }
    assert (results_df["run_id"] == "run1").all()

    assert len(fake_client.loaded) == 1
    table_id, loaded_df = fake_client.loaded[0]
    assert table_id == "my-project.quality.validation_runs"
    assert len(loaded_df) == 5
