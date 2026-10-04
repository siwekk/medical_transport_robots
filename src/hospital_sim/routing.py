from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from random import Random
from typing import Any

import networkx as nx


@dataclass(frozen=True, slots=True)
class RouteEstimate:
    distance_m: float
    elevator_rides: int
    elevator_floors: int = 0

    @property
    def elevator_legs(self) -> int:
        """Compatibility name for the number of separate elevator rides."""
        return self.elevator_rides


class HospitalRouter:
    def __init__(
        self,
        hospital_config: dict[str, Any],
        empirical_routes_by_hour: dict[str, Any] | None = None,
        pathway_network: dict[str, Any] | None = None,
    ):
        self.graph = nx.Graph()
        self.nodes = hospital_config["nodes"]
        for name, attributes in self.nodes.items():
            self.graph.add_node(name, **attributes)
        for origin, destination, distance in hospital_config["edges"]:
            self.graph.add_edge(origin, destination, distance_m=float(distance))
        if not nx.is_connected(self.graph):
            raise ValueError("Hospital graph must be connected")
        self.empirical_routes: dict[tuple[str, str], RouteEstimate] = {}
        self.empirical_graph = nx.Graph()
        self.pathway_graph = nx.DiGraph()
        if pathway_network:
            self.pathway_graph.add_nodes_from(
                (str(node), {"level": int(level)})
                for node, level in pathway_network["nodes"]
            )
            self.pathway_graph.add_edges_from(
                (str(origin), str(destination),
                 {"distance_m": float(distance), "path_type": path_type})
                for origin, destination, distance, path_type in pathway_network["edges"]
            )
        self._route_cache: dict[tuple[str, str], RouteEstimate] = {}
        for hour_data in (empirical_routes_by_hour or {}).values():
            for entry in hour_data["entries"]:
                origin, destination, distance, elevator_rides = entry[:4]
                elevator_floors = entry[4] if len(entry) > 4 else elevator_rides
                origin = str(origin)
                destination = str(destination)
                estimate = RouteEstimate(
                    float(distance), int(elevator_rides), int(elevator_floors)
                )
                self.empirical_routes[(origin, destination)] = estimate
                previous = self.empirical_graph.get_edge_data(origin, destination)
                if previous is None or float(distance) < previous["distance_m"]:
                    self.empirical_graph.add_edge(
                        origin,
                        destination,
                        distance_m=float(distance),
                        elevator_rides=int(elevator_rides),
                        elevator_floors=int(elevator_floors),
                    )

    def estimate(self, origin: str, destination: str) -> RouteEstimate:
        key = (origin, destination)
        cached = self._route_cache.get(key)
        if cached is not None:
            return cached
        estimate = self._estimate_uncached(origin, destination)
        if len(self._route_cache) >= 50_000:
            self._route_cache.clear()
        self._route_cache[key] = estimate
        return estimate

    def _estimate_uncached(self, origin: str, destination: str) -> RouteEstimate:
        empirical = self.empirical_routes.get((origin, destination))
        if empirical is not None:
            return empirical
        if origin in self.pathway_graph and destination in self.pathway_graph:
            path = nx.shortest_path(
                self.pathway_graph, origin, destination, weight="distance_m"
            )
            distance = 0.0
            rides = 0
            floors = 0
            previous_elevator = False
            for left, right in pairwise(path):
                edge = self.pathway_graph[left][right]
                distance += float(edge["distance_m"])
                elevator = edge["path_type"] == "ELEVATOR"
                if elevator:
                    rides += int(not previous_elevator)
                    floors += max(
                        1,
                        abs(
                            self.pathway_graph.nodes[left]["level"]
                            - self.pathway_graph.nodes[right]["level"]
                        ),
                    )
                previous_elevator = elevator
            return RouteEstimate(distance, rides, floors)
        if (
            origin in self.empirical_graph
            and destination in self.empirical_graph
            and nx.has_path(self.empirical_graph, origin, destination)
        ):
            path = nx.shortest_path(
                self.empirical_graph, origin, destination, weight="distance_m"
            )
            edges = [
                self.empirical_graph[left][right] for left, right in pairwise(path)
            ]
            return RouteEstimate(
                sum(float(edge["distance_m"]) for edge in edges),
                sum(int(edge["elevator_rides"]) for edge in edges),
                sum(int(edge["elevator_floors"]) for edge in edges),
            )
        path = nx.shortest_path(self.graph, origin, destination, weight="distance_m")
        distance = nx.path_weight(self.graph, path, weight="distance_m")
        elevator_floors = sum(
            1
            for left, right in pairwise(path)
            if self.nodes[left]["floor"] != self.nodes[right]["floor"]
        )
        elevator_rides = int(elevator_floors > 0)
        return RouteEstimate(float(distance), elevator_rides, elevator_floors)

    def travel_minutes(
        self,
        origin: str,
        destination: str,
        *,
        speed_m_per_min: float,
        elevator_cycle_minutes: float,
        elevator_travel_per_floor_minutes: float = 0.0,
        congestion_sigma: float,
        rng: Random,
    ) -> float:
        route = self.estimate(origin, destination)
        base = route.distance_m / speed_m_per_min
        congestion_factor = max(0.65, rng.lognormvariate(-0.5 * congestion_sigma**2, congestion_sigma))
        return (
            base * congestion_factor
            + route.elevator_rides * elevator_cycle_minutes
            + route.elevator_floors * elevator_travel_per_floor_minutes
        )
