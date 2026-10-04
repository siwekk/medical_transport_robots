"""Build the manuscript table and vector figure from confirmed runs."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
TABLES = ROOT / "results" / "tables"
MANUSCRIPT = ROOT / "manuscript"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    suffix = f"_{args.tag}" if args.tag else ""
    summary = pd.read_csv(TABLES / f"operational_confirmation{suffix}_summary.csv")
    paired = pd.read_csv(TABLES / f"operational_confirmation{suffix}_paired.csv")
    paired = paired[paired.metric.eq("late_or_incomplete_fraction")].copy()
    bank = "four" if args.tag == "elev4_full" else "two"
    label = "tab:operational-main-four" if args.tag else "tab:operational-main"

    lines = [
        r"\begin{table*}[t]",
        (
            r"\caption{Overall late or incomplete requests with "
            + bank
            + r" assumed elevator servers in 30 replications. Differences are percentage points relative to the paired human only case.}"
        ),
        rf"\label{{{label}}}",
        r"\centering\footnotesize",
        r"\begin{tabular}{lllrrr}",
        r"\toprule",
        r"Roster & Task mix & Specimen deadline (min) & Human only (\%) & ARAD difference & Proximity difference \\",
        r"\midrule",
    ]
    for roster in ("high", "moderate"):
        for mix in ("source", "balanced"):
            for deadline in (25, 45, 60):
                case = summary[
                    summary.staffing_name.eq(roster)
                    & summary.task_mix.eq(mix)
                    & summary.specimen_deadline.eq(deadline)
                ]
                human = case[case.policy_name.eq("human_only")].iloc[0]
                contrasts = paired[
                    paired.staffing_name.eq(roster)
                    & paired.task_mix.eq(mix)
                    & paired.specimen_deadline.eq(deadline)
                ]
                arad = contrasts[
                    contrasts.policy_name.eq("adaptive_hybrid")
                ].iloc[0]
                proximity = contrasts[
                    contrasts.policy_name.eq("travel_aware_hybrid")
                ].iloc[0]
                lines.append(
                    f"{roster.capitalize()} & {mix.capitalize()} & {deadline} & "
                    f"{100 * human.late_or_incomplete_fraction_mean:.2f} & "
                    f"{100 * arad.paired_difference:+.2f} & "
                    f"{100 * proximity.paired_difference:+.2f} "
                    + r"\\"
                )
            if mix == "source":
                lines.append(r"\addlinespace")
        if roster == "high":
            lines.append(r"\midrule")
    lines.extend([r"\bottomrule", r"\end{tabular}", r"\end{table*}"])
    (MANUSCRIPT / f"operational_main_table{suffix}.tex").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    plt.rcParams.update({
        "font.family": "serif",
        "font.size": 8,
        "axes.labelsize": 8,
        "axes.titlesize": 8,
        "legend.fontsize": 7,
        "pdf.fonttype": 42,
    })
    fig, axes = plt.subplots(2, 2, figsize=(7.1, 4.4), sharex=True, sharey=True)
    policies = [
        ("adaptive_hybrid", "ARAD", "o", "black", -1.0),
        ("travel_aware_hybrid", "Proximity", "s", "0.45", 1.0),
    ]
    for row, roster in enumerate(("high", "moderate")):
        for column, mix in enumerate(("source", "balanced")):
            ax = axes[row, column]
            for name, label, marker, color, offset in policies:
                subset = paired[
                    paired.staffing_name.eq(roster)
                    & paired.task_mix.eq(mix)
                    & paired.policy_name.eq(name)
                ].sort_values("specimen_deadline")
                x = subset.specimen_deadline.to_numpy(dtype=float) + offset
                y = 100 * subset.paired_difference.to_numpy(dtype=float)
                low = 100 * (subset.paired_difference - subset.ci_low).to_numpy(dtype=float)
                high = 100 * (subset.ci_high - subset.paired_difference).to_numpy(dtype=float)
                ax.errorbar(
                    x, y, yerr=[low, high], marker=marker, linestyle="-",
                    color=color, capsize=2, linewidth=1, markersize=3.5,
                    label=label,
                )
            ax.axhline(0, color="0.65", linewidth=0.8)
            ax.set_title(f"{roster.capitalize()} roster, {mix} mix")
            ax.set_xticks([25, 45, 60])
            ax.grid(axis="y", color="0.9", linewidth=0.5)
            if row == 1:
                ax.set_xlabel("Assumed specimen deadline (min)")
    fig.supylabel("Late fraction difference (percentage points)", x=0.01)
    axes[0, 0].legend(loc="upper left", frameon=False)
    fig.tight_layout(rect=(0.04, 0, 1, 1))
    output = MANUSCRIPT / "images" / f"operational_policy_differences{suffix}.pdf"
    fig.savefig(output, bbox_inches="tight")
    plt.close(fig)
    print(output)


if __name__ == "__main__":
    main()
