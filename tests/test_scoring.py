"""Tests for A3 trip segmentation and A9 VEWS scoring + the attention budget.

Several of these encode the exact failure the algorithm exists to prevent. If a future
"simplification" reintroduces threshold-based segmentation or raises the board cap, these break.
"""

from __future__ import annotations

import pytest

from readyline.algorithms.segmentation import Fix, idle_seconds, segment_trips
from readyline.algorithms.vews import (
    MAX_BOARD_ITEMS,
    BoardItem,
    build_board,
    score,
)

# --------------------------------------------------------------------- A3 segmentation


def _track(spec: list[tuple[int, float]], *, lat=12.9716, lon=77.5946, step=1e-4) -> list[Fix]:
    """Build a track from (seconds_at_speed, speed_kmh) pairs, 1 Hz."""
    fixes: list[Fix] = []
    t = 0.0
    la, lo = lat, lon
    for seconds, speed in spec:
        for _ in range(seconds):
            fixes.append(Fix(ts=t, lat=la, lon=lo, speed_kmh=speed))
            t += 1.0
            if speed > 3.0:
                la += step
                lo += step
    return fixes


def test_simple_stop_move_stop_is_three_segments():
    fixes = _track([(300, 0.0), (600, 40.0), (300, 0.0)])
    segments = segment_trips(fixes)
    assert [s.state for s in segments] == ["STOP", "MOVE", "STOP"]


def test_gps_drift_at_standstill_does_not_create_a_trip():
    """A parked vehicle showing 2 km/h of phantom motion must stay stopped."""
    fixes = _track([(600, 2.0)])
    segments = segment_trips(fixes)
    assert [s.state for s in segments] == ["STOP"]


def test_traffic_light_pause_does_not_split_one_journey():
    """The failure a speed threshold cannot avoid: a 20 s pause fragmenting a single trip."""
    fixes = _track([(60, 0.0), (300, 35.0), (20, 0.0), (300, 35.0), (60, 0.0)])
    segments = segment_trips(fixes, min_stop_seconds=120.0)
    assert [s.state for s in segments] == ["STOP", "MOVE", "STOP"]


def test_genuine_delivery_stop_is_preserved():
    """A real 3-minute stop must survive, or idle time is undercounted."""
    fixes = _track([(60, 0.0), (300, 35.0), (180, 0.0), (300, 35.0), (60, 0.0)])
    segments = segment_trips(fixes, min_stop_seconds=120.0)
    assert [s.state for s in segments] == ["STOP", "MOVE", "STOP", "MOVE", "STOP"]


def test_stop_and_go_traffic_does_not_emit_dozens_of_trips():
    spec: list[tuple[int, float]] = []
    for _ in range(20):
        spec += [(10, 0.0), (20, 25.0)]
    segments = segment_trips(_track(spec), min_stop_seconds=120.0)
    assert len(segments) <= 3, f"fragmented into {len(segments)} segments"


def test_idle_seconds_sums_only_stops():
    fixes = _track([(300, 0.0), (600, 40.0), (180, 0.0)])
    segments = segment_trips(fixes)
    total_idle = idle_seconds(segments)
    assert total_idle == pytest.approx(300 + 179, abs=5)


def test_moving_segment_accumulates_distance():
    fixes = _track([(60, 0.0), (300, 40.0), (60, 0.0)])
    move = [s for s in segment_trips(fixes) if s.state == "MOVE"][0]
    assert move.distance_km > 0


def test_empty_and_single_fix_are_handled():
    assert segment_trips([]) == []
    one = segment_trips([Fix(0.0, 1.0, 1.0, 0.0)])
    assert len(one) == 1 and one[0].state == "STOP"


# --------------------------------------------------------------------- A9 VEWS


def test_healthy_vehicle_scores_low_and_stays_off_the_board():
    result = score(
        {
            "aux_battery_v": 12.8,
            "coolant_temp_c": 88.0,
            "soh_pct": 97.0,
            "pack_temp_c": 25.0,
            "soc_vs_shift_need_pct": 55.0,
            "tyre_pressure_drift_pct": 1.0,
            "dtc_max_severity": 0.0,
            "oil_life_pct": 70.0,
            "brake_wear_pct": 20.0,
        }
    )
    assert result.total == 0
    assert result.severity == "LOW"
    assert result.should_raise is False


