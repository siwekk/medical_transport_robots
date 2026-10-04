"""Plot the matched human only elevator capacity sensitivity."""

from __future__ import annotations

from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
TABLES = ROOT / "results" / "tables"
OUTPUT = ROOT / "manuscript" / "images" / "operational_elevator_sensitivity.pdf"


def main() -> None:
    files = {
        2: "operational_confirmation_summary.csv",
        4: "operational_confirmation_elev4_summary.csv",
        8: "operational_confirmation_elev8_summary.csv",
    }
    records = []
    for capacity, name in files.items():
        table = pd.read_csv(TABLES / name)
        selected = table[
            table.policy_name.eq("human_only")
            & table.task_mix.eq("source")
            & table.specimen_deadline.eq(25)
        ]
        for row in selected.itertuples():
            records.append({
                "capacity": capacity,
                "roster": row.staffing_name,
                "mean": 100 * row.late_or_incomplete_fraction_mean,
                "half": (
                    100 * 2.04523
                    * row.late_or_incomplete_fraction_std
                    / (30 ** 0.5)
                ),
            })
    data = pd.DataFrame(records)
    plt.rcParams.update({
        "font.family": "serif",
        "font.size": 8,
        "axes.labelsize": 8,
        "legend.fontsize": 8,
        "pdf.fonttype": 42,
    })
    fig, ax = plt.subplots(figsize=(3.45, 2.5))
    for roster, marker, color in (
        ("high", "o", "black"),
        ("moderate", "s", "0.4"),
    ):
        part = data[data.roster.eq(roster)].sort_values("capacity")
        ax.errorbar(
            part.capacity, part["mean"], yerr=part.half,
            color=color, marker=marker, linewidth=1,
            capsize=2, markersize=4, label=f"{roster.capitalize()} roster",
        )
    ax.set_xticks([2, 4, 8])
    ax.set_xlabel("Assumed elevator bank capacity")
    ax.set_ylabel("Late or incomplete requests (%)")
    ax.set_ylim(0, 80)
    ax.grid(axis="y", color="0.85", linewidth=0.5)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(OUTPUT, bbox_inches="tight")
    plt.close(fig)
    print(OUTPUT)


if __name__ == "__main__":
    main()
