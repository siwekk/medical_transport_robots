from __future__ import annotations

import json
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd

from .config import scenario_config
from .simulation import run_simulation


def _run_job(job: tuple[dict[str, Any], dict[str, Any], int]) -> dict[str, Any]:
    config, parameters, seed = job
    return run_simulation(config, parameters, seed=seed)


def build_jobs(
    base_config: dict[str, Any],
    parameters: dict[str, Any],
    *,
    quick: bool,
) -> list[tuple[dict[str, Any], dict[str, Any], int]]:
    experiment = base_config["experiment"]
    policies = experiment["policies"]
    fleets = [0, 1, 2] if quick else experiment["fleet_sizes"]
    demands = [1.0] if quick else experiment["demand_multipliers"]
    reliabilities = [0.98] if quick else experiment["reliabilities"]
    replications = 3 if quick else int(experiment["replications"])
    master_seed = int(experiment["seed"])
    jobs = []
    for policy in policies:
        for fleet_size in fleets:
            if policy == "human_only" and fleet_size != 0:
                continue
            if policy != "human_only" and fleet_size == 0:
                continue
            for demand in demands:
                for reliability in reliabilities:
                    for replication in range(replications):
                        config = scenario_config(
                            base_config,
                            policy=policy,
                            fleet_size=fleet_size,
                            demand_multiplier=demand,
                            reliability=reliability,
                        )
                        config["replication"] = replication
                        jobs.append((config, parameters, master_seed + replication))
    return jobs


def run_experiment(
    jobs: Iterable[tuple[dict[str, Any], dict[str, Any], int]],
    *,
    workers: int = 1,
) -> pd.DataFrame:
    jobs = list(jobs)
    if workers <= 1:
        records = [_run_job(job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            records = list(executor.map(_run_job, jobs))
    return pd.DataFrame.from_records(records)


def load_parameters(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)
