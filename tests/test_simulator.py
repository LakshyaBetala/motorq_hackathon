"""Tests for the simulator and its ground-truth ledger.

The simulator is a first-class component, not a script. If it generates VINs our own validator
rejects, or faults nobody could ever detect, every downstream metric is wrong in a way that is
very hard to notice later.
"""

from __future__ import annotations

import random


from readyline.algorithms.identifiers import is_valid_vin
from readyline_sim.faults import (
    ANNUAL_RATE,
    COST_PLANNED,
    COST_UNPLANNED,
    LEAD_TIME_HOURS,
    Mechanism,
    schedule_faults,
)
from readyline_sim.fleet import (
    MODELS,
    generate_depots,
    generate_vehicles,
    make_vin,
    plan_tenants,
)

START_TS = 1_767_225_600.0  # 2026-01-01T00:00:00Z


# --------------------------------------------------------------------- VIN generation


def test_generated_vins_pass_our_own_validator():
    """If this fails, every integration test is silently exercising the reject path."""
    rng = random.Random(1)
    for _ in range(2_000):
        assert is_valid_vin(make_vin(rng))


def test_generated_vins_are_unique_enough_at_scale():
    rng = random.Random(2)
    vins = {make_vin(rng) for _ in range(20_000)}
    assert len(vins) > 19_990  # collisions are possible but must be vanishingly rare


# --------------------------------------------------------------------- tenancy shape


def test_tenant_plan_hits_the_exact_vehicle_target():
    tenants = plan_tenants(total_vehicles=100_000, tenant_count=60)
    assert sum(t.vehicle_count for t in tenants) == 100_000
    assert len(tenants) == 60


def test_tenant_distribution_has_a_long_tail_not_a_uniform_split():
    """A uniform split would hide the partition hot-spotting our VIN key exists to prevent."""
    tenants = plan_tenants(100_000, 60)
    counts = sorted((t.vehicle_count for t in tenants), reverse=True)
    assert counts[0] > 10_000                 # one dominant tenant
    assert counts[0] > counts[-1] * 20        # genuine long tail
    assert counts[-1] >= 20                   # nobody is empty


def test_tenant_plan_is_deterministic_under_seed():
    a = [t.vehicle_count for t in plan_tenants(100_000, 60, seed=42)]
    b = [t.vehicle_count for t in plan_tenants(100_000, 60, seed=42)]
    assert a == b


# --------------------------------------------------------------------- vehicle generation


def test_vehicles_are_generated_for_every_tenant():
    tenants = plan_tenants(2_000, 10)
    depots = generate_depots(tenants)
    vehicles = list(generate_vehicles(tenants, depots))
    assert len(vehicles) == 2_000
    assert {v.tenant_id for v in vehicles} == {t.tenant_id for t in tenants}


def test_every_vehicle_sits_in_a_depot_owned_by_its_own_tenant():
    """A cross-tenant depot reference would be a tenant-isolation bug seeded into the data."""
    tenants = plan_tenants(1_500, 8)
    depots = generate_depots(tenants)
    owner = {d.depot_id: d.tenant_id for d in depots}
    for v in generate_vehicles(tenants, depots):
        assert owner[v.depot_id] == v.tenant_id


def test_powertrain_fields_are_internally_consistent():
    tenants = plan_tenants(1_000, 5)
    depots = generate_depots(tenants)
    for v in generate_vehicles(tenants, depots):
        if v.model.powertrain == "BEV":
            assert v.model.battery_kwh is not None
            assert v.model.max_charge_kw is not None
        if v.model.powertrain == "ICE":
            assert v.model.tank_litres is not None


def test_soh_stays_in_a_plausible_range():
    tenants = plan_tenants(2_000, 10)
    depots = generate_depots(tenants)
    for v in generate_vehicles(tenants, depots):
        assert 60.0 <= v.soh_pct <= 100.0


def test_models_cover_three_distinct_oem_dialects():
    """The normalisation registry is only exercised if payloads genuinely differ."""
    assert len({m.dialect for m in MODELS}) == 3


# --------------------------------------------------------------------- the ledger


