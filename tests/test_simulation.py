from copy import deepcopy

from hospital_sim.config import load_config, scenario_config
from hospital_sim.demand import DEFAULT_PARAMETERS
from hospital_sim.simulation import run_simulation


def small_config(policy: str, fleet_size: int):
    base = load_config("configs/hospital_reference.yaml")
    base["model"]["warmup_days"] = 0
    base["model"]["simulation_days"] = 0.25
    base["model"]["base_requests_per_hour"] = 3
    return scenario_config(
        base,
        policy=policy,
        fleet_size=fleet_size,
        demand_multiplier=1.0,
        reliability=0.98,
    )


def test_human_only_has_no_robot_events():
    result = run_simulation(small_config("human_only", 0), deepcopy(DEFAULT_PARAMETERS), seed=10)
    assert result["created"] > 0
    assert result["robot_completed"] == 0
    assert result["human_completed"] == result["completed"]
    assert result["created"] == result["completed"] + result["incomplete"]
    assert result["human_contiguous_idle_minutes"] <= result["human_idle_minutes"]


def test_robot_policy_is_reproducible():
    config = small_config("robot_first", 2)
    first = run_simulation(config, deepcopy(DEFAULT_PARAMETERS), seed=11)
    second = run_simulation(config, deepcopy(DEFAULT_PARAMETERS), seed=11)
    assert first == second
    assert first["robot_completed"] > 0


def test_released_time_identity():
    config = small_config("robot_first", 1)
    result = run_simulation(config, deepcopy(DEFAULT_PARAMETERS), seed=12)
    expected = result["gross_manual_minutes_avoided"] - result["robot_human_burden_minutes"]
    assert abs(result["net_released_minutes"] - expected) < 1e-9


def test_unfinished_requests_count_against_service_level():
    config = small_config("robot_first", 1)
    config["model"]["base_requests_per_hour"] = 200
    result = run_simulation(config, deepcopy(DEFAULT_PARAMETERS), seed=13)
    assert result["incomplete"] > 0
    assert result["late_or_incomplete_fraction"] >= result["late_fraction"]


def test_explicit_elevator_contention_records_requests():
    config = load_config("configs/hospital_reference.yaml")
    config["model"]["warmup_days"] = 0
    config["model"]["simulation_days"] = 0.5
    config["hospital"]["explicit_elevator_contention"] = True
    config["hospital"]["elevator_bank_capacity"] = 1
    config = scenario_config(
        config,
        policy="overflow_hybrid",
        fleet_size=2,
        demand_multiplier=2.0,
        reliability=0.98,
    )
    result = run_simulation(config, seed=17)
    assert result["elevator_requests"] > 0
    assert result["mean_elevator_wait_minutes"] >= 0


def test_battery_model_records_energy_and_charging():
    config = small_config("overflow_hybrid", 2)
    config["model"]["simulation_days"] = 1
    config["model"]["base_requests_per_hour"] = 12
    config["robots"].update(
        {
            "battery_enabled": True,
            "battery_capacity_kwh": 1.0,
            "energy_kwh_per_active_minute": 0.10,
            "charge_trigger_fraction": 0.90,
            "charging_station_count": 1,
            "charging_power_kw": 2.0,
            "charging_efficiency": 0.90,
        }
    )
    result = run_simulation(config, seed=19)
    assert result["robot_energy_kwh"] > 0
    assert result["charging_sessions"] > 0
    assert result["charging_minutes"] > 0


def test_batch_policy_accounts_for_every_robot_request():
    config = small_config("batch_overflow", 2)
    config["model"]["simulation_days"] = 1
    config["model"]["base_requests_per_hour"] = 12
    config["robots"].update(
        {
            "batch_window_minutes": 10.0,
            "batch_max_size": 4,
            "batch_additional_handling_minutes": 0.5,
            "batch_urgent": False,
        }
    )
    result = run_simulation(config, seed=23)
    assert result["robot_batches"] > 0
    assert result["batched_requests"] == result["robot_completed"]
    assert result["mean_robot_batch_size"] >= 1


def test_infection_control_records_decontamination():
    config = small_config("overflow_hybrid", 2)
    config["model"]["simulation_days"] = 1
    config["model"]["base_requests_per_hour"] = 12
    config["robots"].update(
        {
            "infection_control_enabled": True,
            "decontamination_task_types": [
                "medication",
                "specimen",
                "supply",
                "instrument",
            ],
            "decontamination_compliance": 1.0,
            "decontamination_minutes": 5.0,
            "decontamination_station_count": 1,
        }
    )
    result = run_simulation(config, seed=29)
    assert result["decontamination_sessions"] > 0
    assert result["decontamination_minutes"] > 0


