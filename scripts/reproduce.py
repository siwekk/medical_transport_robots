"""Reproduce published analysis or rerun every reported confirmation scenario."""
from __future__ import annotations
import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DESIGNS = [("operational_study.yaml", ""), ("operational_study_elev4.yaml", "elev4_full"),
           ("operational_no_pickup.yaml", "no_pickup"), ("operational_elevator_4.yaml", "elev4"),
           ("operational_elevator_8.yaml", "elev8")]

def run(script, *args):
    subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], cwd=ROOT, check=True)

def verify():
    manifest = json.loads((ROOT / "PACKAGE_MANIFEST.json").read_text())
    for relative, expected in manifest["sha256"].items():
        path = ROOT / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Packaged reference file differs: " + relative)
    print("Verified all packaged reference files.", flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["verify", "analysis", "smoke", "simulate"], nargs="?", default="analysis")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    os.chdir(ROOT)
    os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".cache/matplotlib"))
    os.environ["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
    if args.mode == "verify":
        verify()
        return
    for directory in ["results/raw", "results/tables", "results/manifests", "manuscript/images"]:
        (ROOT / directory).mkdir(parents=True, exist_ok=True)
    if args.mode == "smoke":
        run("run_operational_study.py", "--phase", "confirmation", "--limit", "1", "--workers", str(args.workers))
        return
    if args.mode == "simulate":
        # Retain the packaged references before overwriting any run outputs.
        folder = ROOT / "results/archive" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        folder.mkdir(parents=True)
        import shutil
        for pattern in ["results/raw/*_runs.csv", "results/manifests/*.json"]:
            for path in ROOT.glob(pattern):
                target = folder / path.relative_to(ROOT / "results")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        for config, tag in DESIGNS:
            options = ["--phase", "confirmation", "--config", "configs/" + config,
                       "--workers", str(args.workers)]
            if tag:
                options += ["--tag", tag]
            run("run_operational_study.py", *options)
        checkpoint = ROOT / "results/raw/reconciled_deadline_benchmark_runs.csv"
        if checkpoint.exists():
            checkpoint.unlink()  # Reference was archived above; force all 120 new runs.
        run("run_reconciled_benchmark.py", "--workers", str(args.workers))
    else:
        verify()
    for config, tag in DESIGNS:
        # Summarize without rewriting the delivered raw CSVs or historical manifests.
        import pandas as pd
        import run_operational_study as study
        suffix = "_" + tag if tag else ""
        data = pd.read_csv(ROOT / f"results/raw/operational_confirmation{suffix}_runs.csv")
        study._summary(data).to_csv(ROOT / f"results/tables/operational_confirmation{suffix}_summary.csv", index=False)
        study._paired(data).to_csv(ROOT / f"results/tables/operational_confirmation{suffix}_paired.csv", index=False)
    run("derive_structural_comparisons.py")
    run("write_operational_publication_assets.py")
    run("write_operational_publication_assets.py", "--tag", "elev4_full")
    run("write_elevator_sensitivity_figure.py")
    run("analyze_reconciled_benchmarks.py")
    print("Reproduced tables in results/tables and LaTeX tables and figures in manuscript/.")

if __name__ == "__main__":
    main()