def test_failing_aux_battery_scores_three_and_escalates():
    result = score({"aux_battery_v": 11.4, "coolant_temp_c": 88.0})
    assert result.severity == "CRITICAL"
    assert "aux_battery_v" in result.triggered_by


def test_one_catastrophic_signal_outranks_a_clean_total():
    """NEWS2's rule: a single 3-point vital escalates even when everything else is fine."""
    result = score({"pack_temp_c": 60.0, "soh_pct": 99.0, "coolant_temp_c": 85.0})
    assert result.total == 3
    assert result.severity == "CRITICAL"


def test_degradation_alone_never_becomes_a_stop_now():
    """A worn pack is not an emergency; it must not jump the CRITICAL lane on its own."""
    result = score({"soh_pct": 65.0})   # 3 points, but critical_at_points is None
    assert result.total == 3
    assert result.severity == "MEDIUM"


def test_accumulated_minor_issues_reach_high():
    result = score(
        {
            "aux_battery_v": 12.2,            # 1
            "tyre_pressure_drift_pct": 12.0,  # 2
            "oil_life_pct": 10.0,             # 2
            "brake_wear_pct": 70.0,           # 1
        }
    )
    assert result.total == 6
    assert result.severity == "HIGH"


def test_missing_signals_score_zero_not_error():
    """An ICE vehicle has no soh_pct; an OEM may not expose tyre pressure."""
    result = score({"coolant_temp_c": 90.0})
    assert result.total == 0
    assert result.severity == "LOW"


def test_unknown_signal_is_ignored():
    result = score({"not_a_real_signal": 999.0, "aux_battery_v": 12.9})
    assert result.total == 0


def test_result_records_band_table_version_for_audit():
    assert score({"aux_battery_v": 12.9}).band_table_version == "vews-1.0.0"


# --------------------------------------------------------------------- attention budget


def _item(vin: str, obs: dict, dollars: float) -> BoardItem:
    return BoardItem(vin=vin, result=score(obs), dollars_at_risk=dollars)


def test_board_is_hard_capped_at_twelve():
    """The cap is the product. Raising it rebuilds the portal the user muted."""
    candidates = [_item(f"VIN{i:03d}", {"aux_battery_v": 11.4}, 100.0) for i in range(40)]
    board = build_board(candidates)
    assert len(board.items) == MAX_BOARD_ITEMS == 12
    assert board.suppressed_count == 28


def test_low_severity_never_reaches_the_board():
    candidates = [_item("VIN001", {"coolant_temp_c": 88.0}, 5000.0)]
    board = build_board(candidates)
    assert board.items == ()
    assert board.suppressed_count == 0


def test_critical_outranks_a_more_expensive_medium():
    """Dollars break ties within a band, never across it."""
    cheap_critical = _item("CRIT", {"pack_temp_c": 60.0}, 50.0)
    pricey_medium = _item("MED", {"soh_pct": 65.0}, 9_000.0)
    board = build_board([pricey_medium, cheap_critical])
    assert [i.vin for i in board.items] == ["CRIT", "MED"]


def test_dollars_rank_within_the_same_severity():
    a = _item("LOWVAL", {"aux_battery_v": 11.4}, 100.0)
    b = _item("HIGHVAL", {"aux_battery_v": 11.4}, 900.0)
    board = build_board([a, b])
    assert [i.vin for i in board.items] == ["HIGHVAL", "LOWVAL"]


def test_board_ordering_is_deterministic_on_ties():
    """Two identical vehicles must not swap places between polls."""
    items = [_item(v, {"aux_battery_v": 11.4}, 100.0) for v in ("VIN_B", "VIN_A")]
    assert [i.vin for i in build_board(items).items] == ["VIN_A", "VIN_B"]
