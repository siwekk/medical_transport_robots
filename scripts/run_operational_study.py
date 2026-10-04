"""Reproducible location aware scenario grid for the T-ASE revision."""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from itertools import product
from math import sqrt
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from hospital_sim.config import load_config, scenario_config
from hospital_sim.experiment import load_parameters
from hospital_sim.simulation import run_simulation

PARAMETERS: dict[str, Any] | None = None


def _initialize(parameters_path: str) -> None:
    global PARAMETERS
    PARAMETERS = load_parameters(parameters_path)


def _hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _config_for(
    base: dict[str, Any], design: dict[str, Any], operations: dict[str, Any],
    scenario: dict[str, Any], phase: str,
) -> dict[str, Any]:
    config = scenario_config(
        base,
        policy=scenario["policy"],
        fleet_size=scenario["fleet"],
        demand_multiplier=1.0,
        reliability=0.98,
    )
    experiment = design["experiment"]
    config["model"]["warmup_days"] = experiment[f"{phase}_warmup_days"]
    config["model"]["simulation_days"] = experiment[f"{phase}_measurement_days"]
    roster = scenario["staffing"]
    config["hospital"]["human_staffing"] = [
        {"name": "night", "start_hour": 22, "end_hour": 6, "count": roster["night"]},
        {"name": "day", "start_hour": 6, "end_hour": 14, "count": roster["day"]},
        {"name": "evening", "start_hour": 14, "end_hour": 22, "count": roster["evening"]},
    ]
    config["hospital"].update(design["hospital_operations"])
    config["robots"].update(operations)
    config["robots"].update(design["robot_operations"])
    config["robots"]["battery_enabled"] = bool(scenario["fleet"])
    config["robots"]["infection_control_enabled"] = bool(scenario["fleet"])
    config["robots"]["schedule_human_support"] = bool(scenario["fleet"])
    config["tasks"]["specimen"]["deadline_minutes"] = scenario["specimen_deadline"]
    return config


def _scenarios(design: dict[str, Any]) -> list[dict[str, Any]]:
    scenarios = []
    for roster, deadline, mix, policy in product(
        design["staffing"],
        design["specimen_deadlines_minutes"],
        design["task_mixes"],
        design["policies"],
    ):
        scenario = {
            "staffing": roster,
            "staffing_name": roster["name"],
            "specimen_deadline": deadline,
            "mix": mix,
            "policy": policy["name"],
            "fleet": policy["fleet"],
        }
        scenario["variant"] = (
            f"{roster['name']}_d{deadline}_{mix}_{policy['name']}"
        )
        scenarios.append(scenario)
    return scenarios


def _run(job: tuple[dict[str, Any], dict[str, Any], int, int]) -> dict[str, Any]:
    config, scenario, seed, replication = job
    if PARAMETERS is None:
        raise RuntimeError("Parameters were not initialized")
    parameters = deepcopy(PARAMETERS)
    if scenario["mix"] == "balanced":
        parameters["task_mix"] = {
            "specimen": 0.40,
            "medication": 0.30,
            "supply": 0.20,
            "instrument": 0.10,
        }
    result = run_simulation(config, parameters, seed=seed)
    result.update(
        variant=scenario["variant"],
        staffing_name=scenario["staffing_name"],
        specimen_deadline=scenario["specimen_deadline"],
        task_mix=scenario["mix"],
        policy_name=scenario["policy"],
        replication=replication,
    )
    return result


def _summary(data: pd.DataFrame) -> pd.DataFrame:
    metrics = [
        "late_or_incomplete_fraction", "urgent_late_fraction",
        "mean_wait_minutes", "mean_cycle_minutes", "human_utilization",
        "human_contiguous_idle_minutes_per_100_beds_day",
        "human_empty_travel_minutes_per_100_beds_day",
        "human_support_minutes_per_100_beds_day", "robot_completed",
        "incomplete",
    ]
    keys = ["variant", "staffing_name", "specimen_deadline", "task_mix", "policy_name"]
    summary = data.groupby(keys, as_index=False)[metrics].agg(["mean", "std"])
    summary.columns = [
        "_".join(str(piece) for piece in column if piece)
        if isinstance(column, tuple) else column for column in summary.columns
    ]
    return summary


def _paired(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    keys = ["staffing_name", "specimen_deadline", "task_mix"]
    for group_key, group in data.groupby(keys):
        baseline = group[group.policy_name.eq("human_only")].set_index("seed")
        for policy, treatment in group.groupby("policy_name"):
            if policy == "human_only":
                continue
            paired = treatment.set_index("seed")
            for metric in (
                "late_or_incomplete_fraction", "urgent_late_fraction",
                "human_contiguous_idle_minutes_per_100_beds_day",
            ):
                difference = paired[metric] - baseline.loc[paired.index, metric]
                n = len(difference)
                mean = float(difference.mean())
                critical = 2.04523 if n == 30 else 2.77645 if n == 5 else 1.96
                half = critical * float(difference.std(ddof=1)) / sqrt(n)
                rows.append({
                    "staffing_name": group_key[0],
                    "specimen_deadline": group_key[1],
                    "task_mix": group_key[2],
                    "policy_name": policy,
                    "metric": metric,
                    "replications": n,
                    "paired_difference": mean,
                    "ci_low": mean - half,
                    "ci_high": mean + half,
                })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/operational_study.yaml")
    parser.add_argument("--parameters", default="data/processed/combined_parameters.json")
    parser.add_argument("--phase", choices=["screening", "confirmation"], required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    design_path = Path(args.config)
    design = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    base = load_config(design_path.parent / design["base_config"])
    revision = yaml.safe_load(
        (design_path.parent / design["robot_operations_config"]).read_text(encoding="utf-8")
    )
    operations = revision["robot_operations"]
    suffix = "smoke" if args.limit else args.phase
    if args.tag:
        suffix += f"_{args.tag}"
    raw = Path(f"results/raw/operational_{suffix}_runs.csv")
    summary = Path(f"results/tables/operational_{suffix}_summary.csv")
    paired = Path(f"results/tables/operational_{suffix}_paired.csv")
    if args.summarize_only:
        data = pd.read_csv(raw)
    else:
        jobs = []
        for scenario in _scenarios(design):
            config = _config_for(base, design, operations, scenario, args.phase)
            count = int(design["experiment"][f"{args.phase}_replications"])
            seed = int(design["experiment"][f"{args.phase}_seed"])
            for replication in range(count):
                jobs.append((config, scenario, seed + replication, replication))
        if args.limit:
            jobs = jobs[:args.limit]
        _initialize(args.parameters)
        if args.workers == 1:
            records = [_run(job) for job in jobs]
        else:
            with ProcessPoolExecutor(
                max_workers=args.workers, initializer=_initialize, initargs=(args.parameters,)
            ) as executor:
                records = list(executor.map(_run, jobs, chunksize=2))
        data = pd.DataFrame.from_records(records)
    raw.parent.mkdir(parents=True, exist_ok=True)
    summary.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(raw, index=False)
    _summary(data).to_csv(summary, index=False)
    if not args.limit:
        _paired(data).to_csv(paired, index=False)
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "phase": args.phase,
        "replications": len(data),
        "scenario_count": int(data.variant.nunique()),
        "source_sha256": _hash("src/hospital_sim/simulation.py"),
        "design_sha256": _hash(design_path),
        "parameters_sha256": _hash(args.parameters),
        "raw_sha256": _hash(raw),
    }
    manifest_path = Path(f"results/manifests/operational_{suffix}.json")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(_summary(data).to_string(index=False))


if __name__ == "__main__":
    main()
