"""A0 — the depot commitment: N vehicles, M chargers, one site power cap, hard deadlines.

This is the centrepiece algorithm. Everything else in the platform exists to feed it or to act on
its output.

The problem is NP-hard (it contains bin-packing), and both naive approaches fail in ways that are
easy to demonstrate and are covered by tests:

  * **Greedy by deadline** fills the cheap window with whoever departs first, then discovers the
    remaining vehicles must buy their whole requirement at peak -- or cannot finish at all.
  * **N independent per-vehicle DPs** is optimal per vehicle and *infeasible in aggregate*: every
    vehicle independently chooses the same cheapest hour, the depot draws N x max_kW at 02:00, and
    the main breaker trips. This is the real-world failure that makes depot charging hard, and it
    is exactly what running A2 alone would produce.

We use **Lagrangian relaxation**, which is the right tool when the coupling between otherwise
independent subproblems is a single shared resource:

  1. Dualise the site cap into a time-varying price lambda(t) -- literally a congestion charge on
     depot power.
  2. Each vehicle independently solves its own DP against (tariff + lambda). Vehicles that can
     wait are priced out of the contended hour and move themselves; vehicles on a tight deadline
     pay the congestion charge and stay.
  3. A subgradient step raises lambda(t) where the cap is still exceeded and lowers it where slack
     remains. Repeat.
  4. Repair any residual violation greedily, shedding load from the vehicles with the most
     departure-deadline slack first.

The reason this is defensible rather than merely clever: the relaxed solution is a **lower bound**
on the true optimum at every iteration, so we can report the **duality gap** and state how close
to optimal the committed plan provably is. We do not have to assert that it is good.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence



@dataclass(frozen=True, slots=True)
class VehicleDemand:
    """One vehicle needing charge before a deadline."""

    vin: str
    soc_start_pct: float
    soc_target_pct: float
    battery_kwh: float
    max_charge_kw: float
    # Index of the last slot in which this vehicle may still draw power. Departure deadline.
    last_slot: int
    # True when this vehicle is in the randomised control arm: it charges unmanaged (full power
    # on arrival) and is excluded from the optimisation entirely. See docs/ARCHITECTURE.md Sec 2.
    is_control: bool = False

    @property
    def required_kwh(self) -> float:
        return max(0.0, (self.soc_target_pct - self.soc_start_pct) / 100.0 * self.battery_kwh)


@dataclass(frozen=True, slots=True)
class VehiclePlan:
    vin: str
    power_by_slot: tuple[float, ...]
    cost: float
    final_soc_pct: float
    feasible: bool
    is_control: bool = False


@dataclass(slots=True)
class DepotPlan:
    """The committed nightly plan for one depot."""

    plans: tuple[VehiclePlan, ...]
    site_load_by_slot: tuple[float, ...]
    total_cost: float
    peak_kw: float
    cap_kw: float
    # Lower bound from the Lagrangian dual. The true optimum lies in [lower_bound, total_cost].
    lower_bound: float
    iterations: int
    infeasible_vins: tuple[str, ...] = field(default_factory=tuple)

    @property
    def cap_respected(self) -> bool:
        # Tolerance absorbs float accumulation across hundreds of vehicles.
        return self.peak_kw <= self.cap_kw + 1e-6

    @property
    def duality_gap(self) -> float | None:
        """Relative gap to the proven lower bound. 0.03 means 'within 3% of optimal'.

        **None when the plan is infeasible.** The bound is derived from a relaxation in which
        every vehicle reaches its target; if we had to shed load to respect the cap, the
        committed plan solves a strictly easier problem and the two are not comparable. Returning
        a number here would understate the gap and read as near-optimal when we in fact failed to
        serve the depot at all -- the most dangerous possible lie for this object to tell.
        """
        if self.infeasible_vins or self.lower_bound <= 0:
            return None
        return max(0.0, (self.total_cost - self.lower_bound) / self.lower_bound)


def _solve_one(
    v: VehicleDemand,
    effective_tariff: Sequence[float],
    slot_minutes: int,
    soc_buckets: int,          # retained for signature stability; unused, see below
    charge_efficiency: float,
) -> VehiclePlan:
    """Solve one vehicle exactly against a tariff that already includes the congestion price.

    **Price-sorted greedy is provably optimal here**, so we do not discretise.

    The single-vehicle subproblem is: buy E kWh across slots, at most `max_charge_kw * slot_hours`
    per slot, none after the deadline, minimising cost. There is no coupling between slots beyond
    the total-energy requirement and the per-slot rate cap, which makes it a continuous knapsack:
    filling the cheapest slots first is exactly optimal by an exchange argument.

    This replaced a call to the A2 bucket DP, and the reason matters. The DP discretises SoC into
    51 buckets, so its optimum is the best point *on that grid* -- strictly costlier than the
    continuous optimum. Summing those inflated per-vehicle costs produced a Lagrangian "lower
    bound" that the continuous repair phase could legitimately beat, i.e. not a bound at all.
    Solving the subproblem exactly removes the discretisation error at its source, and is also
    O(T log T) instead of O(T*S*R).

    A2 remains the right tool for the single-vehicle case with SoC floors and bucket semantics;
    it is simply not what this relaxation needs.
    """
    T = len(effective_tariff)
    slot_hours = slot_minutes / 60.0
    horizon = min(v.last_slot + 1, T)
    grid_kwh = v.required_kwh / charge_efficiency
    per_slot_kwh = v.max_charge_kw * slot_hours

    power = [0.0] * T
    cost = 0.0
    remaining = grid_kwh
    for t in sorted(range(horizon), key=lambda s: effective_tariff[s]):
        if remaining <= 1e-12:
            break
        take = min(per_slot_kwh, remaining)
        power[t] = take / slot_hours
        cost += take * effective_tariff[t]
        remaining -= take

    stored = (grid_kwh - remaining) * charge_efficiency
    final_soc = min(100.0, v.soc_start_pct + stored / v.battery_kwh * 100.0)
    return VehiclePlan(
        vin=v.vin,
        power_by_slot=tuple(power),
        cost=round(cost, 6),
        final_soc_pct=round(final_soc, 2),
        feasible=remaining <= 1e-6,
        is_control=v.is_control,
    )


def _true_cost(plan: VehiclePlan, tariff: Sequence[float], slot_hours: float) -> float:
    """Cost at the real tariff, excluding the artificial congestion price."""
    return sum(p * slot_hours * tariff[t] for t, p in enumerate(plan.power_by_slot))


def unmanaged_plan(
    v: VehicleDemand,
    tariff: Sequence[float],
    slot_minutes: int,
    charge_efficiency: float = 0.92,
) -> VehiclePlan:
    """What the control arm does: plug in on arrival, draw full power until target.

    This is the counterfactual the randomised holdout measures against. It must be implemented
    honestly -- making the baseline artificially bad would inflate the measured saving, which is
    the most tempting way to cheat in this entire system.
    """
    slot_hours = slot_minutes / 60.0
    grid_kwh = v.required_kwh / charge_efficiency
    power: list[float] = [0.0] * len(tariff)
    cost = 0.0
    soc = v.soc_start_pct
    for t in range(min(v.last_slot + 1, len(tariff))):
        if grid_kwh <= 1e-9:
            break
        take = min(v.max_charge_kw * slot_hours, grid_kwh)
        power[t] = take / slot_hours
        cost += take * tariff[t]
        soc += (take * charge_efficiency) / v.battery_kwh * 100.0
        grid_kwh -= take
    return VehiclePlan(
        vin=v.vin,
        power_by_slot=tuple(power),
        cost=round(cost, 4),
        final_soc_pct=round(min(100.0, soc), 2),
        feasible=grid_kwh <= 1e-6,
        is_control=v.is_control,
    )


def plan_depot(
    vehicles: Sequence[VehicleDemand],
    tariff_per_slot: Sequence[float],
    site_cap_kw: float,
    *,
    slot_minutes: int = 30,
    max_iterations: int = 24,
    soc_buckets: int = 51,
    charge_efficiency: float = 0.92,
    step_scale: float = 0.6,
) -> DepotPlan:
    """Commit tonight's depot plan.

    Control-arm vehicles are scheduled unmanaged and their load is reserved against the cap before
    optimisation begins -- they would physically draw that power, so pretending otherwise would
    produce a plan that trips the breaker in reality.

    Time O(K * N * T*S*R). Space O(N*T).
    """
    T = len(tariff_per_slot)
    if T == 0 or not vehicles:
        return DepotPlan((), (), 0.0, 0.0, site_cap_kw, 0.0, 0)

    slot_hours = slot_minutes / 60.0
    treatment = [v for v in vehicles if not v.is_control]
    control = [v for v in vehicles if v.is_control]

    # Control arm charges unmanaged. Its load is a fixed obstacle the optimiser must work around.
    control_plans = [unmanaged_plan(v, tariff_per_slot, slot_minutes, charge_efficiency)
                     for v in control]
    reserved = [0.0] * T
    for cp in control_plans:
        for t, p in enumerate(cp.power_by_slot):
            reserved[t] += p

    lam = [0.0] * T
    best: list[VehiclePlan] = []
    best_cost = math.inf
    best_load: list[float] = [0.0] * T
    best_lower = 0.0
    iterations = 0

    # A reference scale for the subgradient step: the span of real prices. Without this the step
    # size is unit-dependent and the dual oscillates instead of converging.
    price_scale = max(tariff_per_slot) - min(tariff_per_slot) or max(tariff_per_slot) or 1.0

    for it in range(1, max_iterations + 1):
        iterations = it
        effective = [tariff_per_slot[t] + lam[t] for t in range(T)]
        plans = [_solve_one(v, effective, slot_minutes, soc_buckets, charge_efficiency)
                 for v in treatment]

        load = list(reserved)
        for p in plans:
            for t, kw in enumerate(p.power_by_slot):
                load[t] += kw

        # Lagrangian dual value: relaxed objective minus the price of the capacity we granted.
        #   L(lam) = min_x [ c(x) + sum_t lam_t * (load_t(x) - cap) ]
        # Treatment plan costs are already evaluated at (tariff + lam), so they carry their own
        # lam * load term. Control vehicles are fixed, so their cost is at the TRUE tariff and
        # their lam-priced load has to be added explicitly -- omitting it inflated the bound
        # above the achievable cost whenever a control arm was present.
        relaxed = (
            sum(p.cost for p in plans)
            + sum(cp.cost for cp in control_plans)
            + sum(lam[t] * reserved[t] * slot_hours for t in range(T))
        )
        dual = relaxed - sum(lam[t] * site_cap_kw * slot_hours for t in range(T))
        best_lower = max(best_lower, dual)

        true_cost = (sum(_true_cost(p, tariff_per_slot, slot_hours) for p in plans)
                     + sum(cp.cost for cp in control_plans))
        peak = max(load)

        # Keep the cheapest plan that actually respects the cap.
        if peak <= site_cap_kw + 1e-6 and true_cost < best_cost:
            best_cost, best, best_load = true_cost, plans, load

        violation = [load[t] - site_cap_kw for t in range(T)]
        if max(violation) <= 1e-6 and best:
            break

        # Subgradient step. 1/it decay gives the standard diminishing-step convergence; without
        # the decay lambda overshoots and the schedule oscillates between two bad extremes.
        step = step_scale * price_scale / (it * max(site_cap_kw, 1.0))
        for t in range(T):
            lam[t] = max(0.0, lam[t] + step * violation[t])

    if not best:
        # The dual never produced a feasible plan within the iteration budget. Fall back to the
        # last iterate and repair it deterministically.
        best, best_load = plans, load

    best, best_load, infeasible = _repair(
        best, best_load, reserved, treatment, site_cap_kw, slot_hours, T,
        tariff=tariff_per_slot, charge_efficiency=charge_efficiency,
    )
    all_plans = tuple(best) + tuple(control_plans)
    total = sum(_true_cost(p, tariff_per_slot, slot_hours) for p in all_plans)

    return DepotPlan(
        plans=all_plans,
        site_load_by_slot=tuple(round(x, 4) for x in best_load),
        total_cost=round(total, 4),
        peak_kw=round(max(best_load), 4) if best_load else 0.0,
        cap_kw=site_cap_kw,
        lower_bound=round(best_lower, 4),
        iterations=iterations,
        infeasible_vins=infeasible,
    )


def _repair(
    plans: Sequence[VehiclePlan],
    load: Sequence[float],
    reserved: Sequence[float],
    demands: Sequence[VehicleDemand],
    cap_kw: float,
    slot_hours: float,
    T: int,
    tariff: Sequence[float] | None = None,
    charge_efficiency: float = 0.92,
) -> tuple[list[VehiclePlan], list[float], tuple[str, ...]]:
    """Make the plan cap-feasible, then reclaim whatever we had to shed.

    The dual converges to the *relaxed* optimum, which need not be feasible, so a residual
    violation has to be repaired deterministically rather than by iterating forever.

    Two phases, and the second one matters as much as the first:

      1. **Shed proportionally.** Each vehicle drawing in an over-cap slot gives up the same
         *fraction* of its draw. An earlier version shed vehicle-by-vehicle in slack order, which
         zeroed the first few vehicles completely while leaving others untouched -- it reported
         four vehicles stranded on an instance with 200 kWh of capacity and only 152 kWh of
         demand. Starving a few vehicles is far worse than slightly delaying many.
      2. **Re-place the shed energy.** Shed kWh is offered back into the cheapest slots that
         still have headroom before each vehicle's deadline. Without this the algorithm throws
         away energy it had room for, which is what made a feasible depot look infeasible.

    Vehicles still short after both phases are reported, never hidden: a vehicle that silently
    misses target is a route that fails tomorrow morning.
    """
    power = [list(p.power_by_slot) for p in plans]
    cur = list(load)
    demand_by_vin = {d.vin: d for d in demands}

    # ---- phase 1: proportional shedding
    for t in range(T):
        excess = cur[t] - cap_kw
        if excess <= 1e-9:
            continue
        drawing = [i for i in range(len(power)) if power[i][t] > 0]
        total_draw = sum(power[i][t] for i in drawing)
        if total_draw <= 1e-12:
            continue
        # Cannot shed below the reserved (control-arm) floor, so scale only the flexible part.
        scale = max(0.0, (total_draw - excess) / total_draw)
        for i in drawing:
            removed = power[i][t] * (1.0 - scale)
            power[i][t] -= removed
            cur[t] -= removed

    # ---- phase 2: re-place shed energy into slots that still have headroom
    prices = list(tariff) if tariff is not None else [1.0] * T
    for i, p in enumerate(plans):
        d = demand_by_vin.get(p.vin)
        if d is None or d.is_control:
            continue
        target_grid_kwh = d.required_kwh / charge_efficiency
        have = sum(kw * slot_hours for kw in power[i])
        deficit = target_grid_kwh - have
        if deficit <= 1e-9:
            continue
        # Cheapest slots first, bounded by the vehicle's deadline and its own max charge rate.
        for t in sorted(range(min(d.last_slot + 1, T)), key=lambda s: prices[s]):
            if deficit <= 1e-9:
                break
            headroom_site = cap_kw - cur[t]
            headroom_veh = d.max_charge_kw - power[i][t]
            add_kw = min(headroom_site, headroom_veh, deficit / slot_hours)
            if add_kw <= 1e-9:
                continue
            power[i][t] += add_kw
            cur[t] += add_kw
            deficit -= add_kw * slot_hours

    repaired: list[VehiclePlan] = []
    short: list[str] = []
    for i, p in enumerate(plans):
        d = demand_by_vin.get(p.vin)
        delivered_kwh = sum(kw * slot_hours for kw in power[i])
        if d is not None:
            stored = delivered_kwh * 0.92
            final_soc = min(100.0, d.soc_start_pct + stored / d.battery_kwh * 100.0)
            ok = final_soc >= d.soc_target_pct - 0.5
            if not ok:
                short.append(p.vin)
        else:
            final_soc, ok = p.final_soc_pct, p.feasible
        repaired.append(
            VehiclePlan(
                vin=p.vin,
                power_by_slot=tuple(round(x, 4) for x in power[i]),
                cost=p.cost,
                final_soc_pct=round(final_soc, 2),
                feasible=ok,
                is_control=p.is_control,
            )
        )
    return repaired, cur, tuple(short)


def brute_force_depot_cost(
    vehicles: Sequence[VehicleDemand],
    tariff_per_slot: Sequence[float],
    site_cap_kw: float,
    *,
    slot_minutes: int = 30,
    power_levels: int = 3,
) -> float | None:
    """Exhaustive search over discretised power levels. Tests only -- exponential.

    Exists so the Lagrangian solution can be checked against a provable optimum on small
    instances. A claim that a heuristic is "near-optimal" means nothing without this.
    """
    T = len(tariff_per_slot)
    n = len(vehicles)
    if n * T > 12:
        return None  # refuse to run an intractable search

    slot_hours = slot_minutes / 60.0
    levels = [[v.max_charge_kw * k / (power_levels - 1) for k in range(power_levels)]
              for v in vehicles]
    best = math.inf

    def recurse(i: int, t: int, load: list[float], soc: list[float], cost: float) -> None:
        nonlocal best
        if cost >= best:
            return
        if t == T:
            if all(soc[j] >= vehicles[j].soc_target_pct - 1e-6 for j in range(n)):
                best = cost
            return
        if i == n:
            recurse(0, t + 1, load, soc, cost)
            return
        for kw in levels[i]:
            if t > vehicles[i].last_slot and kw > 0:
                continue
            if load[t] + kw > site_cap_kw + 1e-9:
                continue
            load[t] += kw
            stored = kw * slot_hours * 0.92
            soc[i] += stored / vehicles[i].battery_kwh * 100.0
            recurse(i + 1, t, load, soc, cost + kw * slot_hours * tariff_per_slot[t])
            soc[i] -= stored / vehicles[i].battery_kwh * 100.0
            load[t] -= kw

    recurse(0, 0, [0.0] * T, [v.soc_start_pct for v in vehicles], 0.0)
    return None if best == math.inf else round(best, 4)
