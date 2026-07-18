import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from upload_to_gcs import upload_tier_to_gcs  # noqa: E402


def test_upload_tier_to_gcs_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        upload_tier_to_gcs(tmp_path / "does_not_exist", "some-bucket", "dev")


def test_upload_tier_to_gcs_calls_gcloud_storage_rsync(tmp_path):
    tier_dir = tmp_path / "dev"
    tier_dir.mkdir()

    with (
        patch("upload_to_gcs.subprocess.run") as mock_run,
        patch("upload_to_gcs.shutil.which", return_value="/usr/bin/gcloud"),
    ):
        destination = upload_tier_to_gcs(tier_dir, "my-bucket", "dev")

    assert destination == "gs://my-bucket/dev"
    mock_run.assert_called_once_with(
        [
            "/usr/bin/gcloud",
            "storage",
            "rsync",
            "--recursive",
            str(tier_dir),
            "gs://my-bucket/dev",
        ],
        check=True,
    )


def test_upload_tier_to_gcs_raises_when_gcloud_not_on_path(tmp_path):
    tier_dir = tmp_path / "dev"
    tier_dir.mkdir()

    with patch("upload_to_gcs.shutil.which", return_value=None):
        with pytest.raises(FileNotFoundError, match="gcloud"):
            upload_tier_to_gcs(tier_dir, "my-bucket", "dev")