def test_priority_policy_does_not_queue_behind_charging_robots():
    config = small_config("priority_hybrid", 1)
    config["model"]["simulation_days"] = 1
    config["model"]["base_requests_per_hour"] = 12
    config["robots"].update(
        {
            "battery_enabled": True,
            "battery_capacity_kwh": 1.0,
            "energy_kwh_per_active_minute": 0.10,
            "charge_trigger_fraction": 0.90,
            "charging_station_count": 1,
            "charging_power_kw": 1.0,
            "charging_efficiency": 0.90,
        }
    )
    result = run_simulation(config, seed=31)
    assert result["incomplete"] == 0
    assert result["late_or_incomplete_fraction"] < 0.5


def test_calibrated_staffing_configuration_loads():
    config = load_config("configs/hospital_freiburg_calibrated.yaml")
    counts = {shift["name"]: shift["count"] for shift in config["hospital"]["human_staffing"]}
    assert counts == {"night": 1, "day": 7, "evening": 6}


def test_overflow_policy_uses_robot_when_no_human_is_idle():
    config = small_config("overflow_hybrid", 1)
    config["hospital"]["human_staffing"] = [
        {"name": "continuous", "start_hour": 0, "end_hour": 24, "count": 1}
    ]
    config["model"]["base_requests_per_hour"] = 30
    result = run_simulation(config, deepcopy(DEFAULT_PARAMETERS), seed=14)
    assert result["robot_completed"] > 0
    assert result["human_completed"] > 0


def test_explicit_support_repositioning_battery_and_passengers_are_accounted():
    config = small_config("deadline_hybrid", 2)
    config["model"]["simulation_days"] = 1
    config["model"]["base_requests_per_hour"] = 18
    config["hospital"].update(
        {
            "explicit_elevator_contention": True,
            "elevator_bank_capacity": 2,
            "repositioning_enabled": True,
            "human_initial_node": "pharmacy",
            "passenger_elevator_arrivals_per_hour": 6.0,
            "passenger_elevator_service_minutes": 2.2,
        }
    )
    config["robots"].update(
        {
            "battery_enabled": True,
            "battery_capacity_kwh": 0.8,
            "energy_kwh_per_active_minute": 0.04,
            "charge_trigger_fraction": 0.25,
            "charging_station_count": 2,
            "charging_power_kw": 2.0,
            "charging_efficiency": 0.90,
            "enforce_battery_feasibility": True,
            "repositioning_enabled": True,
            "initial_node": "pharmacy",
            "schedule_human_support": True,
            "decontamination_human_minutes": 1.0,
        }
    )
    result = run_simulation(config, deepcopy(DEFAULT_PARAMETERS), seed=41)
    assert result["robot_completed"] > 0
    assert result["human_support_tasks"] > 0
    assert result["human_support_minutes"] > 0
    assert result["robot_empty_travel_minutes"] > 0
    assert result["human_empty_travel_minutes"] > 0
    assert result["passenger_elevator_requests"] > 0
    assert result["battery_infeasible_missions"] == 0


def test_location_aware_dispatch_accounts_for_pickup_and_support_travel():
    config = small_config("deadline_hybrid", 2)
    config["model"]["simulation_days"] = 1
    config["model"]["base_requests_per_hour"] = 12
    config["hospital"].update(
        {
            "location_aware_assignment": True,
            "repositioning_enabled": True,
            "human_initial_node": "pharmacy",
        }
    )
    config["robots"].update(
        {
            "battery_enabled": True,
            "battery_capacity_kwh": 4.0,
            "energy_kwh_per_active_minute": 0.025,
            "charge_trigger_fraction": 0.25,
            "charging_station_count": 2,
            "charging_power_kw": 2.0,
            "charging_efficiency": 0.9,
            "schedule_human_support": True,
            "repositioning_enabled": True,
            "location_aware_assignment": True,
            "initial_node": "pharmacy",
        }
    )
    result = run_simulation(config, deepcopy(DEFAULT_PARAMETERS), seed=101)
    assert result["created"] == result["completed"] + result["incomplete"]
    assert result["human_empty_travel_minutes"] > 0
    assert result["human_support_travel_minutes"] > 0
    assert 0 <= result["human_utilization"] <= 1
    assert result["human_contiguous_idle_minutes"] <= result["human_idle_minutes"]