def _small_ledger(horizon_days: float = 365.0, seed: int = 5):
    tenants = plan_tenants(3_000, 12)
    depots = generate_depots(tenants)
    vehicles = generate_vehicles(tenants, depots)
    return schedule_faults(vehicles, horizon_days=horizon_days, start_ts=START_TS, seed=seed)


def test_fault_rate_matches_the_impact_model_assumption():
    """~1.8 events/vehicle-year is the number the whole impact estimate rests on."""
    ledger, _ = _small_ledger(horizon_days=365.0)
    per_vehicle_year = len(ledger.entries) / 3_000
    assert 1.3 < per_vehicle_year < 2.6, per_vehicle_year


def test_roughly_half_of_faults_are_telemetry_invisible():
    """Recall must be bounded by physics, not by our cleverness.

    A simulator where everything is predictable would let us claim a recall we could never
    reproduce on real vehicles.
    """
    ledger, _ = _small_ledger()
    fraction = ledger.summary()["visible_fraction"]
    assert 0.25 < fraction < 0.65, fraction


def test_visible_faults_have_a_detectable_lead_time():
    ledger, _ = _small_ledger()
    for e in ledger.entries:
        if e.visible:
            assert e.lead_time_hours > 0
            lo, hi = LEAD_TIME_HOURS[e.mechanism]
            assert lo <= e.lead_time_hours <= hi + 1e-6


def test_invisible_faults_have_no_precursor_at_all():
    ledger, _ = _small_ledger()
    for e in ledger.entries:
        if not e.visible:
            assert e.onset_ts == e.failure_ts


def test_avoidable_cost_is_never_negative():
    for m in Mechanism:
        assert COST_UNPLANNED[m] >= COST_PLANNED[m], m


def test_unpredictable_mechanisms_offer_no_saving():
    """A puncture cannot be planned away; claiming a saving on it would inflate the headline."""
    for m in (Mechanism.TYRE_PUNCTURE, Mechanism.COLLISION, Mechanism.FUEL_THEFT):
        assert COST_UNPLANNED[m] == COST_PLANNED[m]


def test_pack_faults_are_only_assigned_to_electric_vehicles():
    """A diesel van has no traction pack. Assigning one would create unlearnable noise."""
    tenants = plan_tenants(3_000, 12)
    depots = generate_depots(tenants)
    vehicles = list(generate_vehicles(tenants, depots))
    powertrain = {v.vin: v.model.powertrain for v in vehicles}

    ledger, _ = schedule_faults(iter(vehicles), horizon_days=365.0, start_ts=START_TS, seed=9)
    for e in ledger.entries:
        if e.mechanism in (Mechanism.BATTERY_SOH_CLIFF, Mechanism.PACK_THERMAL):
            assert powertrain[e.vin] in ("BEV", "PHEV"), e.mechanism


def test_ledger_is_deterministic_under_seed():
    a, _ = _small_ledger(seed=77)
    b, _ = _small_ledger(seed=77)
    assert len(a.entries) == len(b.entries)
    assert a.total_avoidable_cost == b.total_avoidable_cost


def test_failures_fall_inside_the_requested_horizon():
    horizon = 90.0
    ledger, _ = _small_ledger(horizon_days=horizon)
    end = START_TS + horizon * 86_400.0
    for e in ledger.entries:
        assert START_TS <= e.failure_ts <= end


def test_visible_failures_in_window_filters_correctly():
    ledger, _ = _small_ledger(horizon_days=365.0)
    mid = START_TS + 180 * 86_400.0
    window = ledger.visible_failures_in(START_TS, mid)
    assert all(e.visible and e.failure_ts <= mid for e in window)
    assert len(window) < len(ledger.entries)


def test_by_vin_index_round_trips():
    ledger, index = _small_ledger()
    assert sum(len(v) for v in index.values()) == len(ledger.entries)


def test_risk_is_not_uniform_across_the_population():
    """If every vehicle had identical hazard, ranking would be noise and Spearman meaningless."""
    ledger, index = _small_ledger(horizon_days=365.0)
    counts = [len(v) for v in index.values()]
    assert max(counts) >= 3, "no vehicle accumulated multiple faults; hazard looks uniform"


def test_annual_rates_sum_to_the_documented_total():
    assert 1.5 < sum(ANNUAL_RATE.values()) < 2.2
