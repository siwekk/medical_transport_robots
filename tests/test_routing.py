import json
from random import Random

from hospital_sim.config import load_config
from hospital_sim.routing import HospitalRouter


def test_all_task_locations_are_connected():
    config = load_config("configs/hospital_reference.yaml")
    router = HospitalRouter(config["hospital"])
    estimate = router.estimate("pharmacy", "medical_ward")
    assert estimate.distance_m > 0
    assert estimate.elevator_legs >= 1


def test_travel_time_is_positive():
    config = load_config("configs/hospital_reference.yaml")
    router = HospitalRouter(config["hospital"])
    travel = router.travel_minutes(
        "laboratory",
        "icu",
        speed_m_per_min=60,
        elevator_cycle_minutes=2,
        congestion_sigma=0.1,
        rng=Random(1),
    )
    assert travel > 0


def test_router_uses_empirical_freiburg_route():
    config = load_config("configs/hospital_reference.yaml")
    with open("data/processed/combined_parameters.json", encoding="utf-8") as handle:
        parameters = json.load(handle)
    router = HospitalRouter(config["hospital"], parameters["empirical_routes_by_hour"])
    row = parameters["empirical_routes_by_hour"]["8"]["entries"][0]
    estimate = router.estimate(str(row[0]), str(row[1]))
    assert estimate.distance_m == row[2]
    assert estimate.elevator_legs == row[3]


def test_router_connects_empirical_nodes_without_a_direct_observed_route():
    config = load_config("configs/hospital_reference.yaml")
    with open("data/processed/combined_parameters.json", encoding="utf-8") as handle:
        parameters = json.load(handle)
    router = HospitalRouter(config["hospital"], parameters["empirical_routes_by_hour"])
    nodes = sorted(router.empirical_graph)
    origin, destination = next(
        (left, right)
        for left in nodes
        for right in nodes
        if left != right and (left, right) not in router.empirical_routes
    )
    estimate = router.estimate(origin, destination)
    assert estimate.distance_m > 0


def test_unobserved_pickup_route_uses_pathway_network():
    config = load_config("configs/hospital_reference.yaml")
    pathway = {
        "nodes": [["a", 0], ["b", 0], ["c", 2]],
        "edges": [
            ["a", "b", 4.0, "PATH"],
            ["b", "c", 1.0, "ELEVATOR"],
        ],
    }
    router = HospitalRouter(config["hospital"], None, pathway)
    estimate = router.estimate("a", "c")
    assert estimate.distance_m == 5.0
    assert estimate.elevator_rides == 1
    assert estimate.elevator_floors == 2
