"""Correctness tests for the graded algorithm core.

These assert behaviour, not coverage. Several encode the specific failure each algorithm exists
to prevent, so that a "simplification" that reintroduces the failure breaks the build.
"""

from __future__ import annotations

import math
import random

import pytest

from readyline.algorithms.energy import (
    ChargerNode,
    Edge,
    Node,
    RoadGraph,
    haversine_km,
    naive_charge_on_arrival_cost,
    nearest_reachable_charger,
    optimal_charging_schedule,
)
from readyline.algorithms.identifiers import (
    is_valid_vin,
    parse_dtc,
    parse_dtc_list,
    vin_check_digit,
)
from readyline.algorithms.streaming import (
    BloomFilter,
    CountMinSketch,
    TopK,
    UnionFind,
    cluster_parked_vehicles,
    dedup_key,
    geohash_encode,
    geohash_neighbours,
)

# --------------------------------------------------------------------- A4 VIN


def test_known_good_vin_passes_including_check_digit():
    # Widely used ISO 3779 worked example; check digit is 'X'.
    assert vin_check_digit("1M8GDM9AXKP042788") == "X"
    assert is_valid_vin("1M8GDM9AXKP042788")


def test_vin_rejects_letters_iso_excludes():
    # I, O and Q are never issued; a naive [A-Z0-9]{17} regex would wrongly accept these.
    for bad in ("1M8GDM9AXKP04278I", "1M8GDM9AXKP04278O", "1M8GDM9AXKP04278Q"):
        assert not is_valid_vin(bad), bad


def test_vin_rejects_wrong_length():
    assert not is_valid_vin("1M8GDM9AXKP04278")     # 16
    assert not is_valid_vin("1M8GDM9AXKP0427888")   # 18


def test_vin_rejects_bad_check_digit_but_can_be_relaxed_per_oem():
    corrupted = "1M8GDM9A0KP042788"  # check digit forced to '0'
    assert not is_valid_vin(corrupted)
    assert is_valid_vin(corrupted, require_check_digit=False)


# --------------------------------------------------------------------- A5 DTC


def test_parse_dtc_extracts_system_and_severity():
    dtc = parse_dtc("P0301")
    assert dtc is not None
    assert dtc.system == "powertrain"
    assert dtc.severity == "HIGH"
    assert dtc.manufacturer_specific is False


def test_manufacturer_specific_codes_are_flagged():
    dtc = parse_dtc("P1234")
    assert dtc is not None and dtc.manufacturer_specific is True


def test_unknown_code_defaults_to_medium_not_low():
    # An unclassified code must never be silently demoted off the board.
    dtc = parse_dtc("C0123")
    assert dtc is not None and dtc.severity == "MEDIUM"


def test_malformed_dtc_returns_none_rather_than_raising():
    # At 100k events/sec a new OEM dialect must not raise into the hot path.
    for bad in ("", "XYZ", "P03", "P03011", "Z0301", "P9301"):
        assert parse_dtc(bad) is None, bad


def test_parse_dtc_list_separates_rejects_for_the_dlq():
    parsed, rejected = parse_dtc_list(["P0301", "garbage", "U0100"])
    assert [d.code for d in parsed] == ["P0301", "U0100"]
    assert rejected == ["garbage"]


# --------------------------------------------------------------------- A6 Bloom


def test_bloom_never_reports_false_negative():
    """The one-sided guarantee. A false negative would duplicate a real event."""
    bf = BloomFilter(expected_items=10_000, false_positive_rate=0.01)
    keys = [dedup_key(f"VIN{i:013d}", i) for i in range(10_000)]
    for k in keys:
        bf.add(k)
    assert all(k in bf for k in keys)


def test_bloom_false_positive_rate_is_near_target():
    n, p = 20_000, 0.01
    bf = BloomFilter(expected_items=n, false_positive_rate=p)
    for i in range(n):
        bf.add(dedup_key("VINAAAAAAAAAAAA", i))
    # Probe with keys that were definitely not inserted.
    probes = [dedup_key("VINBBBBBBBBBBBB", i) for i in range(20_000)]
    observed = sum(1 for k in probes if k in bf) / len(probes)
    assert observed < p * 2.5, f"observed FP rate {observed:.4f} far above target {p}"


def test_add_if_absent_detects_duplicate_events():
    bf = BloomFilter(expected_items=1_000)
    k = dedup_key("1M8GDM9AXKP042788", 88412)
    assert bf.add_if_absent(k) is True    # first sighting
    assert bf.add_if_absent(k) is False   # replayed duplicate


def test_bloom_sizing_follows_the_standard_formula():
    bf = BloomFilter(expected_items=10_000, false_positive_rate=0.01)
    expected_m = -10_000 * math.log(0.01) / (math.log(2) ** 2)
    assert bf.bits == pytest.approx(expected_m, rel=0.01)
    assert bf.hashes == 7


# --------------------------------------------------------------------- A7 Count-Min / top-K


