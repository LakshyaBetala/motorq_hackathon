"""A1 nearest reachable charger (A*), A2 cheapest charging schedule under ToU tariffs (DP).

These two are the brief's own worked examples, so they are implemented properly rather than
gestured at: A1 is a true A* with an admissible heuristic and energy-feasibility edge pruning,
and A2 is an exact DP over (time slot x SoC bucket), not a greedy "charge when cheap" heuristic.

The distinction matters. Greedy fails whenever the cheapest window is too short to reach the
required SoC by the deadline: it fills the cheap window, then discovers it must buy the shortfall
at the most expensive rate. The DP looks ahead and spreads the load. There is a test for exactly
that case.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Time O(1)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# ----------------------------------------------------------------- A1: A* reachable charger


@dataclass(frozen=True, slots=True)
class Node:
    node_id: int
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class Edge:
    to: int
    distance_km: float
    # Consumption varies by road class: motorway speeds cost more Wh/km than urban crawling,
    # so a flat km->kWh factor would route an almost-flat vehicle onto a motorway it cannot finish.
    kwh_per_km: float


@dataclass(frozen=True, slots=True)
class ChargerNode:
    charger_id: int
    node_id: int
    power_kw: float


@dataclass(frozen=True, slots=True)
class ReachableCharger:
    charger_id: int
    node_id: int
    distance_km: float
    energy_kwh: float
    soc_on_arrival_pct: float
    path: tuple[int, ...]


class RoadGraph:
    """Adjacency-list road graph. Nodes are junctions; edges carry distance and consumption."""

    __slots__ = ("nodes", "adjacency")

    def __init__(self, nodes: Iterable[Node], edges: Mapping[int, Sequence[Edge]]) -> None:
        self.nodes: dict[int, Node] = {n.node_id: n for n in nodes}
        self.adjacency: dict[int, tuple[Edge, ...]] = {
            k: tuple(v) for k, v in edges.items()
        }

    def neighbours(self, node_id: int) -> tuple[Edge, ...]:
        return self.adjacency.get(node_id, ())


def nearest_reachable_charger(
    graph: RoadGraph,
    start_node: int,
    chargers: Sequence[ChargerNode],
    usable_kwh: float,
    battery_kwh: float,
    *,
    reserve_pct: float = 10.0,
    best_kwh_per_km: float | None = None,
) -> ReachableCharger | None:
    """Cheapest-energy reachable charger from `start_node`, or None if the vehicle is stranded.

    This is A* over *energy*, not distance, because energy is the binding constraint — the
    nearest charger by road distance is useless if the route there is uphill motorway and the
    pack cannot cover it.

    Heuristic: straight-line distance to the closest charger multiplied by the most efficient
    consumption rate in the graph. That can never overestimate true remaining energy cost
    (straight line <= road distance, best rate <= actual rate), so the heuristic is **admissible**
    and A* returns the true optimum. It is also consistent, so no node is expanded twice.

    `reserve_pct` keeps a buffer so we never route a vehicle to arrive at exactly 0%.

    Time O(E log V). Space O(V).
    """
    if not chargers:
        return None

    charger_by_node: dict[int, ChargerNode] = {c.node_id: c for c in chargers}
    reserve_kwh = battery_kwh * (reserve_pct / 100.0)
    budget = usable_kwh - reserve_kwh
    if budget <= 0:
        return None

    if best_kwh_per_km is None:
        rates = [e.kwh_per_km for edges in graph.adjacency.values() for e in edges]
        best_kwh_per_km = min(rates) if rates else 0.0

    targets = [graph.nodes[c.node_id] for c in chargers if c.node_id in graph.nodes]
    if not targets:
        return None

    def heuristic(node_id: int) -> float:
        node = graph.nodes[node_id]
        nearest = min(
            haversine_km(node.lat, node.lon, t.lat, t.lon) for t in targets
        )
        return nearest * best_kwh_per_km

    # (f = g + h, g = energy used, node, distance so far, path)
    start_h = heuristic(start_node)
    frontier: list[tuple[float, float, int, float, tuple[int, ...]]] = [
        (start_h, 0.0, start_node, 0.0, (start_node,))
    ]
    best_energy: dict[int, float] = {start_node: 0.0}

    while frontier:
        _, energy, node_id, distance, path = heapq.heappop(frontier)

        # Stale entry: we already reached this node more cheaply.
        if energy > best_energy.get(node_id, math.inf):
            continue

        if node_id in charger_by_node:
            charger = charger_by_node[node_id]
            soc_on_arrival = max(0.0, (usable_kwh - energy) / battery_kwh * 100.0)
            return ReachableCharger(
                charger_id=charger.charger_id,
                node_id=node_id,
                distance_km=round(distance, 3),
                energy_kwh=round(energy, 4),
                soc_on_arrival_pct=round(soc_on_arrival, 2),
                path=path,
            )

        for edge in graph.neighbours(node_id):
            new_energy = energy + edge.distance_km * edge.kwh_per_km
            # Energy-feasibility pruning: an edge that would strand the vehicle is not an edge.
            if new_energy > budget:
                continue
            if new_energy >= best_energy.get(edge.to, math.inf):
                continue
            best_energy[edge.to] = new_energy
            heapq.heappush(
                frontier,
                (
                    new_energy + heuristic(edge.to),
                    new_energy,
                    edge.to,
                    distance + edge.distance_km,
                    path + (edge.to,),
                ),
            )

    return None


# ----------------------------------------------------------------- A2: DP charging schedule


@dataclass(frozen=True, slots=True)
class ChargePlan:
    total_cost: float
    final_soc_pct: float
    # kW to draw in each slot; 0 means idle.
    power_by_slot: tuple[float, ...]
    feasible: bool


def optimal_charging_schedule(
    *,
    soc_start_pct: float,
    soc_target_pct: float,
    battery_kwh: float,
    max_charge_kw: float,
    slot_minutes: int,
    tariff_per_slot: Sequence[float],
    soc_buckets: int = 101,
    charge_efficiency: float = 0.92,
    soc_floor_pct: float = 0.0,
) -> ChargePlan:
    """Cheapest way to reach `soc_target_pct` by the end of the horizon under a ToU tariff.

    Exact DP. State is (slot, SoC bucket); the transition is "how many kW to draw this slot",
    discretised to the bucket grid. We minimise cost and reconstruct the plan by backpointers.

    Deadline handling: the horizon *is* the deadline. Slots before departure are the only ones
    offered, so a plan that cannot reach the target is reported `feasible=False` with the best
    SoC achievable, rather than silently returning a plan that misses the departure.

    `charge_efficiency` matters: grid energy bought exceeds energy stored, so ignoring it
    understates cost by ~8% and biases the plan toward fast charging.

    Time O(T * S * R), where T slots, S SoC buckets, R reachable bucket-steps per slot.
    Space O(T * S) for backpointers.
    """
    if soc_buckets < 2:
        raise ValueError("soc_buckets must be at least 2")
    if not tariff_per_slot:
        return ChargePlan(0.0, soc_start_pct, (), soc_start_pct >= soc_target_pct)

    T = len(tariff_per_slot)
    S = soc_buckets
    pct_per_bucket = 100.0 / (S - 1)
    hours = slot_minutes / 60.0

    def to_bucket(pct: float) -> int:
        return min(S - 1, max(0, int(round(pct / pct_per_bucket))))

    start_b = to_bucket(soc_start_pct)
    target_b = to_bucket(soc_target_pct)
    floor_b = to_bucket(soc_floor_pct)

    # Energy stored by drawing max power for one slot, expressed in buckets.
    max_kwh_per_slot = max_charge_kw * hours * charge_efficiency
    max_step = int(math.floor(max_kwh_per_slot / battery_kwh * 100.0 / pct_per_bucket))
    max_step = max(0, min(max_step, S - 1))

    INF = math.inf
    cost = [[INF] * S for _ in range(T + 1)]
    back = [[-1] * S for _ in range(T + 1)]
    cost[0][start_b] = 0.0

    for t in range(T):
        price = tariff_per_slot[t]
        row = cost[t]
        nxt = cost[t + 1]
        for b in range(floor_b, S):
            c = row[b]
            if c == INF:
                continue
            for step in range(0, max_step + 1):
                nb = b + step
                if nb >= S:
                    break
                # Grid energy bought = stored energy / efficiency.
                stored_kwh = step * pct_per_bucket / 100.0 * battery_kwh
                grid_kwh = stored_kwh / charge_efficiency
                new_cost = c + grid_kwh * price
                if new_cost < nxt[nb]:
                    nxt[nb] = new_cost
                    back[t + 1][nb] = b

    # Prefer the cheapest state at or above target; if none is reachable, take the highest SoC.
    final_row = cost[T]
    candidates = [b for b in range(target_b, S) if final_row[b] < INF]
    feasible = bool(candidates)
    if feasible:
        end_b = min(candidates, key=lambda b: final_row[b])
    else:
        reachable = [b for b in range(S) if final_row[b] < INF]
        if not reachable:
            return ChargePlan(0.0, soc_start_pct, tuple([0.0] * T), False)
        end_b = max(reachable)

    # Reconstruct.
    power: list[float] = [0.0] * T
    b = end_b
    for t in range(T, 0, -1):
        pb = back[t][b]
        if pb < 0:
            break
        step = b - pb
        if step > 0:
            stored_kwh = step * pct_per_bucket / 100.0 * battery_kwh
            grid_kwh = stored_kwh / charge_efficiency
            power[t - 1] = grid_kwh / hours
        b = pb

    return ChargePlan(
        total_cost=round(final_row[end_b], 4),
        final_soc_pct=round(end_b * pct_per_bucket, 2),
        power_by_slot=tuple(round(p, 3) for p in power),
        feasible=feasible,
    )


def naive_charge_on_arrival_cost(
    *,
    soc_start_pct: float,
    soc_target_pct: float,
    battery_kwh: float,
    max_charge_kw: float,
    slot_minutes: int,
    tariff_per_slot: Sequence[float],
    charge_efficiency: float = 0.92,
) -> float:
    """What the fleet does today: plug in on arrival and draw full power until target.

    This is the baseline the savings claim is measured against. Without it the DP's output is a
    number with nothing to compare to, and the impact figure would be unfalsifiable.
    """
    needed_kwh = max(0.0, (soc_target_pct - soc_start_pct) / 100.0 * battery_kwh)
    grid_kwh = needed_kwh / charge_efficiency
    hours = slot_minutes / 60.0
    per_slot_kwh = max_charge_kw * hours
    total = 0.0
    for price in tariff_per_slot:
        if grid_kwh <= 0:
            break
        take = min(per_slot_kwh, grid_kwh)
        total += take * price
        grid_kwh -= take
    return round(total, 4)
