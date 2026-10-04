"""Pair current-model policies by seed and generate the reconciled paper assets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
KEYS = ["elevator_capacity", "staffing_name", "task_mix", "specimen_deadline", "seed"]
METRICS = ["late_or_incomplete_fraction", "urgent_late_fraction", "human_contiguous_idle_minutes_per_100_beds_day"]


def paired(left: pd.DataFrame, right: pd.DataFrame, metric: str) -> dict:
    joined = left[KEYS + [metric, "created", "urgent_created"]].merge(
        right[KEYS + [metric, "created", "urgent_created"]], on=KEYS,
        suffixes=("_left", "_right"), validate="one_to_one")
    assert len(joined) == len(left) == len(right) == 30
    assert (joined.created_left == joined.created_right).all()
    assert (joined.urgent_created_left == joined.urgent_created_right).all()
    differences = joined[f"{metric}_left"] - joined[f"{metric}_right"]
    scale = 100 if metric.endswith("fraction") else 1
    mean = float(differences.mean()) * scale
    half = 2.045229642132703 * float(differences.std(ddof=1)) / sqrt(30) * scale
    return {"metric": metric, "replications": 30, "mean_difference": mean,
            "ci_low": mean - half, "ci_high": mean + half}


def signed(value: float) -> str:
    return "0.00" if round(float(value), 2) == 0 else f"{value:+.2f}"


def main() -> None:
    grids = []
    for capacity, suffix in ((2, ""), (4, "_elev4_full")):
        path = ROOT / f"results/raw/operational_confirmation{suffix}_runs.csv"
        data = pd.read_csv(path)
        data["elevator_capacity"] = capacity
        grids.append(data)
    new = pd.read_csv(ROOT / "results/raw/reconciled_deadline_benchmark_runs.csv")
    data = pd.concat([*grids, new], ignore_index=True)
    assert not data.duplicated(KEYS + ["policy_name"]).any()
    assert (data.created == data.completed + data.incomplete).all()
    assert (data.battery_infeasible_missions == 0).all()
    contrasts = []
    context = []
    labels = {"adaptive_hybrid": "ARAD", "travel_aware_hybrid": "Proximity", "deadline_hybrid": "Deadline"}
    for group_key, group in data.groupby(KEYS[:-1]):
        policies = {name: part for name, part in group.groupby("policy_name")}
        comparisons = [(policy, "human_only") for policy in labels if policy in policies]
        comparisons += [("adaptive_hybrid", "travel_aware_hybrid")]
        if "deadline_hybrid" in policies:
            comparisons += [("deadline_hybrid", "adaptive_hybrid"), ("deadline_hybrid", "travel_aware_hybrid")]
        for left, right in comparisons:
            for metric in METRICS:
                contrasts.append(dict(zip(KEYS[:-1], group_key, strict=True), left=left, right=right,
                                      **paired(policies[left], policies[right], metric)))
        for policy, part in policies.items():
            context.append(dict(zip(KEYS[:-1], group_key, strict=True), policy=policy,
                                coverage_percent=float((100 * part.robot_completed / part.created).mean()),
                                robot_completed=float(part.robot_completed.mean()),
                                human_support_minutes_per_100_beds_day=float(part.human_support_minutes_per_100_beds_day.mean()),
                                human_support_travel_minutes_per_100_beds_day=float((part.human_support_travel_minutes / 60).mean()),
                                created=float(part.created.mean())))
    contrasts = pd.DataFrame(contrasts)
    context = pd.DataFrame(context)
    contrasts.to_csv(ROOT / "results/tables/reconciled_policy_contrasts.csv", index=False)
    context.to_csv(ROOT / "results/tables/reconciled_policy_exposure.csv", index=False)
    source = contrasts[(contrasts.task_mix == "source") & (contrasts.specimen_deadline == 25)]
    lines = [r"\begin{table*}[t]",
             r"\caption{Exploratory benchmark comparisons for the source count mix and 25 minute specimen deadline. Bank denotes assumed elevator servers. Every contrast pairs 30 runs by demand seed against human only dispatch. Late differences are percentage points; idle differences and support are minutes per 100 nominal beds per day. Brackets give pointwise 95\% Monte Carlo intervals. Coverage is the mean fraction of requests completed by robots. Support excludes worker travel, which is recorded separately.}",
             r"\label{tab:reconciled-benchmarks}", r"\centering\footnotesize",
             r"\setlength{\tabcolsep}{4pt}",
             r"\begin{tabular}{lllrlllr}", r"\toprule",
             r"Bank & Roster & Policy & Coverage (\%) & Overall difference & Urgent difference & Idle difference & Support \\",
             r"\midrule"]
    for capacity in (2, 4):
        for roster in ("high", "moderate"):
            for policy, label in labels.items():
                subset = source[(source.elevator_capacity == capacity) & (source.staffing_name == roster) &
                                (source.left == policy) & (source.right == "human_only")].set_index("metric")
                exposure = context[(context.elevator_capacity == capacity) & (context.staffing_name == roster) &
                                   (context.policy == policy) & (context.task_mix == "source") &
                                   (context.specimen_deadline == 25)].iloc[0]
                cells = []
                for metric in METRICS:
                    row = subset.loc[metric]
                    cells.append(r"\shortstack{" + signed(row.mean_difference) + r"\\{}[" + signed(row.ci_low) + ", " + signed(row.ci_high) + "]}")
                lines.append(f"{capacity} & {roster.capitalize()} & {label} & {exposure.coverage_percent:.2f} & " +
                             " & ".join(cells) + f" & {exposure.human_support_minutes_per_100_beds_day:.1f} " + r"\\")
            lines.append(r"\addlinespace")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table*}"]
    (ROOT / "manuscript/operational_benchmark_table.tex").write_text("\n".join(lines) + "\n", encoding="ascii")
    direct = contrasts[(contrasts.left == "adaptive_hybrid") & (contrasts.right == "travel_aware_hybrid") &
                       (contrasts.metric == "late_or_incomplete_fraction")]
    lines = [r"\subsection{Direct policy contrasts and the deadline benchmark}", r"\label{sec:benchmark-results}",
             r"Table~\ref{tab:reconciled-benchmarks} adds the simpler deadline admission rule to four representative cases, using the same source count mix, 25 minute deadline, rosters, and elevator capacities as the main grids. The additional 120 runs use the same 30 demand seeds and the same pickup travel, support, charging, and cleaning model. These comparisons are exploratory. They do not reuse the older experiment without pickup travel.",
             r"\input{operational_benchmark_table.tex}"]
    for capacity in (2, 4):
        paragraph = []
        for roster in ("high", "moderate"):
            row = source[(source.elevator_capacity == capacity) & (source.staffing_name == roster) &
                         (source.left == "adaptive_hybrid") & (source.right == "travel_aware_hybrid") &
                         (source.metric == "late_or_incomplete_fraction")].iloc[0]
            paragraph.append(f"{row.mean_difference:+.2f} point ({row.ci_low:+.2f} to {row.ci_high:+.2f}) with the {roster} roster")
        lines.append(f"With {capacity} elevator servers, the paired ARAD minus proximity difference in overall lateness is " +
                     " and ".join(paragraph) + ". The intervals are calculated from direct differences within each seed.")
    lines.append(f"Across all 24 scenarios in the two main grids, the ARAD minus proximity mean difference ranges from ${direct.mean_difference.min():+.2f}$ to ${direct.mean_difference.max():+.2f}$ percentage points. Pointwise intervals lie below zero in {int((direct.ci_high < 0).sum())} scenarios and above zero in {int((direct.ci_low > 0).sum())}; the other {int(((direct.ci_low <= 0) & (direct.ci_high >= 0)).sum())} cross zero. This is a descriptive summary of exploratory contrasts, without multiplicity correction. It does not identify one policy as uniformly preferable.")
    for capacity in (2, 4):
        statements = []
        for roster in ("high", "moderate"):
            row = source[(source.elevator_capacity == capacity) & (source.staffing_name == roster) &
                         (source.left == "deadline_hybrid") & (source.right == "adaptive_hybrid") &
                         (source.metric == "late_or_incomplete_fraction")].iloc[0]
            statements.append("$" + signed(row.mean_difference) + "$ point (" + signed(row.ci_low) + " to " + signed(row.ci_high) + f") with the {roster} roster")
        lines.append(f"Removing the three nonurgent ARAD conditions together changes overall lateness relative to ARAD by " +
                     " and ".join(statements) + f" at {capacity} servers. This contrast concerns the combined admission restriction. It cannot attribute an effect to an individual gate.")
    lines.append(r"The deadline and ARAD overall contrasts all have intervals crossing zero in these four cases. This limited separation is consistent with the scenario composition: specimens account for 84.06\% of requests and have priority 1, so both rules use the common gate for most requests. The extension does not establish that the nonurgent gates are unimportant under a different task mix.")
    lines.append(r"Coverage and support help interpret the service differences. A controller that admits more robot requests also schedules more handoff work on the transporter queue. The benchmark comparisons therefore evaluate complete workflows, rather than matched numbers of robot missions. Coverage is descriptive; dividing the overall policy effect by robot completions would not estimate a causal effect per robot trip, because assignments change subsequent queues and resource states.")
    (ROOT / "manuscript/operational_benchmark_results.tex").write_text("\n\n".join(lines) + "\n", encoding="ascii")
    manifest = {"created_at": datetime.now(UTC).isoformat(), "simulator_runs": len(data), "contrast_rows": len(contrasts),
                "conservation_checks_pass": True, "battery_checks_pass": True, "pairing_checks_pass": True,
                "method": "Direct differences paired by demand seed; Student t with 29 degrees of freedom",
                "outputs_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in [ROOT / "results/tables/reconciled_policy_contrasts.csv",
                                                ROOT / "results/tables/reconciled_policy_exposure.csv",
                                                ROOT / "manuscript/operational_benchmark_table.tex",
                                                ROOT / "manuscript/operational_benchmark_results.tex"]}}
    (ROOT / "results/manifests/reconciled_benchmark_analysis.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(direct[["elevator_capacity", "staffing_name", "task_mix", "specimen_deadline", "mean_difference", "ci_low", "ci_high"]].to_string(index=False))


if __name__ == "__main__":
    main()
