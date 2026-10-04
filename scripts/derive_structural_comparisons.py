"""Compute paired structural comparisons from the same demand seeds."""

from __future__ import annotations

from math import sqrt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "results" / "raw"
OUTPUT = ROOT / "results" / "tables" / "operational_structural_comparisons.csv"


def human_case(name: str) -> pd.DataFrame:
    data = pd.read_csv(RAW / f"{name}_runs.csv")
    return data[
        data.policy_name.eq("human_only")
        & data.task_mix.eq("source")
        & data.specimen_deadline.eq(25)
    ]


def main() -> None:
    cases = {
        "no_pickup_two_servers": human_case("operational_confirmation_no_pickup"),
        "pickup_two_servers": human_case("operational_confirmation"),
        "pickup_four_servers": human_case("operational_confirmation_elev4"),
        "pickup_eight_servers": human_case("operational_confirmation_elev8"),
    }
    contrasts = [
        ("pickup_vs_no_pickup", "no_pickup_two_servers", "pickup_two_servers"),
        ("four_vs_two_servers", "pickup_two_servers", "pickup_four_servers"),
        ("eight_vs_four_servers", "pickup_four_servers", "pickup_eight_servers"),
    ]
    rows = []
    for label, baseline, variant in contrasts:
        joined = cases[baseline].merge(
            cases[variant],
            on=["seed", "staffing_name"],
            suffixes=("_baseline", "_variant"),
            validate="one_to_one",
        )
        for roster, subset in joined.groupby("staffing_name"):
            difference = 100 * (
                subset.late_or_incomplete_fraction_variant
                - subset.late_or_incomplete_fraction_baseline
            )
            half_width = 2.04523 * difference.std(ddof=1) / sqrt(len(difference))
            mean = difference.mean()
            rows.append({
                "contrast": label,
                "roster": roster,
                "replications": len(difference),
                "baseline_late_percent": (
                    100 * subset.late_or_incomplete_fraction_baseline.mean()
                ),
                "variant_late_percent": (
                    100 * subset.late_or_incomplete_fraction_variant.mean()
                ),
                "paired_difference_points": mean,
                "ci_low_points": mean - half_width,
                "ci_high_points": mean + half_width,
                "baseline_elevator_wait_minutes": (
                    subset.mean_elevator_wait_minutes_baseline.mean()
                ),
                "variant_elevator_wait_minutes": (
                    subset.mean_elevator_wait_minutes_variant.mean()
                ),
            })
    pd.DataFrame(rows).to_csv(OUTPUT, index=False)
    print(OUTPUT)


if __name__ == "__main__":
    main()
