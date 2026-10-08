"""Telemetry generation: turn a fleet plus a fault ledger into a realistic event stream.

This is the component whose absence meant nothing in the platform could ever run. It produces the
observable consequences of the latent vehicle state in `fleet.py`, degraded by whatever faults
`faults.py` scheduled -- and it injects the three stream pathologies the brief explicitly calls
for: bursts, duplicates and out-of-order arrival.

The separation that keeps the ground truth honest: this module may read a vehicle's latent health
and a fault's onset time, but it emits only **noisy observables**. The detector sees the stream;
it never sees the ledger. Comparing the two afterwards is what produces precision and recall.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from typing import Iterator, Mapping, Sequence

from .faults import LedgerEntry, Mechanism
from .fleet import Vehicle

# Three OEM dialects. The same physical reading encoded three different ways, because the
# normalisation registry is only exercised if payloads genuinely differ. An SDV OTA update
# renaming a field is the failure this exists to survive.
DIALECTS = ("tata_v2", "suzuki_v1", "mahindra_v3")


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """The normalised internal form. Every dialect maps onto exactly this."""

    vin: str
    tenant_id: int
    fleet_id: int
    ts: float
    seq: int
    lat: float
    lon: float
    speed_kmh: float
    odo_km: float
    soc_pct: float | None
    soh_pct: float | None
    pack_temp_c: float | None
    fuel_pct: float | None
    aux_battery_v: float
    coolant_temp_c: float | None
    tyre_drift_pct: float
    oil_life_pct: float | None
    brake_wear_pct: float
    evt: str
    dtc: tuple[str, ...]


def _active_mechanisms(
    ledger: Sequence[LedgerEntry], ts: float
) -> list[tuple[Mechanism, float]]:
    """Mechanisms degrading at `ts`, with progress in [0, 1] from onset to failure."""
    out: list[tuple[Mechanism, float]] = []
    for e in ledger:
        if e.onset_ts <= ts <= e.failure_ts:
            span = e.failure_ts - e.onset_ts
            progress = 1.0 if span <= 0 else (ts - e.onset_ts) / span
            out.append((e.mechanism, progress))
    return out


def _apply_degradation(
    v: Vehicle, active: Sequence[tuple[Mechanism, float]], rng: random.Random
) -> Vehicle:
    """Move observables along their degradation curves.

    Each curve is deliberately *not* linear and is noised, so a model cannot trivially invert it.
    The ML re-ranker never sees these formulas, only the resulting samples.
    """
    aux = v.aux_battery_v
    coolant = v.coolant_temp_c
    soh = v.soh_pct
    pack = v.pack_temp_c
    tyre = v.tyre_drift_pct
    brake = v.brake_wear_pct
    dtcs: list[str] = []

    for mech, p in active:
        if mech is Mechanism.AUX_BATTERY_DECAY:
            # Resting voltage sags roughly exponentially toward no-crank.
            aux = min(aux, 12.75 - 1.5 * (p ** 1.6))
        elif mech is Mechanism.COOLANT_LEAK:
            coolant = max(coolant or 88.0, 88.0 + 30.0 * (p ** 2))
            if p > 0.55:
                dtcs.append("P0128")
            if p > 0.85:
                dtcs.append("P0217")
        elif mech is Mechanism.BATTERY_SOH_CLIFF:
            soh = min(soh, soh - 18.0 * (p ** 1.3))
            if p > 0.7:
                dtcs.append("P0A80")
        elif mech is Mechanism.PACK_THERMAL:
            pack = max(pack, 28.0 + 35.0 * (p ** 1.8))
        elif mech is Mechanism.BRAKE_WEAR:
            brake = min(99.0, max(brake, brake + 45.0 * p))

    return replace(
        v,
        aux_battery_v=round(aux + rng.gauss(0, 0.035), 3),
        coolant_temp_c=round((coolant or 88.0) + rng.gauss(0, 1.1), 2),
        # Clamped at both ends. Noise on a healthy 100% pack was pushing SoH to 100.2, which the
        # normaliser correctly rejected as physically impossible -- 0.3% of the stream went to
        # the DLQ before this was fixed. State of health is a fraction of original capacity and
        # cannot exceed it.
        soh_pct=round(min(100.0, max(40.0, soh + rng.gauss(0, 0.25))), 2),
        pack_temp_c=round(pack + rng.gauss(0, 0.8), 2),
        tyre_drift_pct=round(min(40.0, tyre + abs(rng.gauss(0, 0.05))), 3),
        brake_wear_pct=round(brake, 2),
    )


def _duty_cycle_speed(ts: float, rng: random.Random) -> tuple[float, str]:
    """Speed from time of day. Depots are quiet overnight and busy 07:00-19:00."""
    hour = (ts % 86_400) / 3600.0
    if 7.0 <= hour <= 19.0:
        if rng.random() < 0.18:
            return 0.0, "IDLE"           # traffic, loading, delivery stop
        return max(0.0, rng.gauss(38.0, 16.0)), "TICK"
    if rng.random() < 0.94:
        return 0.0, "PARKED"
    return max(0.0, rng.gauss(25.0, 10.0)), "TICK"


def generate_events(
    vehicles: Sequence[Vehicle],
    ledger_by_vin: Mapping[str, Sequence[LedgerEntry]],
    *,
    start_ts: float,
    duration_s: float,
    hz: float = 1.0,
    seed: int = 20261008,
    burst_multiplier: int = 3,
    burst_window_s: float = 300.0,
    duplicate_rate: float = 0.004,
    out_of_order_rate: float = 0.012,
    max_reorder_s: float = 8.0,
) -> Iterator[CanonicalEvent]:
    """Stream canonical events for `vehicles` over `duration_s`.

    Three pathologies the brief names, all on by default because a pipeline that only works on
    clean input is not a pipeline:

      * **Bursts** -- at shift start every vehicle reconnects at once. Modelled as a window in
        which emission rate is multiplied, which is what actually breaks naive consumers.
      * **Duplicates** -- at-least-once delivery upstream means the same (vin, seq) arrives twice.
        The Bloom filter (A6) exists for exactly this.
      * **Out-of-order** -- mobile networks reorder. Events are emitted with a backdated
        timestamp, which is what makes event-time watermarking necessary rather than decorative.

    Yields in emission order, not timestamp order -- deliberately.
    """
    rng = random.Random(seed + 11)
    state = {v.vin: v for v in vehicles}
    seqs = {v.vin: 0 for v in vehicles}
    step = 1.0 / hz
    burst_start = start_ts + duration_s * 0.3

    t = start_ts
    while t < start_ts + duration_s:
        in_burst = burst_start <= t < burst_start + burst_window_s
        repeats = burst_multiplier if in_burst else 1

        for _ in range(repeats):
            for vin, v in state.items():
                active = _active_mechanisms(ledger_by_vin.get(vin, ()), t)
                v = _apply_degradation(v, active, rng)

                speed, evt = _duty_cycle_speed(t, rng)
                # Move the vehicle. ~0.009 deg per km is close enough at these latitudes.
                dist_km = speed * (step / 3600.0)
                heading = rng.uniform(0, 2 * math.pi)
                cur_lat, cur_lon = _POS.get(vin, (12.9716, 77.5946))
                new_lat = _clamp(cur_lat + dist_km * math.cos(heading) * 0.009, -89.9, 89.9)
                new_lon = _clamp(cur_lon + dist_km * math.sin(heading) * 0.009, -179.9, 179.9)

                is_ev = v.model.battery_kwh is not None
                soc = v.soc_pct
                fuel = v.fuel_pct
                if is_ev and v.model.kwh_per_km:
                    used = dist_km * v.model.kwh_per_km / v.model.battery_kwh * 100.0
                    soc = max(0.0, soc - used)
                elif v.model.litres_per_km and v.model.tank_litres:
                    used = dist_km * v.model.litres_per_km / v.model.tank_litres * 100.0
                    fuel = max(0.0, fuel - used)

                dtcs: list[str] = []
                for mech, p in active:
                    if mech is Mechanism.COOLANT_LEAK and p > 0.55:
                        dtcs.append("P0128")
                    if mech is Mechanism.BATTERY_SOH_CLIFF and p > 0.7:
                        dtcs.append("P0A80")
                if evt == "TICK" and speed > 55 and rng.random() < 0.002:
                    evt = "HARSH_BRAKE"

                seqs[vin] += 1
                v = replace(v, soc_pct=soc, fuel_pct=fuel,
                            odo_km=round(v.odo_km + dist_km, 3))
                state[vin] = v
                _POS[vin] = (new_lat, new_lon)

                emit_ts = t
                if rng.random() < out_of_order_rate:
                    emit_ts = t - rng.uniform(0.5, max_reorder_s)

                ev = CanonicalEvent(
                    vin=vin,
                    tenant_id=v.tenant_id,
                    fleet_id=v.fleet_id,
                    ts=emit_ts,
                    seq=seqs[vin],
                    lat=round(new_lat, 6),
                    lon=round(new_lon, 6),
                    speed_kmh=round(speed, 2),
                    odo_km=state[vin].odo_km,
                    soc_pct=round(soc, 2) if is_ev else None,
                    soh_pct=v.soh_pct if is_ev else None,
                    pack_temp_c=v.pack_temp_c if is_ev else None,
                    fuel_pct=round(fuel, 2) if not is_ev else None,
                    aux_battery_v=v.aux_battery_v,
                    coolant_temp_c=v.coolant_temp_c if v.model.tank_litres else None,
                    tyre_drift_pct=v.tyre_drift_pct,
                    oil_life_pct=v.oil_life_pct if v.model.tank_litres else None,
                    brake_wear_pct=v.brake_wear_pct,
                    evt=evt,
                    dtc=tuple(dict.fromkeys(dtcs)),
                )
                yield ev

                # At-least-once upstream: the same event can arrive twice.
                if rng.random() < duplicate_rate:
                    yield ev

        t += step


# Vehicle has no lat/lon field (it is depot-anchored), so position is carried in a side table
# keyed by VIN. Keeping it out of the dataclass avoids widening a 100k-instance structure.
_POS: dict[str, tuple[float, float]] = {}


def seed_positions(vehicles: Sequence[Vehicle], depots: Mapping[int, tuple[float, float]]) -> None:
    """Place every vehicle at its depot before the stream starts."""
    for v in vehicles:
        _POS[v.vin] = depots.get(v.depot_id, (12.9716, 77.5946))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ----------------------------------------------------------------- OEM dialect encoding


def encode(event: CanonicalEvent, dialect: str) -> dict:
    """Render a canonical event in one OEM's wire format.

    Each dialect differs in field names, units and SoC semantics -- the three ways real OEM feeds
    actually diverge. `suzuki_v1` reports distance in miles and temperature in Fahrenheit;
    `mahindra_v3` nests everything and reports SoC as a 0-1 fraction. A normaliser that only
    renames keys would silently produce a vehicle with 40% the range it really has.
    """
    if dialect == "tata_v2":
        return {
            "vin": event.vin, "ts": event.ts, "seq": event.seq,
            "lat": event.lat, "lon": event.lon,
            "speed_kmh": event.speed_kmh, "odo_km": event.odo_km,
            "soc_pct": event.soc_pct, "soh_pct": event.soh_pct,
            "pack_c": event.pack_temp_c, "fuel_pct": event.fuel_pct,
            "aux_v": event.aux_battery_v, "cool_c": event.coolant_temp_c,
            "tyre_drift": event.tyre_drift_pct, "oil_life": event.oil_life_pct,
            "brake_wear": event.brake_wear_pct,
            "evt": event.evt, "dtc": list(event.dtc),
        }
    if dialect == "suzuki_v1":
        return {
            "vehicleId": event.vin, "timestamp": int(event.ts * 1000), "sequence": event.seq,
            "position": {"latitude": event.lat, "longitude": event.lon},
            "speedMph": round(event.speed_kmh / 1.609344, 2),
            "odometerMiles": round(event.odo_km / 1.609344, 3),
            "batteryStateOfCharge": event.soc_pct,
            "batteryHealth": event.soh_pct,
            "packTempF": None if event.pack_temp_c is None
                         else round(event.pack_temp_c * 9 / 5 + 32, 2),
            "fuelLevel": event.fuel_pct,
            "auxVoltage": event.aux_battery_v,
            "coolantTempF": None if event.coolant_temp_c is None
                            else round(event.coolant_temp_c * 9 / 5 + 32, 2),
            "tyrePressureDrift": event.tyre_drift_pct,
            "oilLifeRemaining": event.oil_life_pct,
            "brakePadWear": event.brake_wear_pct,
            "eventType": event.evt, "faultCodes": list(event.dtc),
        }
    if dialect == "mahindra_v3":
        return {
            "identity": {"vin": event.vin, "seq": event.seq},
            "time": {"epoch_s": event.ts},
            "telemetry": {
                "geo": [event.lat, event.lon],
                "velocity_kmph": event.speed_kmh,
                "distance_km": event.odo_km,
                "energy": {
                    # SoC as a 0-1 fraction, not a percentage. The classic off-by-100 bug.
                    "soc_frac": None if event.soc_pct is None else round(event.soc_pct / 100.0, 5),
                    "soh_frac": None if event.soh_pct is None else round(event.soh_pct / 100.0, 5),
                    "pack_temp_c": event.pack_temp_c,
                    "fuel_frac": None if event.fuel_pct is None else round(event.fuel_pct / 100.0, 5),
                },
                "health": {
                    "aux_volts": event.aux_battery_v,
                    "coolant_c": event.coolant_temp_c,
                    "tyre_drift_pct": event.tyre_drift_pct,
                    "oil_life_pct": event.oil_life_pct,
                    "brake_wear_pct": event.brake_wear_pct,
                },
            },
            "diagnostics": {"event": event.evt, "codes": list(event.dtc)},
        }
    raise ValueError(f"unknown dialect: {dialect}")
