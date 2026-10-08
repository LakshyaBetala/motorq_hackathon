"""The normalisation registry: map any OEM dialect onto one canonical event.

This is the subsystem that makes a mixed fleet possible, and the one most likely to break in
production, because Software-Defined Vehicles get OTA updates that rename fields without notice.

Three properties the design turns on:

  1. **A dialect is versioned data, not code.** Adding or changing a mapping is a registry entry,
     so onboarding an OEM does not require a deploy. A `FieldMap` is a plain declarative object.
  2. **Units are part of the mapping, not an afterthought.** The dialects differ in units, not
     just key names: Suzuki reports miles and Fahrenheit, Mahindra reports SoC as a 0-1 fraction.
     A normaliser that only renamed keys would report a vehicle at 0.41% charge instead of 41%,
     or a 100 km range as 62. Both are silent, both are catastrophic for a charging plan.
  3. **Unknown payloads go to the DLQ, never to a guess.** If a required field is missing or a
     value is out of physical range, the envelope is rejected with a reason and replayed later
     from MongoDB once the mapping is fixed. Guessing a default would quietly corrupt the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

MILES_TO_KM = 1.609344


class NormalisationError(ValueError):
    """Raised when an envelope cannot be mapped. Carries a machine-readable reason for the DLQ."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _dig(payload: Mapping[str, Any], path: Sequence[str | int]) -> Any:
    """Walk a nested path, returning None if any hop is absent."""
    cur: Any = payload
    for key in path:
        if cur is None:
            return None
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


@dataclass(frozen=True, slots=True)
class FieldMap:
    """One canonical field, and how to obtain it from a dialect's payload."""

    path: tuple[str | int, ...]
    transform: Callable[[Any], Any] | None = None
    required: bool = False
    # Physical plausibility bounds. A reading outside these is rejected rather than ingested:
    # a -40% state of charge is not a vehicle telling us something, it is a broken mapping.
    lo: float | None = None
    hi: float | None = None


def _pct_from_fraction(x: Any) -> float | None:
    return None if x is None else round(float(x) * 100.0, 4)


def _km_from_miles(x: Any) -> float | None:
    return None if x is None else round(float(x) * MILES_TO_KM, 4)


def _c_from_f(x: Any) -> float | None:
    return None if x is None else round((float(x) - 32.0) * 5.0 / 9.0, 4)


def _s_from_ms(x: Any) -> float | None:
    return None if x is None else float(x) / 1000.0


# ----------------------------------------------------------------- the registry

