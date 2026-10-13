"""Tests for the OEM normalisation registry and the telemetry generator.

The round-trip property test is the important one: anything the simulator can emit, in any
dialect, must normalise back to the same canonical values. That single property covers the whole
unit-conversion surface, which is where silent corruption lives.
"""

from __future__ import annotations


import pytest

from readyline.normalise import (
    DIALECTS,
    NormalisationError,
    normalise,
    shadow_compare,
)
from readyline_sim.faults import schedule_faults
from readyline_sim.fleet import generate_depots, generate_vehicles, plan_tenants
from readyline_sim.telemetry import encode, generate_events, seed_positions

START = 1_767_225_600.0


def _sample_events(n_vehicles: int = 6, duration: float = 12.0):
    tenants = plan_tenants(300, 5)
    depots = generate_depots(tenants)
    vehicles = list(generate_vehicles(tenants, depots))[:n_vehicles]
    _, by_vin = schedule_faults(iter(vehicles), horizon_days=30, start_ts=START, seed=4)
    seed_positions(vehicles, {d.depot_id: (d.lat, d.lon) for d in depots})
    return list(generate_events(vehicles, by_vin, start_ts=START, duration_s=duration,
                                hz=1.0, seed=99))


# --------------------------------------------------------------------- generator


def test_generator_emits_events():
    assert len(_sample_events()) > 0


def test_generator_injects_duplicates():
    """At-least-once delivery upstream. The Bloom filter (A6) exists for this."""
    events = _sample_events(n_vehicles=20, duration=40.0)
    seen = set()
    dupes = 0
    for e in events:
        key = (e.vin, e.seq)
        if key in seen:
            dupes += 1
        seen.add(key)
    assert dupes > 0, "no duplicates emitted; the dedup path would never be exercised"


def test_generator_injects_out_of_order_arrivals():
    """Mobile networks reorder. This is what makes event-time watermarking necessary."""
    events = _sample_events(n_vehicles=20, duration=40.0)
    per_vin: dict[str, list[float]] = {}
    for e in events:
        per_vin.setdefault(e.vin, []).append(e.ts)
    regressions = sum(
        1 for ts in per_vin.values() for a, b in zip(ts, ts[1:]) if b < a
    )
    assert regressions > 0, "stream was perfectly ordered; watermarking would be untested"


def test_generator_produces_a_burst():
    """Shift start: every vehicle reconnects at once. This is what breaks naive consumers.

    Compared pre-burst against burst explicitly. Comparing peak to the *median* does not work:
    the burst window is longer than the sampled stream, so the elevated rate becomes the median
    and the burst hides itself.
    """
    duration = 60.0
    events = _sample_events(n_vehicles=10, duration=duration)
    burst_start = duration * 0.3  # matches generate_events

    buckets: dict[int, int] = {}
    for e in events:
        buckets[int(e.ts - START)] = buckets.get(int(e.ts - START), 0) + 1

    before = [n for s, n in buckets.items() if s < burst_start - 1]
    during = [n for s, n in buckets.items() if burst_start + 1 <= s < duration - 1]
    assert before and during, "not enough samples either side of the burst"

    avg_before = sum(before) / len(before)
    avg_during = sum(during) / len(during)
    assert avg_during >= avg_before * 2, (
        f"no burst: {avg_before:.1f}/s before vs {avg_during:.1f}/s during"
    )


def test_ice_vehicles_have_no_soc_and_evs_have_no_fuel():
    for e in _sample_events(n_vehicles=30, duration=4.0):
        assert not (e.soc_pct is not None and e.fuel_pct is not None), e.vin


def test_sequence_numbers_increase_per_vehicle():
    per_vin: dict[str, list[int]] = {}
    for e in _sample_events(n_vehicles=8, duration=10.0):
        per_vin.setdefault(e.vin, []).append(e.seq)
    for vin, seqs in per_vin.items():
        # Duplicates repeat a seq, but it must never go backwards.
        assert all(b >= a for a, b in zip(seqs, seqs[1:])), vin


# --------------------------------------------------------------------- round trip


@pytest.mark.parametrize("dialect", ["tata_v2", "suzuki_v1", "mahindra_v3"])
def test_encode_normalise_round_trip_preserves_values(dialect: str):
    """The property that covers the entire unit-conversion surface at once.

    A normaliser that renamed keys without converting units would pass a shape check and fail
    here: Suzuki's miles and Fahrenheit, Mahindra's 0-1 SoC fraction.
    """
    for e in _sample_events(n_vehicles=8, duration=6.0):
        got = normalise(encode(e, dialect), dialect)

        assert got["vin"] == e.vin
        assert got["seq"] == e.seq
        assert got["ts"] == pytest.approx(e.ts, abs=1e-3)
        assert got["lat"] == pytest.approx(e.lat, abs=1e-6)
        assert got["lon"] == pytest.approx(e.lon, abs=1e-6)
        assert got["speed_kmh"] == pytest.approx(e.speed_kmh, abs=0.02)
        assert got["odo_km"] == pytest.approx(e.odo_km, abs=0.02)
        assert tuple(got["dtc"]) == e.dtc

        for canon, actual in (("soc_pct", e.soc_pct), ("soh_pct", e.soh_pct),
                              ("pack_temp_c", e.pack_temp_c), ("fuel_pct", e.fuel_pct),
                              ("coolant_temp_c", e.coolant_temp_c)):
            if actual is None:
                assert got[canon] is None, canon
            else:
                assert got[canon] == pytest.approx(actual, abs=0.02), canon