def test_count_min_never_underestimates():
    """One-sided error: over-counting is safe, under-counting hides a real cost leak."""
    cms = CountMinSketch(epsilon=0.001, delta=0.01)
    truth: dict[str, int] = {}
    rng = random.Random(7)
    for _ in range(20_000):
        key = f"VIN{rng.randrange(500):05d}"
        cms.update(key)
        truth[key] = truth.get(key, 0) + 1
    for key, true_count in truth.items():
        assert cms.estimate(key) >= true_count


def test_topk_finds_the_real_heavy_hitters():
    top = TopK(k=5)
    rng = random.Random(11)
    # 5 planted heavy hitters, buried in noise.
    heavy = {f"HEAVY{i}": 0 for i in range(5)}
    for _ in range(50_000):
        key = f"NOISE{rng.randrange(5_000)}"
        top.offer(key)
    for key in heavy:
        for _ in range(2_000):
            top.offer(key)
    found = {k for k, _ in top.top()}
    assert found == set(heavy)


# --------------------------------------------------------------------- A8 geohash / union-find


def test_geohash_matches_reference_value():
    # Canonical reference: the geohash of (57.64911, 10.40744) is "u4pruyd".
    assert geohash_encode(57.64911, 10.40744, 7) == "u4pruyd"


def test_geohash_precision_7_groups_nearby_and_splits_distant():
    a = geohash_encode(12.9716, 77.5946, 7)
    near = geohash_encode(12.97165, 77.59465, 7)   # ~7 m away
    far = geohash_encode(12.9900, 77.6200, 7)      # ~3 km away
    assert a == near
    assert a != far


def test_geohash_rejects_out_of_range():
    with pytest.raises(ValueError):
        geohash_encode(91.0, 0.0)


def test_geohash_neighbours_includes_self_and_surrounds():
    cells = geohash_neighbours(geohash_encode(12.9716, 77.5946, 7))
    assert geohash_encode(12.9716, 77.5946, 7) in cells
    assert len(cells) >= 8


def test_union_find_groups_transitively():
    uf = UnionFind()
    uf.union("a", "b")
    uf.union("b", "c")
    uf.union("x", "y")
    assert uf.find("a") == uf.find("c")
    assert uf.find("a") != uf.find("x")


def test_cluster_detects_unapproved_depot():
    """Vehicles parked together away from any registered depot must be flagged."""
    depot_lat, depot_lon = 12.9716, 77.5946
    approved = {geohash_encode(depot_lat, depot_lon, 7)}

    parked = [(f"VIN{i:03d}", depot_lat + i * 1e-5, depot_lon + i * 1e-5) for i in range(4)]
    # A second, unregistered gathering ~50 km away.
    rogue_lat, rogue_lon = 13.4200, 77.9000
    parked += [(f"ROGUE{i:03d}", rogue_lat + i * 1e-5, rogue_lon + i * 1e-5) for i in range(5)]

    clusters = cluster_parked_vehicles(parked, approved, min_vehicles=3)
    unapproved = [c for c in clusters if not c.is_approved]
    assert len(unapproved) == 1
    assert all(v.startswith("ROGUE") for v in unapproved[0].vins)
    assert len(unapproved[0].vins) == 5


def test_cluster_ignores_groups_below_threshold():
    clusters = cluster_parked_vehicles(
        [("A", 1.0, 1.0), ("B", 1.00001, 1.00001)], approved_geohashes=set(), min_vehicles=3
    )
    assert clusters == []


# --------------------------------------------------------------------- A1 A* charger


def _line_graph() -> tuple[RoadGraph, list[ChargerNode]]:
    """0 -- 1 -- 2 -- 3 along a line, charger at node 3 and a far one at node 2."""
    nodes = [Node(i, 12.9716 + i * 0.05, 77.5946) for i in range(4)]
    edges = {
        0: [Edge(1, 5.0, 0.18)],
        1: [Edge(2, 5.0, 0.18), Edge(0, 5.0, 0.18)],
        2: [Edge(3, 5.0, 0.18), Edge(1, 5.0, 0.18)],
        3: [Edge(2, 5.0, 0.18)],
    }
    return RoadGraph(nodes, edges), [ChargerNode(charger_id=99, node_id=3, power_kw=50.0)]


def test_astar_finds_charger_within_range():
    graph, chargers = _line_graph()
    result = nearest_reachable_charger(
        graph, start_node=0, chargers=chargers, usable_kwh=20.0, battery_kwh=40.0
    )
    assert result is not None
    assert result.charger_id == 99
    assert result.path == (0, 1, 2, 3)
    assert result.distance_km == pytest.approx(15.0)
    assert result.energy_kwh == pytest.approx(15.0 * 0.18)


def test_astar_returns_none_when_stranded():
    """The reserve must prevent routing to a charger the vehicle cannot actually reach."""
    graph, chargers = _line_graph()
    # 15 km at 0.18 kWh/km = 2.7 kWh needed, but only 3.0 kWh total with a 10% (4 kWh) reserve.
    result = nearest_reachable_charger(
        graph, start_node=0, chargers=chargers, usable_kwh=3.0, battery_kwh=40.0
    )
    assert result is None


