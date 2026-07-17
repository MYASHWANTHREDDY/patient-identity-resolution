import pytest

from mdm.config import DEFAULT_CONFIG_PATH, VALID_TIERS, ConfigError, load_config, resolve_tier


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
