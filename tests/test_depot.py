"""Tests for A0, the depot commitment under a shared power cap.

The first two tests are the point of the whole algorithm: they encode the exact failures that
greedy scheduling and N-independent-DPs produce. If someone later "simplifies" A0 back into
per-vehicle optimisation, these break loudly.
"""

from __future__ import annotations

import pytest

from readyline.algorithms.depot import (
    DepotPlan,
    VehicleDemand,
    brute_force_depot_cost,
    plan_depot,
    unmanaged_plan,
)
from readyline.algorithms.energy import optimal_charging_schedule

SLOT = 60  # 1-hour slots keep the arithmetic checkable by hand


def _fleet(n: int, *, soc_start=20.0, target=80.0, batt=50.0, kw=20.0, last_slot=5,
           control_every: int | None = None) -> list[VehicleDemand]:
    return [
        VehicleDemand(
            vin=f"VIN{i:03d}",
            soc_start_pct=soc_start,
            soc_target_pct=target,
            battery_kwh=batt,
            max_charge_kw=kw,
            last_slot=last_slot,
            is_control=(control_every is not None and i % control_every == 0),
        )
        for i in range(n)
    ]


# --------------------------------------------------------------- the failures A0 exists to stop


def test_independent_dps_would_violate_the_cap_but_a0_does_not():
    """The real-world failure: every vehicle independently picks the same cheapest hour.

    Ten vehicles at 20 kW all choosing slot 3 draw 200 kW. The site is capped at 60 kW, so the
    naive plan trips the main breaker. A0 must spread them.
    """
    tariff = [12.0, 12.0, 12.0, 2.0, 12.0, 12.0]  # one obviously cheapest slot
    vehicles = _fleet(10, kw=20.0)

    # What N independent DPs would do.
    naive_load = [0.0] * len(tariff)
    for v in vehicles:
        p = optimal_charging_schedule(
            soc_start_pct=v.soc_start_pct, soc_target_pct=v.soc_target_pct,
            battery_kwh=v.battery_kwh, max_charge_kw=v.max_charge_kw,
            slot_minutes=SLOT, tariff_per_slot=tariff,
        )
        for t, kw in enumerate(p.power_by_slot):
            naive_load[t] += kw
    assert max(naive_load) > 60.0, "precondition: naive plan should overload the site"

    plan = plan_depot(vehicles, tariff, site_cap_kw=60.0, slot_minutes=SLOT)
    assert plan.cap_respected, f"peak {plan.peak_kw} exceeded cap {plan.cap_kw}"


def test_cap_is_respected_in_every_slot_not_just_on_average():
    """An average-power constraint is easy and useless; the breaker trips on instantaneous draw."""
    tariff = [9.0, 3.0, 3.0, 9.0, 9.0, 2.0]
    plan = plan_depot(_fleet(12, kw=15.0), tariff, site_cap_kw=70.0, slot_minutes=SLOT)
    for t, kw in enumerate(plan.site_load_by_slot):
        assert kw <= plan.cap_kw + 1e-6, f"slot {t} drew {kw} kW against a {plan.cap_kw} kW cap"


def test_deadline_is_respected_no_vehicle_charges_after_departure():
    tariff = [10.0] * 8
    vehicles = [
        VehicleDemand("EARLY", 30.0, 70.0, 50.0, 25.0, last_slot=2),
        VehicleDemand("LATE", 30.0, 70.0, 50.0, 25.0, last_slot=7),
    ]
    plan = plan_depot(vehicles, tariff, site_cap_kw=100.0, slot_minutes=SLOT)
    early = next(p for p in plan.plans if p.vin == "EARLY")
    assert all(kw == 0.0 for kw in early.power_by_slot[3:]), "charged after departure"


# --------------------------------------------------------------- optimality


