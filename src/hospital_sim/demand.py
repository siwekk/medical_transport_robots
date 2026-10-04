from __future__ import annotations

from bisect import bisect_left
from random import Random
from typing import Any

from .entities import DeliveryTask

DEFAULT_PARAMETERS = {
    "arrival_hour_weights": [1.0 / 24.0] * 24,
    "acuity_probabilities": {"1": 0.03, "2": 0.17, "3": 0.45, "4": 0.28, "5": 0.07},
    "task_mix": {"medication": 0.36, "specimen": 0.38, "supply": 0.18, "instrument": 0.08},
    "destination_weights": {
        "emergency": 0.24,
        "icu": 0.15,
        "medical_ward": 0.28,
        "surgical_ward": 0.23,
        "operating_suite": 0.10,
    },
}


def weighted_choice(weights: dict[str, float], rng: Random) -> str:
    labels = list(weights)
    return rng.choices(labels, weights=[weights[label] for label in labels], k=1)[0]


def build_task(
    task_id: int,
    created_at: float,
    config: dict[str, Any],
    parameters: dict[str, Any],
    rng: Random,
    observed_pair: tuple[str, str] | None = None,
) -> DeliveryTask:
    task_type = weighted_choice(parameters["task_mix"], rng)
    task_config = config["tasks"][task_type]
    priority_probabilities = parameters.get("task_priority_probabilities", {}).get(
        task_type
    )
    priority = (
        int(weighted_choice(priority_probabilities, rng))
        if priority_probabilities
        else int(task_config["priority"])
    )
    empirical = parameters.get("empirical_routes_by_hour")
    if observed_pair is not None:
        origin, destination = observed_pair
    elif empirical:
        hour_data = empirical[str(int(created_at // 60) % 24)]
        draw = rng.randint(1, int(hour_data["total"]))
        entries = hour_data["entries"]
        index = bisect_left(hour_data["cumulative_counts"], draw)
        origin, destination = str(entries[index][0]), str(entries[index][1])
    elif "origin" in task_config:
        origin = task_config["origin"]
    else:
        origin = weighted_choice(parameters["destination_weights"], rng)
        allowed = set(task_config["origins"])
        if origin not in allowed:
            origin = rng.choice(sorted(allowed))
    if observed_pair is not None or empirical:
        pass
    elif "destination" in task_config:
        destination = task_config["destination"]
    else:
        allowed_destinations = task_config["destinations"]
        candidate_weights = {
            location: parameters["destination_weights"].get(location, 0.1)
            for location in allowed_destinations
        }
        destination = weighted_choice(candidate_weights, rng)
    return DeliveryTask(
        task_id=task_id,
        created_at=created_at,
        task_type=task_type,
        origin=origin,
        destination=destination,
        priority=priority,
        deadline_minutes=float(task_config["deadline_minutes"]),
        loading_minutes=float(task_config["loading_minutes"]),
        receiving_minutes=float(task_config["receiving_minutes"]),
    )


def next_interarrival_minutes(
    now_minutes: float,
    base_rate_per_hour: float,
    demand_multiplier: float,
    hour_weights: list[float],
    rng: Random,
) -> float:
    hour = int(now_minutes // 60) % 24
    normalized_hour_factor = max(0.05, hour_weights[hour] * 24.0)
    rate = max(1e-6, base_rate_per_hour * demand_multiplier * normalized_hour_factor)
    return rng.expovariate(rate / 60.0)