def test_astar_prefers_lower_energy_not_shorter_distance():
    """A longer but far more efficient route must win, because energy is the binding constraint."""
    nodes = [Node(0, 0.0, 0.0), Node(1, 0.0, 0.1), Node(2, 0.0, 0.2), Node(3, 0.0, 0.05)]
    edges = {
        0: [Edge(3, 6.0, 0.60), Edge(1, 11.0, 0.10)],  # short+thirsty (3.6) vs long+efficient (1.1)
        1: [Edge(2, 1.0, 0.10)],
        3: [Edge(2, 6.0, 0.60)],
        2: [],
    }
    graph = RoadGraph(nodes, edges)
    result = nearest_reachable_charger(
        graph, 0, [ChargerNode(1, 2, 50.0)], usable_kwh=10.0, battery_kwh=40.0
    )
    assert result is not None
    assert result.path == (0, 1, 2)
    assert result.energy_kwh == pytest.approx(1.2)


def test_astar_handles_no_chargers():
    graph, _ = _line_graph()
    assert nearest_reachable_charger(graph, 0, [], usable_kwh=40.0, battery_kwh=40.0) is None


def test_haversine_known_distance():
    # Bangalore -> Chennai, ~290 km great-circle.
    d = haversine_km(12.9716, 77.5946, 13.0827, 80.2707)
    assert 280 < d < 300


# --------------------------------------------------------------------- A2 DP charging


def test_dp_charges_in_the_cheapest_slots():
    # Four 1-hour slots; slot 2 is far cheapest.
    plan = optimal_charging_schedule(
        soc_start_pct=20.0,
        soc_target_pct=60.0,
        battery_kwh=50.0,
        max_charge_kw=50.0,
        slot_minutes=60,
        tariff_per_slot=[12.0, 12.0, 2.0, 12.0],
    )
    assert plan.feasible
    assert plan.final_soc_pct >= 60.0
    # Essentially all the energy should land in the cheap slot.
    assert plan.power_by_slot[2] > sum(plan.power_by_slot) * 0.9


def test_dp_beats_greedy_when_the_cheap_window_is_too_short():
    """The case that makes the DP worth writing.

    One cheap slot cannot supply the whole requirement, so a greedy 'fill the cheap window then
    buy the rest at whatever comes next' strategy is forced onto the most expensive slot. The DP
    looks ahead and uses the medium-priced slot instead.
    """
    tariff = [20.0, 2.0, 6.0, 20.0]  # cheap slot is slot 1, medium is slot 2
    kwargs = dict(
        soc_start_pct=10.0,
        soc_target_pct=90.0,
        battery_kwh=60.0,
        max_charge_kw=30.0,     # one slot supplies only 30 kWh of the ~48 kWh needed
        slot_minutes=60,
        tariff_per_slot=tariff,
    )
    plan = optimal_charging_schedule(**kwargs)
    greedy = naive_charge_on_arrival_cost(**kwargs)

    assert plan.feasible
    assert plan.total_cost < greedy
    # It must avoid the expensive slots entirely when cheaper capacity exists.
    assert plan.power_by_slot[0] == 0.0
    assert plan.power_by_slot[3] == 0.0


def test_dp_reports_infeasible_rather_than_missing_the_deadline():
    """A plan that cannot meet the departure must say so, not silently under-deliver."""
    plan = optimal_charging_schedule(
        soc_start_pct=10.0,
        soc_target_pct=100.0,
        battery_kwh=100.0,
        max_charge_kw=7.0,       # slow charger
        slot_minutes=60,
        tariff_per_slot=[5.0, 5.0],  # only 2 hours before departure
    )
    assert plan.feasible is False
    assert plan.final_soc_pct < 100.0
    assert plan.final_soc_pct > 10.0  # it still charged as much as it could


def test_dp_accounts_for_charging_efficiency():
    """Grid energy bought exceeds energy stored; ignoring that understates cost ~8%."""
    common = dict(
        soc_start_pct=0.0,
        soc_target_pct=100.0,
        battery_kwh=100.0,
        max_charge_kw=100.0,
        slot_minutes=60,
        tariff_per_slot=[10.0] * 3,
    )
    lossy = optimal_charging_schedule(**common, charge_efficiency=0.92)
    ideal = optimal_charging_schedule(**common, charge_efficiency=1.0)
    assert lossy.total_cost > ideal.total_cost
    assert lossy.total_cost == pytest.approx(ideal.total_cost / 0.92, rel=1e-6)


def test_dp_no_charge_needed_is_free():
    plan = optimal_charging_schedule(
        soc_start_pct=80.0,
        soc_target_pct=50.0,
        battery_kwh=50.0,
        max_charge_kw=50.0,
        slot_minutes=60,
        tariff_per_slot=[10.0, 10.0],
    )
    assert plan.feasible
    assert plan.total_cost == 0.0
    assert all(p == 0.0 for p in plan.power_by_slot)


def test_dp_empty_horizon_is_handled():
    plan = optimal_charging_schedule(
        soc_start_pct=50.0,
        soc_target_pct=80.0,
        battery_kwh=50.0,
        max_charge_kw=50.0,
        slot_minutes=60,
        tariff_per_slot=[],
    )
    assert plan.feasible is False
