from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from random import Random
from typing import Any

import simpy

from .demand import DEFAULT_PARAMETERS, build_task, next_interarrival_minutes
from .entities import (
    DeliveryTask,
    HumanAssignment,
    MetricAccumulator,
    SupportAssignment,
)
from .routing import HospitalRouter


def _stable_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


class HospitalSimulation:
    def __init__(
        self,
        config: dict[str, Any],
        parameters: dict[str, Any] | None = None,
        *,
        seed: int,
    ):
        self.config = config
        self.parameters = parameters or DEFAULT_PARAMETERS
        self.seed = int(seed)
        self.rng = Random(self.seed)
        self.demand_rng = Random(self.seed + 10_000_019)
        self.passenger_rng = Random(self.seed + 20_000_033)
        self.env = simpy.Environment()
        self.router = HospitalRouter(
            config["hospital"],
            self.parameters.get("empirical_routes_by_hour"),
            self.parameters.get("pathway_network"),
        )
        self.metrics = MetricAccumulator()
        self.event_log: list[dict[str, Any]] = []
        self.task_counter = 0
        self.queue_counter = 0
        self.measurement_task_ids: set[int] = set()
        self.completed_task_ids: set[int] = set()
        self.active_human_workers = 0
        self.busy_human_workers = 0
        self.measurement_start = float(config["model"]["warmup_days"]) * 24.0 * 60.0
        self.measurement_end = self.measurement_start + float(
            config["model"]["simulation_days"]
        ) * 24.0 * 60.0
        self.staffed_intervals: list[tuple[str, float, float]] = []
        self.human_busy_intervals: list[tuple[str, float, float]] = []
        self.location_aware_humans = bool(
            config["hospital"].get("location_aware_assignment", False)
        )
        self.available_humans: dict[str, tuple[str | None, simpy.Event]] = {}
        human_capacity = int(config["hospital"]["human_transporters"])
        robot_capacity = max(1, int(config["robots"]["fleet_size"]))
        self.humans = simpy.PriorityResource(self.env, capacity=human_capacity)
        self.human_queue = simpy.PriorityStore(self.env)
        self.robots = simpy.PriorityResource(self.env, capacity=robot_capacity)
        self.battery_enabled = bool(config["robots"].get("battery_enabled", False))
        self.robot_units = simpy.FilterStore(self.env, capacity=robot_capacity)
        battery_capacity = float(config["robots"].get("battery_capacity_kwh", 4.0))
        robot_initial_node = config["robots"].get("initial_node")
        for robot_id in range(int(config["robots"]["fleet_size"])):
            self.robot_units.put(
                {
                    "robot_id": robot_id,
                    "battery_kwh": battery_capacity,
                    "location": robot_initial_node,
                }
            )
        charging_capacity = max(
            1, int(config["robots"].get("charging_station_count", 1))
        )
        self.chargers = simpy.Resource(self.env, capacity=charging_capacity)
        decontamination_capacity = max(
            1, int(config["robots"].get("decontamination_station_count", 1))
        )
        self.decontamination_stations = simpy.Resource(
            self.env, capacity=decontamination_capacity
        )
        elevator_capacity = max(
            1, int(config["hospital"].get("elevator_bank_capacity", 1))
        )
        self.elevators = simpy.Resource(self.env, capacity=elevator_capacity)
        self.robot_batch_queue = simpy.Store(self.env)

    def _next_queue_counter(self) -> int:
        self.queue_counter += 1
        return self.queue_counter

    def _travel_minutes(self, origin: str, destination: str, *, robot: bool) -> float:
        hospital = self.config["hospital"]
        speed_key = "robot_speed_m_per_min" if robot else "human_speed_m_per_min"
        return self.router.travel_minutes(
            origin,
            destination,
            speed_m_per_min=float(hospital[speed_key]),
            elevator_cycle_minutes=float(hospital["elevator_cycle_minutes"]),
            elevator_travel_per_floor_minutes=float(
                hospital.get("elevator_travel_per_floor_minutes", 0.0)
            ),
            congestion_sigma=float(hospital["congestion_sigma"]),
            rng=self.rng,
        )

    def _deterministic_travel_minutes(
        self, origin: str, destination: str, *, robot: bool
    ) -> float:
        hospital = self.config["hospital"]
        route = self.router.estimate(origin, destination)
        speed_key = "robot_speed_m_per_min" if robot else "human_speed_m_per_min"
        return (
            route.distance_m / float(hospital[speed_key])
            + route.elevator_rides * float(hospital["elevator_cycle_minutes"])
            + route.elevator_floors
            * float(hospital.get("elevator_travel_per_floor_minutes", 0.0))
        )

    def _request_human_support(
        self, task: DeliveryTask, duration_minutes: float, support_kind: str
    ):
        if duration_minutes <= 0:
            return 0.0
        completed = self.env.event()
        assignment = SupportAssignment(
            task=task,
            duration_minutes=float(duration_minutes),
            support_kind=support_kind,
            queued_at=self.env.now,
            completed=completed,
        )
        queued_at = self.env.now
        yield self.human_queue.put(
            (
                task.priority,
                task.due_at,
                self._next_queue_counter(),
                assignment,
            )
        )
        if self.location_aware_humans:
            self._dispatch_nearest_human()
        yield completed
        return self.env.now - queued_at

    def _decontaminate_if_needed(self, tasks: list[DeliveryTask]):
        robots = self.config["robots"]
        if not robots.get("infection_control_enabled", False):
            return
        restricted = set(robots.get("decontamination_task_types", []))
        if not any(task.task_type in restricted for task in tasks):
            return
        if self.rng.random() > float(robots.get("decontamination_compliance", 1.0)):
            return
        if robots.get("schedule_human_support", False):
            support_minutes = float(
                robots.get("decontamination_human_minutes", 0.0)
            )
            if support_minutes > 0:
                yield from self._request_human_support(
                    tasks[0], support_minutes, "decontamination"
                )
        queued_at = self.env.now
        with self.decontamination_stations.request() as request:
            yield request
            wait = self.env.now - queued_at
            duration = float(robots.get("decontamination_minutes", 5.0))
            if any(task.created_at >= self.measurement_start for task in tasks):
                self.metrics.decontamination_sessions += 1
                self.metrics.decontamination_wait_minutes += wait
                self.metrics.decontamination_minutes += duration
            yield self.env.timeout(duration)

    def _execute_route(
        self,
        origin: str,
        destination: str,
        planned_duration: float,
        *,
        measured: bool,
    ):
        hospital = self.config["hospital"]
        if not hospital.get("explicit_elevator_contention", False):
            yield self.env.timeout(planned_duration)
            return planned_duration
        route = self.router.estimate(origin, destination)
        elevator_service = (
            route.elevator_rides * float(hospital["elevator_cycle_minutes"])
            + route.elevator_floors
            * float(hospital.get("elevator_travel_per_floor_minutes", 0.0))
        )
        if elevator_service <= 0:
            yield self.env.timeout(planned_duration)
            return planned_duration
        non_elevator = max(0.0, planned_duration - elevator_service)
        yield self.env.timeout(non_elevator / 2.0)
        queued_at = self.env.now
        with self.elevators.request() as request:
            yield request
            elevator_wait = self.env.now - queued_at
            if measured:
                self.metrics.elevator_requests += 1
                self.metrics.elevator_wait_minutes += elevator_wait
            yield self.env.timeout(elevator_service)
        yield self.env.timeout(non_elevator / 2.0)
        return planned_duration + elevator_wait

    def _execute_transport(self, task: DeliveryTask, planned_duration: float):
        return (
            yield from self._execute_route(
                task.origin,
                task.destination,
                planned_duration,
                measured=task.created_at >= self.measurement_start,
            )
        )

    def _manual_duration(self, task: DeliveryTask) -> float:
        outbound = self._travel_minutes(task.origin, task.destination, robot=False)
        return task.loading_minutes + outbound + task.receiving_minutes

    def _robot_duration(self, task: DeliveryTask) -> float:
        return (
            task.loading_minutes
            + self._travel_minutes(task.origin, task.destination, robot=True)
            + task.receiving_minutes
        )

    def _deterministic_robot_duration(self, task: DeliveryTask) -> float:
        return (
            task.loading_minutes
            + self._deterministic_travel_minutes(
                task.origin, task.destination, robot=True
            )
            + task.receiving_minutes
        )

    def _deterministic_human_duration(self, task: DeliveryTask) -> float:
        return (
            task.loading_minutes
            + self._deterministic_travel_minutes(
                task.origin, task.destination, robot=False
            )
            + task.receiving_minutes
        )

    def _nearest_idle_robot(self, origin: str) -> dict[str, Any] | None:
        if not self.robot_units.items:
            return None
        if not self.config["robots"].get("location_aware_assignment", False):
            return self.robot_units.items[0]
        return min(
            self.robot_units.items,
            key=lambda robot: (
                self._deterministic_travel_minutes(
                    str(robot["location"]), origin, robot=True
                )
                if robot.get("location") is not None
                else 0.0,
                robot["robot_id"],
            ),
        )

    def _robot_eligible(self, task: DeliveryTask) -> bool:
        scenario = self.config.get("scenario", {})
        policy = scenario.get("policy", "robot_first")
        fleet_size = int(self.config["robots"]["fleet_size"])
        if policy == "human_only" or fleet_size <= 0:
            return False
        if policy == "travel_aware_hybrid":
            robot = self._nearest_idle_robot(task.origin)
            if robot is None:
                return False
            robot_pickup = (
                self._deterministic_travel_minutes(
                    str(robot["location"]), task.origin, robot=True
                )
                if self.config["robots"].get("repositioning_enabled", False)
                and robot.get("location") is not None
                else 0.0
            )
            robot_eta = robot_pickup + self._deterministic_robot_duration(task)
            if robot_eta > task.due_at - self.env.now:
                return False
            human_direct = self._deterministic_human_duration(task)
            if self.location_aware_humans and self.available_humans:
                pickup = min(
                    self._deterministic_travel_minutes(
                        str(location), task.origin, robot=False
                    )
                    if location is not None else 0.0
                    for location, _ in self.available_humans.values()
                )
                human_eta = pickup + human_direct
            else:
                queue_depth = len(self.human_queue.items) + 1
                human_eta = human_direct * (
                    1.5 + queue_depth / max(1, self.active_human_workers)
                )
            support_minutes = (
                task.loading_minutes
                + task.receiving_minutes
                + float(self.config["robots"].get("coordination_minutes_mean", 2.0))
            )
            support_pressure = len(self.human_queue.items) / max(
                1, self.active_human_workers
            )
            return robot_eta + support_minutes * (1.0 + support_pressure) < human_eta
        if policy in {"overflow_hybrid", "batch_overflow"}:
            threshold = int(self.config["robots"].get("human_queue_threshold", 1))
            idle_workers = max(
                0, self.active_human_workers - self.busy_human_workers
            )
            human_overloaded = (
                idle_workers == 0 or len(self.human_queue.items) >= threshold
            )
            robot_available = (
                bool(self.robot_units.items)
                if self.battery_enabled
                else self.robots.count < fleet_size
            )
            return human_overloaded and robot_available
        if policy == "priority_hybrid":
            if self.battery_enabled and not self.robot_units.items:
                return False
            estimated = self._robot_duration(task)
            queue_delay = len(self.robots.queue) * estimated / max(1, fleet_size)
            slack = task.due_at - self.env.now
            return task.priority > 1 or estimated + queue_delay <= 0.75 * slack
        if policy in {"adaptive_hybrid", "deadline_hybrid"}:
            idle_workers = max(
                0, self.active_human_workers - self.busy_human_workers
            )
            if self.battery_enabled:
                available_robots = len(self.robot_units.items)
            else:
                available_robots = max(0, fleet_size - self.robots.count)
            if available_robots <= 0:
                return False
            predicted = self._deterministic_robot_duration(task)
            if self.config["robots"].get("repositioning_enabled", False):
                selected_robot = self._nearest_idle_robot(task.origin)
                robot_location = selected_robot.get("location") if selected_robot else None
                if robot_location and robot_location != task.origin:
                    predicted += self._deterministic_travel_minutes(
                        str(robot_location), task.origin, robot=True
                    )
            elevator_pressure = (
                self.elevators.count + len(self.elevators.queue)
            ) / self.elevators.capacity
            support_pressure = max(
                (self.chargers.count + len(self.chargers.queue))
                / self.chargers.capacity,
                (
                    self.decontamination_stations.count
                    + len(self.decontamination_stations.queue)
                )
                / self.decontamination_stations.capacity,
            )
            slack = task.due_at - self.env.now
            elevator_inflation = float(
                self.config["robots"].get("adaptive_elevator_inflation", 0.15)
            )
            if policy == "deadline_hybrid":
                return (
                    idle_workers == 0
                    and predicted * (1.0 + elevator_inflation * elevator_pressure)
                    <= slack
                )
            if task.priority == 1:
                return (
                    idle_workers == 0
                    and predicted
                    * (1.0 + elevator_inflation * elevator_pressure)
                    <= slack
                )
            reserve = min(
                int(self.config["robots"].get("adaptive_robot_reserve", 1)),
                max(0, fleet_size - 1),
            )
            human_pressure = len(self.human_queue.items) / max(
                1, self.active_human_workers
            )
            return (
                idle_workers == 0
                and human_pressure >= float(
                    self.config["robots"].get("adaptive_human_pressure", 0.25)
                )
                and available_robots > reserve
                and support_pressure
                <= float(
                    self.config["robots"].get("adaptive_support_pressure", 1.0)
                )
                and predicted * (1.0 + elevator_inflation * elevator_pressure)
                <= slack
            )
        return True

    def _complete(
        self,
        task: DeliveryTask,
        mode: str,
        started: float,
        duration: float,
        worker_id: str | None = None,
    ) -> None:
        if task.created_at < self.measurement_start:
            return
        completed_at = self.env.now
        self.completed_task_ids.add(task.task_id)
        late = completed_at > task.due_at
        self.metrics.completed += 1
        self.metrics.late += int(late)
        self.metrics.total_wait_minutes += started - task.created_at
        self.metrics.total_cycle_minutes += completed_at - task.created_at
        if task.priority == 1:
            self.metrics.urgent_completed += 1
            self.metrics.urgent_late += int(late)
        self.event_log.append(
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "origin": task.origin,
                "destination": task.destination,
                "mode": mode,
                "priority": task.priority,
                "created_at": task.created_at,
                "due_at": task.due_at,
                "started_at": started,
                "completed_at": completed_at,
                "duration": duration,
                "late": late,
                "worker_id": worker_id,
            }
        )

    def _serve_human(self, task: DeliveryTask):
        completed = self.env.event()
        assignment = HumanAssignment(task=task, completed=completed)
        yield self.human_queue.put(
            (
                task.priority,
                task.due_at,
                self._next_queue_counter(),
                assignment,
            )
        )
        if self.location_aware_humans:
            self._dispatch_nearest_human()
        yield completed

    def _support_node(self, assignment: SupportAssignment) -> str:
        if assignment.support_kind == "loading_coordination_recovery":
            return assignment.task.origin
        return assignment.task.destination

    def _dispatch_nearest_human(self) -> None:
        """Give the highest priority request to the closest idle transporter."""
        while self.available_humans and self.human_queue.items:
            _, _, _, assignment = self.human_queue.items.pop(0)
            pickup = (
                self._support_node(assignment)
                if isinstance(assignment, SupportAssignment)
                else assignment.task.origin
            )
            worker_id = min(
                self.available_humans,
                key=lambda identity: (
                    self._deterministic_travel_minutes(
                        self.available_humans[identity][0], pickup, robot=False
                    )
                    if self.available_humans[identity][0] is not None
                    else 0.0,
                    identity,
                ),
            )
            _, ready = self.available_humans.pop(worker_id)
            self.busy_human_workers += 1
            ready.succeed(assignment)

    def _location_aware_human_worker(self, start: float, end: float, worker_id: str):
        if start > self.env.now:
            yield self.env.timeout(start - self.env.now)
        staffed_start = max(start, self.measurement_start)
        staffed_end = min(end, self.measurement_end)
        if staffed_end > staffed_start:
            self.staffed_intervals.append((worker_id, staffed_start, staffed_end))
        self.active_human_workers += 1
        worker_location = self.config["hospital"].get("human_initial_node")
        while self.env.now < end:
            ready = self.env.event()
            self.available_humans[worker_id] = (worker_location, ready)
            self._dispatch_nearest_human()
            shift_end = self.env.timeout(max(0.0, end - self.env.now))
            events = yield ready | shift_end
            if ready not in events:
                self.available_humans.pop(worker_id, None)
                break
            assignment = ready.value
            task = assignment.task
            started = self.env.now
            pickup = (
                self._support_node(assignment)
                if isinstance(assignment, SupportAssignment)
                else task.origin
            )
            reposition = 0.0
            if worker_location is not None and worker_location != pickup:
                planned = self._travel_minutes(str(worker_location), pickup, robot=False)
                reposition = yield from self._execute_route(
                    str(worker_location),
                    pickup,
                    planned,
                    measured=task.created_at >= self.measurement_start,
                )
            worker_location = pickup
            if isinstance(assignment, SupportAssignment):
                work_started = self.env.now
                yield self.env.timeout(assignment.duration_minutes)
                if task.created_at >= self.measurement_start:
                    self.metrics.human_support_tasks += 1
                    self.metrics.human_support_minutes += assignment.duration_minutes
                    self.metrics.human_support_travel_minutes += reposition
                    self.metrics.human_empty_travel_minutes += reposition
                    self.metrics.human_support_wait_minutes += max(
                        0.0, work_started - assignment.queued_at
                    )
            else:
                planned = self._manual_duration(task)
                actual = yield from self._execute_transport(task, planned)
                worker_location = task.destination
                if task.created_at >= self.measurement_start:
                    self.metrics.human_completed += 1
                    self.metrics.human_transport_minutes += actual + reposition
                    self.metrics.human_empty_travel_minutes += reposition
                self._complete(task, "human", started, actual + reposition, worker_id)
            busy_start = max(started, self.measurement_start)
            busy_end = min(self.env.now, self.measurement_end)
            if busy_end > busy_start:
                self.human_busy_intervals.append((worker_id, busy_start, busy_end))
            self.busy_human_workers -= 1
            assignment.completed.succeed()
        self.active_human_workers -= 1

    def _human_worker(self, start: float, end: float, worker_id: str):
        if start > self.env.now:
            yield self.env.timeout(start - self.env.now)
        staffed_start = max(start, self.measurement_start)
        staffed_end = min(end, self.measurement_end)
        if staffed_end > staffed_start:
            self.staffed_intervals.append((worker_id, staffed_start, staffed_end))
        self.active_human_workers += 1
        worker_location = self.config["hospital"].get("human_initial_node")
        while self.env.now < end:
            get_event = self.human_queue.get()
            shift_end = self.env.timeout(end - self.env.now)
            yield get_event | shift_end
            if not get_event.triggered:
                get_event.cancel()
                break
            _, _, _, assignment = get_event.value
            task = assignment.task
            started = self.env.now
            if isinstance(assignment, SupportAssignment):
                self.busy_human_workers += 1
                yield self.env.timeout(assignment.duration_minutes)
                self.busy_human_workers -= 1
                busy_start = max(started, self.measurement_start)
                busy_end = min(self.env.now, self.measurement_end)
                if busy_end > busy_start:
                    self.human_busy_intervals.append(
                        (worker_id, busy_start, busy_end)
                    )
                if task.created_at >= self.measurement_start:
                    self.metrics.human_support_tasks += 1
                    self.metrics.human_support_minutes += assignment.duration_minutes
                    self.metrics.human_support_wait_minutes += max(
                        0.0, started - assignment.queued_at
                    )
                assignment.completed.succeed()
                continue
            reposition = 0.0
            if (
                self.config["hospital"].get("repositioning_enabled", False)
                and worker_location
                and worker_location != task.origin
            ):
                reposition = self._travel_minutes(
                    str(worker_location), task.origin, robot=False
                )
            duration = self._manual_duration(task)
            self.busy_human_workers += 1
            if reposition > 0:
                reposition = yield from self._execute_route(
                    str(worker_location),
                    task.origin,
                    reposition,
                    measured=task.created_at >= self.measurement_start,
                )
            duration = yield from self._execute_transport(task, duration)
            duration += reposition
            self.busy_human_workers -= 1
            worker_location = task.destination
            busy_start = max(started, self.measurement_start)
            busy_end = min(self.env.now, self.measurement_end)
            if busy_end > busy_start:
                self.human_busy_intervals.append((worker_id, busy_start, busy_end))
            if task.created_at >= self.measurement_start:
                self.metrics.human_completed += 1
                self.metrics.human_transport_minutes += duration
                self.metrics.human_empty_travel_minutes += reposition
            self._complete(task, "human", started, duration, worker_id)
            assignment.completed.succeed()
        self.active_human_workers -= 1

    def _start_human_shifts(self, total_minutes: float) -> None:
        staffing = self.config["hospital"].get("human_staffing")
        if not staffing:
            staffing = [
                {
                    "name": "continuous",
                    "start_hour": 0,
                    "end_hour": 24,
                    "count": int(self.config["hospital"]["human_transporters"]),
                }
            ]
        days = int(total_minutes // (24 * 60)) + 2
        for day in range(-1, days):
            for shift in staffing:
                start_hour = float(shift["start_hour"])
                end_hour = float(shift["end_hour"])
                start = day * 24 * 60 + start_hour * 60
                end_day = day + int(end_hour <= start_hour)
                end = end_day * 24 * 60 + end_hour * 60
                if end <= 0 or start >= total_minutes + 24 * 60:
                    continue
                for worker_number in range(int(shift["count"])):
                    worker_id = f"d{day}:{shift['name']}:{worker_number}"
                    worker = (
                        self._location_aware_human_worker
                        if self.location_aware_humans
                        else self._human_worker
                    )
                    self.env.process(worker(max(0.0, start), end, worker_id))

    def _idle_capacity(self) -> tuple[float, float]:
        threshold = float(self.config["model"]["usable_block_minutes"])
        busy_by_worker: dict[str, list[tuple[float, float]]] = {}
        for worker_id, start, end in self.human_busy_intervals:
            busy_by_worker.setdefault(worker_id, []).append((start, end))
        total_idle = 0.0
        contiguous_idle = 0.0
        for worker_id, shift_start, shift_end in self.staffed_intervals:
            cursor = shift_start
            for busy_start, busy_end in sorted(busy_by_worker.get(worker_id, [])):
                busy_start = max(shift_start, busy_start)
                busy_end = min(shift_end, busy_end)
                if busy_end <= busy_start:
                    continue
                gap = max(0.0, busy_start - cursor)
                total_idle += gap
                if gap >= threshold:
                    contiguous_idle += gap
                cursor = max(cursor, busy_end)
            gap = max(0.0, shift_end - cursor)
            total_idle += gap
            if gap >= threshold:
                contiguous_idle += gap
        return total_idle, contiguous_idle

    def _charge_robot(self, robot: dict[str, Any]):
        robots = self.config["robots"]
        queued_at = self.env.now
        with self.chargers.request() as request:
            yield request
            wait = self.env.now - queued_at
            energy = float(robots["battery_capacity_kwh"]) - robot["battery_kwh"]
            charge_minutes = (
                energy
                / float(robots["charging_power_kw"])
                / float(robots.get("charging_efficiency", 0.9))
                * 60.0
            )
            if self.env.now >= self.measurement_start:
                self.metrics.charging_sessions += 1
                self.metrics.charging_wait_minutes += wait
                self.metrics.charging_minutes += charge_minutes
            yield self.env.timeout(charge_minutes)
            robot["battery_kwh"] = float(robots["battery_capacity_kwh"])

    def _charge_and_return(self, robot: dict[str, Any]):
        yield from self._charge_robot(robot)
        yield self.robot_units.put(robot)

    def _serve_robot_acquired(
        self, task: DeliveryTask, robot: dict[str, Any] | None = None
    ):
        manual_counterfactual = self._manual_duration(task)
        planned_duration = self._robot_duration(task)
        reposition = 0.0
        robot_location = robot.get("location") if robot is not None else None
        if (
            robot is not None
            and self.config["robots"].get("repositioning_enabled", False)
            and robot_location
            and robot_location != task.origin
        ):
            reposition = self._travel_minutes(
                str(robot_location), task.origin, robot=True
            )
        energy = 0.0
        if robot is not None:
            energy = (planned_duration + reposition) * float(
                self.config["robots"]["energy_kwh_per_active_minute"]
            )
            if (
                self.config["robots"].get("enforce_battery_feasibility", False)
                and robot["battery_kwh"] + 1e-12 < energy
            ):
                if task.created_at >= self.measurement_start:
                    self.metrics.battery_forced_charges += 1
                yield from self._charge_robot(robot)
            if robot["battery_kwh"] + 1e-12 < energy:
                if task.created_at >= self.measurement_start:
                    self.metrics.battery_infeasible_missions += 1
                raise RuntimeError(
                    "Mission energy exceeds full battery capacity under enforced feasibility"
                )
        started = self.env.now
        coordination = max(
            0.0,
            self.rng.lognormvariate(0.0, 0.30)
            * float(self.config["robots"]["coordination_minutes_mean"]),
        )
        reliable = self.rng.random() <= float(self.config["robots"]["reliability"])
        recovery = 0.0
        if not reliable:
            if task.created_at >= self.measurement_start:
                self.metrics.failures += 1
                self.metrics.human_interventions += 1
            recovery_mean = float(self.config["robots"]["failure_recovery_minutes_mean"])
            recovery = self.rng.expovariate(1.0 / recovery_mean)
            coordination += recovery
        schedule_support = bool(
            self.config["robots"].get("schedule_human_support", False)
        )
        if schedule_support:
            if reposition > 0:
                yield from self._execute_route(
                    str(robot_location),
                    task.origin,
                    reposition,
                    measured=task.created_at >= self.measurement_start,
                )
            yield from self._request_human_support(
                task,
                task.loading_minutes + coordination,
                "loading_coordination_recovery",
            )
            travel_duration = max(
                0.0,
                planned_duration - task.loading_minutes - task.receiving_minutes,
            )
            yield from self._execute_route(
                task.origin,
                task.destination,
                travel_duration,
                measured=task.created_at >= self.measurement_start,
            )
            yield from self._request_human_support(
                task, task.receiving_minutes, "receiving"
            )
            duration = self.env.now - started
            human_burden = coordination + task.loading_minutes + task.receiving_minutes
        else:
            duration = planned_duration + recovery
            reposition_actual = 0.0
            if reposition > 0:
                reposition_actual = yield from self._execute_route(
                    str(robot_location),
                    task.origin,
                    reposition,
                    measured=task.created_at >= self.measurement_start,
                )
            mission_actual = yield from self._execute_transport(task, duration)
            duration = reposition_actual + mission_actual
            human_burden = coordination
        if robot is not None:
            remaining = robot["battery_kwh"] - energy
            if remaining < -1e-9:
                if task.created_at >= self.measurement_start:
                    self.metrics.battery_infeasible_missions += 1
                if self.config["robots"].get("enforce_battery_feasibility", False):
                    raise RuntimeError("Battery state became negative")
            robot["battery_kwh"] = max(0.0, remaining)
            robot["location"] = task.destination
        if task.created_at >= self.measurement_start:
            self.metrics.robot_completed += 1
            self.metrics.robot_busy_minutes += duration
            self.metrics.gross_manual_minutes_avoided += manual_counterfactual
            self.metrics.robot_human_burden_minutes += human_burden
            self.metrics.robot_energy_kwh += energy
            self.metrics.robot_empty_travel_minutes += reposition
        self._complete(task, "robot", started, duration)
        yield from self._decontaminate_if_needed([task])

    def _serve_robot(self, task: DeliveryTask):
        if self.battery_enabled:
            selected_robot = self._nearest_idle_robot(task.origin)
            if selected_robot is None:
                robot = yield self.robot_units.get()
            else:
                robot = yield self.robot_units.get(
                    lambda item: item is selected_robot
                )
            yield from self._serve_robot_acquired(task, robot)
            robots = self.config["robots"]
            trigger = float(robots["battery_capacity_kwh"]) * float(
                robots["charge_trigger_fraction"]
            )
            if robot["battery_kwh"] <= trigger:
                self.env.process(self._charge_and_return(robot))
            else:
                yield self.robot_units.put(robot)
            return
        priority = (task.priority, task.due_at, task.task_id)
        with self.robots.request(priority=priority) as request:
            yield request
            yield from self._serve_robot_acquired(task)

    def _serve_robot_batch_acquired(
        self,
        assignments: list[tuple[DeliveryTask, simpy.Event]],
        robot: dict[str, float] | None = None,
    ):
        tasks = [assignment[0] for assignment in assignments]
        lead = tasks[0]
        started = self.env.now
        manual_counterfactuals = [self._manual_duration(task) for task in tasks]
        planned_duration = self._robot_duration(lead) + (
            len(tasks) - 1
        ) * float(self.config["robots"].get("batch_additional_handling_minutes", 0.5))
        energy = 0.0
        if robot is not None:
            energy = planned_duration * float(
                self.config["robots"]["energy_kwh_per_active_minute"]
            )
        duration = planned_duration
        coordination = max(
            0.0,
            self.rng.lognormvariate(0.0, 0.30)
            * float(self.config["robots"]["coordination_minutes_mean"]),
        )
        reliable = self.rng.random() <= float(self.config["robots"]["reliability"])
        if not reliable:
            measured_tasks = sum(
                task.created_at >= self.measurement_start for task in tasks
            )
            if measured_tasks:
                self.metrics.failures += 1
                self.metrics.human_interventions += 1
            recovery_mean = float(self.config["robots"]["failure_recovery_minutes_mean"])
            recovery = self.rng.expovariate(1.0 / recovery_mean)
            coordination += recovery
            duration += recovery
        duration = yield from self._execute_transport(lead, duration)
        if robot is not None:
            robot["battery_kwh"] = max(0.0, robot["battery_kwh"] - energy)
        measured = [task for task in tasks if task.created_at >= self.measurement_start]
        if measured:
            self.metrics.robot_batches += 1
            self.metrics.batched_requests += len(measured)
            self.metrics.robot_completed += len(measured)
            self.metrics.robot_busy_minutes += duration
            self.metrics.gross_manual_minutes_avoided += sum(
                manual_counterfactual
                for task, manual_counterfactual in zip(
                    tasks, manual_counterfactuals, strict=True
                )
                if task.created_at >= self.measurement_start
            )
            self.metrics.robot_human_burden_minutes += coordination
            self.metrics.robot_energy_kwh += energy
        for task, completed in assignments:
            self._complete(task, "robot", started, duration)
            completed.succeed()
        yield from self._decontaminate_if_needed(tasks)

    def _serve_robot_batch(
        self, assignments: list[tuple[DeliveryTask, simpy.Event]]
    ):
        if self.battery_enabled:
            robot = yield self.robot_units.get()
            yield from self._serve_robot_batch_acquired(assignments, robot)
            robots = self.config["robots"]
            trigger = float(robots["battery_capacity_kwh"]) * float(
                robots["charge_trigger_fraction"]
            )
            if robot["battery_kwh"] <= trigger:
                self.env.process(self._charge_and_return(robot))
            else:
                yield self.robot_units.put(robot)
            return
        lead = assignments[0][0]
        priority = (lead.priority, lead.due_at, lead.task_id)
        with self.robots.request(priority=priority) as request:
            yield request
            yield from self._serve_robot_batch_acquired(assignments)

    def _batch_dispatcher(self):
        robots = self.config["robots"]
        maximum = int(robots.get("batch_max_size", 2))
        window = float(robots.get("batch_window_minutes", 5.0))
        batch_urgent = bool(robots.get("batch_urgent", False))
        while True:
            first = yield self.robot_batch_queue.get()
            lead = first[0]
            wait = 0.0 if lead.priority == 1 and not batch_urgent else window
            if wait:
                yield self.env.timeout(wait)
            assignments = [first]
            for candidate in list(self.robot_batch_queue.items):
                task = candidate[0]
                if len(assignments) >= maximum:
                    break
                if (
                    task.origin == lead.origin
                    and task.destination == lead.destination
                    and (batch_urgent or task.priority != 1)
                ):
                    self.robot_batch_queue.items.remove(candidate)
                    assignments.append(candidate)
            self.env.process(self._serve_robot_batch(assignments))

    def _serve_robot_batched(self, task: DeliveryTask):
        completed = self.env.event()
        yield self.robot_batch_queue.put((task, completed))
        yield completed

    def _task_process(self, task: DeliveryTask):
        if self._robot_eligible(task):
            if self.config.get("scenario", {}).get("policy") == "batch_overflow":
                yield from self._serve_robot_batched(task)
            else:
                yield from self._serve_robot(task)
        else:
            yield from self._serve_human(task)

    def _passenger_elevator_trip(self, measured: bool):
        with self.elevators.request() as request:
            yield request
            if measured:
                self.metrics.passenger_elevator_requests += 1
            service_minutes = float(
                self.config["hospital"].get(
                    "passenger_elevator_service_minutes",
                    self.config["hospital"]["elevator_cycle_minutes"],
                )
            )
            yield self.env.timeout(service_minutes)

    def _generate_passenger_elevator_load(self, end_minutes: float):
        rate = float(
            self.config["hospital"].get(
                "passenger_elevator_arrivals_per_hour", 0.0
            )
        )
        if rate <= 0:
            return
        while self.env.now < end_minutes:
            yield self.env.timeout(self.passenger_rng.expovariate(rate / 60.0))
            if self.env.now >= end_minutes:
                break
            self.env.process(
                self._passenger_elevator_trip(
                    self.measurement_start <= self.env.now < self.measurement_end
                )
            )

    def _generate_tasks(self, end_minutes: float):
        model = self.config["model"]
        scenario = self.config.get("scenario", {})
        base_rate = float(
            self.parameters.get("observed_requests_per_hour", model["base_requests_per_hour"])
        )
        demand_multiplier = float(scenario.get("demand_multiplier", 1.0))
        hour_weights = list(self.parameters["arrival_hour_weights"])
        while self.env.now < end_minutes:
            delay = next_interarrival_minutes(
                self.env.now,
                base_rate,
                demand_multiplier,
                hour_weights,
                self.demand_rng,
            )
            yield self.env.timeout(delay)
            if self.env.now >= end_minutes:
                break
            self.task_counter += 1
            task = build_task(
                self.task_counter,
                self.env.now,
                self.config,
                self.parameters,
                self.demand_rng,
            )
            if task.created_at >= self.measurement_start:
                self.metrics.created += 1
                self.measurement_task_ids.add(task.task_id)
                if task.priority == 1:
                    self.metrics.urgent_created += 1
            self.env.process(self._task_process(task))

    def _create_observed_task(
        self, created_at: float, origin: str, destination: str
    ) -> None:
        self.task_counter += 1
        task = build_task(
            self.task_counter,
            created_at,
            self.config,
            self.parameters,
            self.demand_rng,
            observed_pair=(str(origin), str(destination)),
        )
        if task.created_at >= self.measurement_start:
            self.metrics.created += 1
            self.measurement_task_ids.add(task.task_id)
            if task.priority == 1:
                self.metrics.urgent_created += 1
        self.env.process(self._task_process(task))

    def _generate_replayed_tasks(self, end_minutes: float):
        profiles = self.parameters["empirical_daily_requests"]
        multiplier = float(
            self.config.get("scenario", {}).get("demand_multiplier", 1.0)
        )
        day_count = int(end_minutes // (24 * 60))
        for day in range(day_count):
            profile = self.demand_rng.choice(profiles)
            scheduled: list[tuple[float, str, str]] = []
            whole = int(multiplier)
            fraction = multiplier - whole
            for minute, origin, destination in profile["requests"]:
                copies = whole + int(self.demand_rng.random() < fraction)
                for copy in range(copies):
                    scheduled.append(
                        (float(minute) + copy * 0.001, str(origin), str(destination))
                    )
            scheduled.sort()
            for minute, origin, destination in scheduled:
                event_time = day * 24 * 60 + minute
                if event_time >= end_minutes:
                    break
                if event_time > self.env.now:
                    yield self.env.timeout(event_time - self.env.now)
                self._create_observed_task(self.env.now, origin, destination)
            day_end = (day + 1) * 24 * 60
            if self.env.now < day_end:
                yield self.env.timeout(day_end - self.env.now)

    def run(self) -> dict[str, Any]:
        warmup_days = float(self.config["model"]["warmup_days"])
        simulation_days = float(self.config["model"]["simulation_days"])
        total_minutes = (warmup_days + simulation_days) * 24.0 * 60.0
        self._start_human_shifts(total_minutes)
        if float(
            self.config["hospital"].get(
                "passenger_elevator_arrivals_per_hour", 0.0
            )
        ) > 0:
            self.env.process(self._generate_passenger_elevator_load(total_minutes))
        if self.config.get("scenario", {}).get("policy") == "batch_overflow":
            self.env.process(self._batch_dispatcher())
        if self.parameters.get("empirical_daily_requests"):
            self.env.process(self._generate_replayed_tasks(total_minutes))
        else:
            self.env.process(self._generate_tasks(total_minutes))
        self.env.run(until=total_minutes + 24.0 * 60.0)
        net_minutes = (
            self.metrics.gross_manual_minutes_avoided
            - self.metrics.robot_human_burden_minutes
        )
        idle_minutes, contiguous_idle_minutes = self._idle_capacity()
        completed = max(1, self.metrics.completed)
        created = max(1, self.metrics.created)
        urgent = max(1, self.metrics.urgent_created)
        incomplete = max(0, self.metrics.created - self.metrics.completed)
        urgent_incomplete = max(
            0, self.metrics.urgent_created - self.metrics.urgent_completed
        )
        staffing = self.config["hospital"].get("human_staffing", [])
        staffed_hours_per_day = sum(
            float(shift["count"])
            * ((float(shift["end_hour"]) - float(shift["start_hour"])) % 24 or 24)
            for shift in staffing
        )
        result = {
            **asdict(self.metrics),
            "seed": self.seed,
            "scenario_hash": _stable_hash(self.config),
            "policy": self.config.get("scenario", {}).get("policy", "robot_first"),
            "fleet_size": int(self.config["robots"]["fleet_size"]),
            "demand_multiplier": float(
                self.config.get("scenario", {}).get("demand_multiplier", 1.0)
            ),
            "reliability": float(self.config["robots"]["reliability"]),
            "incomplete": incomplete,
            "urgent_incomplete": urgent_incomplete,
            "late_fraction": self.metrics.late / completed,
            "late_or_incomplete_fraction": (self.metrics.late + incomplete) / created,
            "urgent_late_fraction": (
                self.metrics.urgent_late + urgent_incomplete
            ) / urgent,
            "mean_wait_minutes": self.metrics.total_wait_minutes / completed,
            "mean_cycle_minutes": self.metrics.total_cycle_minutes / completed,
            "net_released_minutes": net_minutes,
            "human_idle_minutes": idle_minutes,
            "human_contiguous_idle_minutes": contiguous_idle_minutes,
            "human_contiguous_idle_minutes_per_100_beds_day": contiguous_idle_minutes
            / simulation_days
            / (float(self.config["model"]["beds"]) / 100.0),
            "human_support_minutes_per_100_beds_day": self.metrics.human_support_minutes
            / simulation_days
            / (float(self.config["model"]["beds"]) / 100.0),
            "human_empty_travel_minutes_per_100_beds_day": self.metrics.human_empty_travel_minutes
            / simulation_days
            / (float(self.config["model"]["beds"]) / 100.0),
            "robot_empty_travel_minutes_per_100_beds_day": self.metrics.robot_empty_travel_minutes
            / simulation_days
            / (float(self.config["model"]["beds"]) / 100.0),
            "mean_human_support_wait_minutes": self.metrics.human_support_wait_minutes
            / max(1, self.metrics.human_support_tasks),
            "staffed_hours_per_day": staffed_hours_per_day,
            "human_utilization": 1.0 - idle_minutes
            / max(
                1.0,
                sum(end - start for _, start, end in self.staffed_intervals),
            ),
            "mean_elevator_wait_minutes": self.metrics.elevator_wait_minutes
            / max(1, self.metrics.elevator_requests),
            "mean_charging_wait_minutes": self.metrics.charging_wait_minutes
            / max(1, self.metrics.charging_sessions),
            "charging_utilization": self.metrics.charging_minutes
            / max(
                1.0,
                float(self.config["robots"].get("charging_station_count", 1))
                * simulation_days
                * 24.0
                * 60.0,
            ),
            "mean_robot_batch_size": self.metrics.batched_requests
            / max(1, self.metrics.robot_batches),
            "mean_decontamination_wait_minutes": self.metrics.decontamination_wait_minutes
            / max(1, self.metrics.decontamination_sessions),
            "decontamination_utilization": self.metrics.decontamination_minutes
            / max(
                1.0,
                float(
                    self.config["robots"].get("decontamination_station_count", 1)
                )
                * simulation_days
                * 24.0
                * 60.0,
            ),
        }
        for shift in staffing:
            result[f"staff_{shift['name']}"] = int(shift["count"])
        return result


def run_simulation(
    config: dict[str, Any],
    parameters: dict[str, Any] | None = None,
    *,
    seed: int,
) -> dict[str, Any]:
    return HospitalSimulation(config, parameters, seed=seed).run()
