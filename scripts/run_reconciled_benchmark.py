"""Evaluate the existing deadline admission rule in the current operating model.

This is an exploratory extension of the confirmed grids. The simulator and the
original configurations are unchanged. Only source mix / 25 minute cases are
added, at both rosters and both elevator capacities, with the original seeds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml

import run_operational_study as study
from hospital_sim.config import load_config

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results" / "raw"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def restore_verified_inputs() -> dict[str, str]:
    sources = {}
    for tag in ("", "_elev4_full"):
        stem = f"operational_confirmation{tag}"
        manifest = json.loads((ROOT / "results/manifests" / f"{stem}.json").read_text())
        if digest(ROOT / "src/hospital_sim/simulation.py") != manifest["source_sha256"]:
            raise ValueError("Simulator differs from the original study")
        if digest(ROOT / "data/processed/combined_parameters.json") != manifest["parameters_sha256"]:
            raise ValueError("Parameters differ from the original study")
        target = RAW / f"{stem}_runs.csv"
        source = target if target.exists() else ROOT / "tmp/server_elev4_full/raw" / target.name
        if digest(source) != manifest["raw_sha256"]:
            raise ValueError(f"Original raw data checksum mismatch: {source}")
        if source != target:
            target.write_bytes(source.read_bytes())
        sources[target.relative_to(ROOT).as_posix()] = digest(target)
    return sources


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    sources = restore_verified_inputs()
    jobs = []
    operations = yaml.safe_load((ROOT / "configs/revision_experiments.yaml").read_text())["robot_operations"]
    for filename, capacity in (("operational_study.yaml", 2), ("operational_study_elev4.yaml", 4)):
        design_path = ROOT / "configs" / filename
        sources[design_path.relative_to(ROOT).as_posix()] = digest(design_path)
        design = yaml.safe_load(design_path.read_text())
        base = load_config(design_path.parent / design["base_config"])
        for scenario in study._scenarios(design):
            if scenario["policy"] != "adaptive_hybrid" or scenario["mix"] != "source" or scenario["specimen_deadline"] != 25:
                continue
            scenario.update(policy="deadline_hybrid", fleet=2, elevator_capacity=capacity)
            scenario["variant"] = f"e{capacity}_{scenario['staffing_name']}_d25_source_deadline_hybrid"
            config = study._config_for(base, design, operations, scenario, "confirmation")
            for replication in range(30):
                jobs.append((config, scenario, 20261120 + replication, replication))
    checkpoint = RAW / "reconciled_deadline_benchmark_runs.csv"
    records = pd.read_csv(checkpoint).to_dict("records") if checkpoint.exists() else []
    done = {(row["variant"], int(row["seed"])) for row in records}
    jobs = [job for job in jobs if (job[1]["variant"], job[2]) not in done]
    print(f"Pending benchmark runs: {len(jobs)}; already saved: {len(records)}", flush=True)
    with ProcessPoolExecutor(max_workers=args.workers, initializer=study._initialize, initargs=(str(ROOT / "data/processed/combined_parameters.json"),)) as pool:
        futures = {pool.submit(study._run, job): job[1]["elevator_capacity"] for job in jobs}
        for future in as_completed(futures):
            result = future.result()
            result["elevator_capacity"] = futures[future]
            records.append(result)
            if len(records) % 10 == 0:
                pd.DataFrame(records).to_csv(checkpoint, index=False)
                print(f"Saved {len(records)}/120 benchmark runs", flush=True)
    data = pd.DataFrame(records).sort_values(["elevator_capacity", "staffing_name", "seed"])
    data.to_csv(checkpoint, index=False)
    assert len(data) == 120
    assert not data.duplicated(["variant", "seed"]).any()
    assert (data.created == data.completed + data.incomplete).all()
    assert (data.battery_infeasible_missions == 0).all()
    sources["src/hospital_sim/simulation.py"] = digest(ROOT / "src/hospital_sim/simulation.py")
    sources["src/hospital_sim/routing.py"] = digest(ROOT / "src/hospital_sim/routing.py")
    sources["data/processed/combined_parameters.json"] = digest(ROOT / "data/processed/combined_parameters.json")
    sources["scripts/run_reconciled_benchmark.py"] = digest(Path(__file__))
    manifest = {"created_at": datetime.now(UTC).isoformat(), "status": "exploratory", "replications": 120,
                "scenario_count": 4, "seeds": [20261120, 20261149], "warmup_days": 7, "measurement_days": 30,
                "clearance_days": 1, "inputs_sha256": sources, "raw_sha256": digest(checkpoint)}
    (ROOT / "results/manifests/reconciled_deadline_benchmark.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Completed and verified all 120 benchmark runs", flush=True)


if __name__ == "__main__":
    main()
