from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeliveryTask:
    task_id: int
    created_at: float
    task_type: str
    origin: str
    destination: str
    priority: int
    deadline_minutes: float
    loading_minutes: float
    receiving_minutes: float

    @property
    def due_at(self) -> float:
        return self.created_at + self.deadline_minutes


@dataclass(slots=True)
class HumanAssignment:
    task: DeliveryTask
    completed: object


@dataclass(slots=True)
class SupportAssignment:
    task: DeliveryTask
    duration_minutes: float
    support_kind: str
    queued_at: float
    completed: object


@dataclass(slots=True)
class MetricAccumulator:
    created: int = 0
    completed: int = 0
    late: int = 0
    urgent_created: int = 0
    urgent_completed: int = 0
    urgent_late: int = 0
    robot_completed: int = 0
    human_completed: int = 0
    failures: int = 0
    human_interventions: int = 0
    gross_manual_minutes_avoided: float = 0.0
    robot_human_burden_minutes: float = 0.0
    human_transport_minutes: float = 0.0
    total_wait_minutes: float = 0.0
    total_cycle_minutes: float = 0.0
    robot_busy_minutes: float = 0.0
    elevator_requests: int = 0
    elevator_wait_minutes: float = 0.0
    charging_sessions: int = 0
    charging_wait_minutes: float = 0.0
    charging_minutes: float = 0.0
    robot_energy_kwh: float = 0.0
    robot_batches: int = 0
    batched_requests: int = 0
    decontamination_sessions: int = 0
    decontamination_wait_minutes: float = 0.0
    decontamination_minutes: float = 0.0
    human_support_tasks: int = 0
    human_support_minutes: float = 0.0
    human_support_travel_minutes: float = 0.0
    human_support_wait_minutes: float = 0.0
    human_empty_travel_minutes: float = 0.0
    robot_empty_travel_minutes: float = 0.0
    battery_forced_charges: int = 0
    battery_infeasible_missions: int = 0
    passenger_elevator_requests: int = 0