DIALECTS: dict[str, dict[str, FieldMap]] = {
    "tata_v2": {
        "vin":            FieldMap(("vin",), required=True),
        "ts":             FieldMap(("ts",), required=True),
        "seq":            FieldMap(("seq",), required=True),
        "lat":            FieldMap(("lat",), required=True, lo=-90, hi=90),
        "lon":            FieldMap(("lon",), required=True, lo=-180, hi=180),
        "speed_kmh":      FieldMap(("speed_kmh",), required=True, lo=0, hi=300),
        "odo_km":         FieldMap(("odo_km",), lo=0, hi=5_000_000),
        "soc_pct":        FieldMap(("soc_pct",), lo=0, hi=100),
        "soh_pct":        FieldMap(("soh_pct",), lo=0, hi=100),
        "pack_temp_c":    FieldMap(("pack_c",), lo=-50, hi=150),
        "fuel_pct":       FieldMap(("fuel_pct",), lo=0, hi=100),
        "aux_battery_v":  FieldMap(("aux_v",), lo=0, hi=36),
        "coolant_temp_c": FieldMap(("cool_c",), lo=-50, hi=200),
        "tyre_drift_pct": FieldMap(("tyre_drift",), lo=0, hi=100),
        "oil_life_pct":   FieldMap(("oil_life",), lo=0, hi=100),
        "brake_wear_pct": FieldMap(("brake_wear",), lo=0, hi=100),
        "evt":            FieldMap(("evt",)),
        "dtc":            FieldMap(("dtc",)),
    },
    "suzuki_v1": {
        "vin":            FieldMap(("vehicleId",), required=True),
        # Milliseconds, not seconds. Off by 1000x if taken literally.
        "ts":             FieldMap(("timestamp",), _s_from_ms, required=True),
        "seq":            FieldMap(("sequence",), required=True),
        "lat":            FieldMap(("position", "latitude"), required=True, lo=-90, hi=90),
        "lon":            FieldMap(("position", "longitude"), required=True, lo=-180, hi=180),
        "speed_kmh":      FieldMap(("speedMph",), _km_from_miles, required=True, lo=0, hi=300),
        "odo_km":         FieldMap(("odometerMiles",), _km_from_miles, lo=0, hi=5_000_000),
        "soc_pct":        FieldMap(("batteryStateOfCharge",), lo=0, hi=100),
        "soh_pct":        FieldMap(("batteryHealth",), lo=0, hi=100),
        "pack_temp_c":    FieldMap(("packTempF",), _c_from_f, lo=-50, hi=150),
        "fuel_pct":       FieldMap(("fuelLevel",), lo=0, hi=100),
        "aux_battery_v":  FieldMap(("auxVoltage",), lo=0, hi=36),
        "coolant_temp_c": FieldMap(("coolantTempF",), _c_from_f, lo=-50, hi=200),
        "tyre_drift_pct": FieldMap(("tyrePressureDrift",), lo=0, hi=100),
        "oil_life_pct":   FieldMap(("oilLifeRemaining",), lo=0, hi=100),
        "brake_wear_pct": FieldMap(("brakePadWear",), lo=0, hi=100),
        "evt":            FieldMap(("eventType",)),
        "dtc":            FieldMap(("faultCodes",)),
    },
    "mahindra_v3": {
        "vin":            FieldMap(("identity", "vin"), required=True),
        "ts":             FieldMap(("time", "epoch_s"), required=True),
        "seq":            FieldMap(("identity", "seq"), required=True),
        "lat":            FieldMap(("telemetry", "geo", 0), required=True, lo=-90, hi=90),
        "lon":            FieldMap(("telemetry", "geo", 1), required=True, lo=-180, hi=180),
        "speed_kmh":      FieldMap(("telemetry", "velocity_kmph"), required=True, lo=0, hi=300),
        "odo_km":         FieldMap(("telemetry", "distance_km"), lo=0, hi=5_000_000),
        # 0-1 fraction, not a percentage. The classic off-by-100.
        "soc_pct":        FieldMap(("telemetry", "energy", "soc_frac"), _pct_from_fraction, lo=0, hi=100),
        "soh_pct":        FieldMap(("telemetry", "energy", "soh_frac"), _pct_from_fraction, lo=0, hi=100),
        "pack_temp_c":    FieldMap(("telemetry", "energy", "pack_temp_c"), lo=-50, hi=150),
        "fuel_pct":       FieldMap(("telemetry", "energy", "fuel_frac"), _pct_from_fraction, lo=0, hi=100),
        "aux_battery_v":  FieldMap(("telemetry", "health", "aux_volts"), lo=0, hi=36),
        "coolant_temp_c": FieldMap(("telemetry", "health", "coolant_c"), lo=-50, hi=200),
        "tyre_drift_pct": FieldMap(("telemetry", "health", "tyre_drift_pct"), lo=0, hi=100),
        "oil_life_pct":   FieldMap(("telemetry", "health", "oil_life_pct"), lo=0, hi=100),
        "brake_wear_pct": FieldMap(("telemetry", "health", "brake_wear_pct"), lo=0, hi=100),
        "evt":            FieldMap(("diagnostics", "event")),
        "dtc":            FieldMap(("diagnostics", "codes")),
    },
}


def normalise(payload: Mapping[str, Any], dialect: str) -> dict[str, Any]:
    """Map one OEM envelope onto the canonical schema.

    Raises NormalisationError rather than guessing. The caller routes the raw envelope to the DLQ
    and the Mongo envelope store, so it can be replayed once the mapping is corrected -- which is
    the whole point of keeping un-normalised payloads.

    Time O(number of fields). Space O(1).
    """
    spec = DIALECTS.get(dialect)
    if spec is None:
        raise NormalisationError("unknown_dialect", dialect)

    out: dict[str, Any] = {}
    for name, fm in spec.items():
        raw = _dig(payload, fm.path)

        if raw is None:
            if fm.required:
                raise NormalisationError("missing_required_field", name)
            out[name] = None
            continue

        try:
            value = fm.transform(raw) if fm.transform else raw
        except (TypeError, ValueError) as exc:
            raise NormalisationError("transform_failed", f"{name}: {exc}") from exc

        if fm.lo is not None and value is not None:
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise NormalisationError("non_numeric", name) from exc
            if not (fm.lo <= numeric <= (fm.hi if fm.hi is not None else float("inf"))):
                # Out of physical range means the mapping is wrong, not that the vehicle is
                # unusual. Ingesting it would corrupt every downstream aggregate silently.
                raise NormalisationError("out_of_range", f"{name}={numeric}")

        out[name] = value

    out["dtc"] = tuple(out.get("dtc") or ())
    out["evt"] = out.get("evt") or "TICK"
    out["dialect"] = dialect
    return out


def shadow_compare(
    payload: Mapping[str, Any], current: str, candidate: str
) -> dict[str, tuple[Any, Any]]:
    """Run a candidate mapping beside the live one and report every field that differs.

    This is how a new or changed dialect is promoted: it processes live traffic in shadow mode,
    and is only promoted when the diff is empty or every difference is explained. Deploying a
    mapping straight to production is how a fleet silently loses a day of telemetry.
    """
    try:
        a = normalise(payload, current)
    except NormalisationError as exc:
        a = {"__error__": exc.reason}
    try:
        b = normalise(payload, candidate)
    except NormalisationError as exc:
        b = {"__error__": exc.reason}

    diffs: dict[str, tuple[Any, Any]] = {}
    for key in set(a) | set(b):
        if a.get(key) != b.get(key) and key != "dialect":
            diffs[key] = (a.get(key), b.get(key))
    return diffs