def test_matches_brute_force_optimum_on_a_small_instance():
    """A 'near-optimal' claim means nothing without an exhaustive cross-check."""
    tariff = [8.0, 2.0, 6.0]
    vehicles = [
        VehicleDemand("A", 50.0, 80.0, 20.0, 10.0, last_slot=2),
        VehicleDemand("B", 50.0, 80.0, 20.0, 10.0, last_slot=2),
    ]
    optimum = brute_force_depot_cost(vehicles, tariff, site_cap_kw=10.0, slot_minutes=SLOT)
    assert optimum is not None, "brute force refused; shrink the instance"

    plan = plan_depot(vehicles, tariff, site_cap_kw=10.0, slot_minutes=SLOT)
    assert plan.cap_respected
    # Within 15% of the proven optimum on a tightly-contended instance.
    assert plan.total_cost <= optimum * 1.15, f"got {plan.total_cost}, optimum {optimum}"


def test_reports_a_duality_gap_rather_than_asserting_quality():
    """The lower bound is what makes the plan defensible under questioning.

    Instance is deliberately feasible: 8 vehicles need ~19 kWh of grid energy each (152 kWh),
    against 50 kW x 4 h = 200 kWh of site capacity.
    """
    tariff = [10.0, 3.0, 3.0, 10.0]
    plan = plan_depot(_fleet(8, target=55.0, kw=12.0), tariff, site_cap_kw=50.0, slot_minutes=SLOT)
    assert plan.cap_respected
    assert not plan.infeasible_vins, "precondition: this instance must be satisfiable"
    assert plan.lower_bound > 0.0
    assert plan.total_cost >= plan.lower_bound - 1e-6, "cost below a valid lower bound is a bug"
    assert plan.duality_gap is not None and plan.duality_gap >= 0.0


def test_duality_gap_is_undefined_when_the_plan_sheds_load():
    """Reporting a small gap on a plan that failed to serve the depot would be the worst lie
    this object could tell: it would read as near-optimal precisely when we failed."""
    tariff = [5.0] * 3
    vehicles = _fleet(20, soc_start=10.0, target=95.0, batt=60.0, kw=20.0, last_slot=2)
    plan = plan_depot(vehicles, tariff, site_cap_kw=30.0, slot_minutes=SLOT)
    assert plan.infeasible_vins
    assert plan.duality_gap is None


def test_uncontended_depot_costs_no_more_than_unconstrained_optimum():
    """With a cap so high it never binds, A0 must not be worse than per-vehicle DP."""
    tariff = [10.0, 2.0, 10.0, 10.0]
    v = VehicleDemand("SOLO", 20.0, 60.0, 50.0, 25.0, last_slot=3)
    solo = optimal_charging_schedule(
        soc_start_pct=20.0, soc_target_pct=60.0, battery_kwh=50.0, max_charge_kw=25.0,
        slot_minutes=SLOT, tariff_per_slot=tariff,
    )
    plan = plan_depot([v], tariff, site_cap_kw=10_000.0, slot_minutes=SLOT)
    assert plan.total_cost <= solo.total_cost * 1.05


def test_cheap_slots_are_preferred_when_capacity_allows():
    tariff = [20.0, 20.0, 1.0, 20.0]
    plan = plan_depot(_fleet(2, kw=25.0), tariff, site_cap_kw=60.0, slot_minutes=SLOT)
    load = plan.site_load_by_slot
    assert load[2] >= max(load[0], load[1], load[3]), "did not favour the cheap slot"


# --------------------------------------------------------------- the control arm


def test_control_vehicles_charge_unmanaged_on_arrival():
    """The holdout must reproduce today's behaviour: full power, immediately."""
    tariff = [20.0, 20.0, 1.0, 1.0]
    v = VehicleDemand("CTRL", 50.0, 70.0, 40.0, 10.0, last_slot=3, is_control=True)
    p = unmanaged_plan(v, tariff, SLOT)
    assert p.power_by_slot[0] > 0.0, "control arm must charge on arrival, not wait for cheap power"
    assert p.is_control


