import re

import pytest

from mdm.config import (
    DEFAULT_CONFIG_PATH,
    REPO_ROOT,
    VALID_TIERS,
    ConfigError,
    load_config,
    resolve_tier,
)


def test_default_config_file_loads():
    config = load_config(DEFAULT_CONFIG_PATH)
    assert "tiers" in config


@pytest.mark.parametrize("tier_name", VALID_TIERS)
def test_resolve_tier_known_tiers(tier_name):
    tier = resolve_tier(tier_name)
    assert tier.name == tier_name
    assert tier.num_identities > 0
    assert tier.target_records > 0
    assert tier.backend in {"duckdb", "spark"}
    assert tier.dbt_target in {"dev", "prod"}


def test_scale_tier_is_the_largest():
    ci = resolve_tier("ci")
    dev = resolve_tier("dev")
    scale = resolve_tier("scale")
    assert ci.target_records < dev.target_records < scale.target_records


def test_resolve_tier_rejects_unknown_tier():
    with pytest.raises(ConfigError):
        resolve_tier("staging")


def test_resolve_tier_defaults_to_env_var(monkeypatch):
    monkeypatch.setenv("MDM_TIER", "ci")
    tier = resolve_tier()
    assert tier.name == "ci"


def test_resolve_tier_defaults_to_dev_without_env_var(monkeypatch):
    monkeypatch.delenv("MDM_TIER", raising=False)
    tier = resolve_tier()
    assert tier.name == "dev"


def test_load_config_missing_file_raises():
    with pytest.raises(ConfigError):
        load_config("config/does_not_exist.yml")


def test_resolve_tier_missing_required_key_raises():
    broken_config = {"tiers": {"dev": {"num_identities": 100}}}
    with pytest.raises(ConfigError):
        resolve_tier("dev", broken_config)


def test_dbt_max_block_size_matches_config():
    """dbt/dbt_project.yml's max_block_size default must agree with
    config/matching.yml's blocking.max_block_size -- dbt can't read our Python-loaded
    config directly, so this is the drift guard (see docs/design-decisions.md)."""
    config_value = load_config()["blocking"]["max_block_size"]

    dbt_project_text = (REPO_ROOT / "dbt" / "dbt_project.yml").read_text(encoding="utf-8")
    match = re.search(r"env_var\('MDM_MAX_BLOCK_SIZE',\s*'(\d+)'\)", dbt_project_text)
    assert match, "Could not find MDM_MAX_BLOCK_SIZE default in dbt/dbt_project.yml"
    dbt_default_value = int(match.group(1))

    assert dbt_default_value == config_value
