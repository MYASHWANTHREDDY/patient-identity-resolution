import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from load_bigquery import VENDOR_TABLES, load_tier_to_bigquery  # noqa: E402


def test_load_tier_to_bigquery_loads_all_three_vendors():
    with (
        patch("load_bigquery.subprocess.run") as mock_run,
        patch("load_bigquery.shutil.which", return_value="/usr/bin/bq"),
    ):
        destinations = load_tier_to_bigquery("my-project", "my-bucket", "dev")

    assert set(destinations) == set(VENDOR_TABLES)
    assert mock_run.call_count == 3
    assert destinations["vendor_a"] == "my-project:raw_standard.vendor_a"


def test_load_tier_to_bigquery_calls_bq_load_with_replace():
    with (
        patch("load_bigquery.subprocess.run") as mock_run,
        patch("load_bigquery.shutil.which", return_value="/usr/bin/bq"),
    ):
        load_tier_to_bigquery("my-project", "my-bucket", "dev")

    first_call_args = mock_run.call_args_list[0].args[0]
    assert first_call_args[0] == "/usr/bin/bq"
    assert "load" in first_call_args
    assert "--replace" in first_call_args
    assert "gs://my-bucket/dev/raw/vendor_a/part-*.parquet" in first_call_args
    assert "my-project:raw_standard.vendor_a" in first_call_args


def test_load_tier_to_bigquery_raises_when_bq_not_on_path():
    with patch("load_bigquery.shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError, match="bq"):
            load_tier_to_bigquery("my-project", "my-bucket", "dev")
