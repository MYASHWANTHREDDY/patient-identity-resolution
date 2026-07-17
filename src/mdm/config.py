"""Tier resolution and config loading.

Selecting `--tier` / `MDM_TIER` resolves to a dataset size, a compute backend, and a dbt
target. Nothing else changes (see PROJECT_CONSTITUTION.md #4 and #16).
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "matching.yml"
VALID_TIERS = ("ci", "dev", "scale")
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
_REQUIRED_TIER_KEYS = {"num_identities", "target_records", "backend", "dbt_target"}


class ConfigError(ValueError):
    """Raised when config/matching.yml is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class TierConfig:
    name: str
    num_identities: int
    target_records: int
    backend: str
    dbt_target: str


def _interpolate_env(value: Any) -> Any:
    """Resolve ${VAR} references against the environment. Unset vars become ''."""
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError(f"Config file did not parse to a mapping: {path}")

    return _interpolate_env(raw)


def resolve_tier(tier_name: str | None = None, config: dict[str, Any] | None = None) -> TierConfig:
    """Resolve a tier name (explicit arg, then MDM_TIER, then 'dev') to its TierConfig."""
    tier_name = tier_name or os.environ.get("MDM_TIER", "dev")
    if tier_name not in VALID_TIERS:
        raise ConfigError(f"Unknown tier '{tier_name}'. Valid tiers: {VALID_TIERS}")

    config = config if config is not None else load_config()
    tiers = config.get("tiers", {})
    if tier_name not in tiers:
        raise ConfigError(f"Tier '{tier_name}' is not defined in config/matching.yml")

    tier_raw = tiers[tier_name]
    missing = _REQUIRED_TIER_KEYS - tier_raw.keys()
    if missing:
        raise ConfigError(f"Tier '{tier_name}' is missing required keys: {sorted(missing)}")

    return TierConfig(
        name=tier_name,
        num_identities=int(tier_raw["num_identities"]),
        target_records=int(tier_raw["target_records"]),
        backend=str(tier_raw["backend"]),
        dbt_target=str(tier_raw["dbt_target"]),
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Resolve and print an MDM tier config.")
    parser.add_argument("--tier", choices=VALID_TIERS, default=None)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()

    tier = resolve_tier(args.tier, load_config(args.config))
    print(tier)


if __name__ == "__main__":
    _main()