def test_suzuki_miles_are_actually_converted():
    """Explicit guard: 62.1371 mph is 100 km/h, not 62.1371 km/h."""
    payload = {
        "vehicleId": "1M8GDM9AXKP042788", "timestamp": 1_767_225_600_000, "sequence": 1,
        "position": {"latitude": 12.9, "longitude": 77.5},
        "speedMph": 62.1371, "odometerMiles": 621.371,
        "auxVoltage": 12.6,
    }
    got = normalise(payload, "suzuki_v1")
    assert got["speed_kmh"] == pytest.approx(100.0, abs=0.01)
    assert got["odo_km"] == pytest.approx(1000.0, abs=0.01)
    assert got["ts"] == pytest.approx(1_767_225_600.0)


def test_mahindra_soc_fraction_becomes_a_percentage():
    """0.41 is 41%, not 0.41%. Taken literally this strands a vehicle that is nearly half full."""
    payload = {
        "identity": {"vin": "1M8GDM9AXKP042788", "seq": 7},
        "time": {"epoch_s": START},
        "telemetry": {"geo": [12.9, 77.5], "velocity_kmph": 40.0, "distance_km": 100.0,
                      "energy": {"soc_frac": 0.41, "soh_frac": 0.93},
                      "health": {"aux_volts": 12.6}},
        "diagnostics": {"event": "TICK", "codes": []},
    }
    got = normalise(payload, "mahindra_v3")
    assert got["soc_pct"] == pytest.approx(41.0)
    assert got["soh_pct"] == pytest.approx(93.0)


def test_fahrenheit_is_converted():
    payload = {
        "vehicleId": "1M8GDM9AXKP042788", "timestamp": 1_767_225_600_000, "sequence": 1,
        "position": {"latitude": 12.9, "longitude": 77.5}, "speedMph": 0.0,
        "coolantTempF": 212.0, "packTempF": 32.0,
    }
    got = normalise(payload, "suzuki_v1")
    assert got["coolant_temp_c"] == pytest.approx(100.0, abs=0.01)
    assert got["pack_temp_c"] == pytest.approx(0.0, abs=0.01)


# --------------------------------------------------------------------- failure handling


def test_unknown_dialect_is_rejected():
    with pytest.raises(NormalisationError) as exc:
        normalise({"vin": "X"}, "ford_v9")
    assert exc.value.reason == "unknown_dialect"


def test_missing_required_field_is_rejected_not_defaulted():
    """Guessing a default would quietly corrupt every downstream aggregate."""
    with pytest.raises(NormalisationError) as exc:
        normalise({"ts": START, "seq": 1, "lat": 12.9, "lon": 77.5, "speed_kmh": 0}, "tata_v2")
    assert exc.value.reason == "missing_required_field"


def test_out_of_range_value_is_rejected():
    """A -40% state of charge is a broken mapping, not an unusual vehicle."""
    payload = {"vin": "1M8GDM9AXKP042788", "ts": START, "seq": 1, "lat": 12.9, "lon": 77.5,
               "speed_kmh": 10.0, "soc_pct": -40.0}
    with pytest.raises(NormalisationError) as exc:
        normalise(payload, "tata_v2")
    assert exc.value.reason == "out_of_range"


def test_absent_optional_field_becomes_none():
    payload = {"vin": "1M8GDM9AXKP042788", "ts": START, "seq": 1,
               "lat": 12.9, "lon": 77.5, "speed_kmh": 10.0}
    got = normalise(payload, "tata_v2")
    assert got["soc_pct"] is None
    assert got["evt"] == "TICK"


# --------------------------------------------------------------------- shadow mode


def test_shadow_compare_detects_a_unit_regression():
    """How a new dialect is promoted: diff it against the live mapping on real traffic first."""
    e = _sample_events(n_vehicles=2, duration=3.0)[0]
    # The same bytes read under the wrong dialect must not silently agree.
    payload = encode(e, "tata_v2")
    diffs = shadow_compare(payload, "tata_v2", "mahindra_v3")
    assert diffs, "a wrong mapping produced no diff; shadow mode would promote it"


def test_shadow_compare_is_empty_for_an_identical_mapping():
    e = _sample_events(n_vehicles=2, duration=3.0)[0]
    assert shadow_compare(encode(e, "tata_v2"), "tata_v2", "tata_v2") == {}


def test_every_dialect_declares_the_same_canonical_fields():
    """A dialect missing a field would produce events the detector cannot score."""
    reference = set(DIALECTS["tata_v2"])
    for name, spec in DIALECTS.items():
        assert set(spec) == reference, f"{name} diverges: {set(spec) ^ reference}"