def test_control_load_is_reserved_against_the_cap():
    """Control vehicles physically draw power. Ignoring them would trip the breaker in reality."""
    tariff = [5.0] * 4
    vehicles = _fleet(6, kw=20.0, control_every=2)  # 3 control, 3 treatment
    plan = plan_depot(vehicles, tariff, site_cap_kw=70.0, slot_minutes=SLOT)
    assert plan.cap_respected
    assert any(p.is_control for p in plan.plans)
    assert any(not p.is_control for p in plan.plans)


def test_optimised_arm_beats_unmanaged_arm_on_cost():
    """The entire business case. If this fails there is nothing to sell."""
    tariff = [18.0, 18.0, 2.0, 2.0, 18.0, 18.0]
    v = VehicleDemand("V", 20.0, 80.0, 50.0, 15.0, last_slot=5)

    managed = plan_depot([v], tariff, site_cap_kw=1_000.0, slot_minutes=SLOT)
    control = unmanaged_plan(v, tariff, SLOT)

    assert managed.total_cost < control.cost, (
        f"managed {managed.total_cost} not cheaper than unmanaged {control.cost}"
    )


def test_unmanaged_baseline_is_not_artificially_crippled():
    """Guard against the most tempting way to cheat: making the baseline worse than reality.

    The control arm must still reach its target when there is time and power to do so. A baseline
    that fails to charge would inflate the measured saving.
    """
    tariff = [10.0] * 6
    v = VehicleDemand("V", 20.0, 80.0, 50.0, 20.0, last_slot=5)
    p = unmanaged_plan(v, tariff, SLOT)
    assert p.feasible
    assert p.final_soc_pct >= 79.5


# --------------------------------------------------------------- honesty about failure


def test_shortfalls_are_reported_not_hidden():
    """A vehicle that silently misses target is a route that fails tomorrow morning."""
    tariff = [5.0] * 3
    # Far more demand than the cap can possibly serve in 3 hours.
    vehicles = _fleet(20, soc_start=10.0, target=95.0, batt=60.0, kw=20.0, last_slot=2)
    plan = plan_depot(vehicles, tariff, site_cap_kw=30.0, slot_minutes=SLOT)
    assert plan.cap_respected, "must never break the cap, even when demand is unsatisfiable"
    assert plan.infeasible_vins, "under-served vehicles must be reported"
    assert any(not p.feasible for p in plan.plans)


def test_empty_depot_is_handled():
    plan = plan_depot([], [5.0, 5.0], site_cap_kw=100.0)
    assert plan.total_cost == 0.0
    assert plan.plans == ()


def test_vehicle_needing_no_charge_draws_nothing():
    tariff = [5.0] * 4
    v = VehicleDemand("FULL", 90.0, 80.0, 50.0, 20.0, last_slot=3)
    plan = plan_depot([v], tariff, site_cap_kw=100.0, slot_minutes=SLOT)
    assert plan.total_cost == pytest.approx(0.0, abs=1e-6)


def test_scales_to_a_realistic_depot():
    """120 vehicles, 60 chargers' worth of capacity, 24 half-hour slots overnight."""
    tariff = [9.0] * 6 + [3.0] * 8 + [6.0] * 10
    vehicles = _fleet(120, soc_start=35.0, target=85.0, batt=50.0, kw=11.0,
                      last_slot=23, control_every=10)
    plan = plan_depot(vehicles, tariff, site_cap_kw=400.0, slot_minutes=30)
    assert plan.cap_respected, f"peak {plan.peak_kw} over cap {plan.cap_kw}"
    assert len(plan.plans) == 120
    # Load should be pushed into the cheap block rather than the expensive opening hours.
    cheap = sum(plan.site_load_by_slot[6:14])
    pricey = sum(plan.site_load_by_slot[0:6])
    assert cheap > pricey
