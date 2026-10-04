from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_config(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate a YAML model configuration."""
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    base_name = config.pop("base_config", None)
    if base_name:
        base = load_config(config_path.parent / base_name)
        config = _deep_merge(base, config)
    required = {"model", "hospital", "tasks", "robots", "experiment"}
    missing = required.difference(config)
    if missing:
        raise ValueError(f"Missing configuration sections: {sorted(missing)}")
    if config["model"]["beds"] <= 0:
        raise ValueError("Hospital bed count must be positive")
    return config


def scenario_config(
    base: dict[str, Any],
    *,
    policy: str,
    fleet_size: int,
    demand_multiplier: float,
    reliability: float,
) -> dict[str, Any]:
    """Return an isolated configuration for one simulation scenario."""
    config = deepcopy(base)
    config["scenario"] = {
        "policy": policy,
        "demand_multiplier": float(demand_multiplier),
    }
    config["robots"]["fleet_size"] = int(fleet_size)
    config["robots"]["reliability"] = float(reliability)
    return config
