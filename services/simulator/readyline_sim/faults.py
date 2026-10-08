"""Nine injectable fault mechanisms, and the ground-truth ledger they write.

This module is the reason the acceptance tests can assert anything at all. The simulator knows
exactly which vehicle began degrading, when, by which mechanism, and what the failure would have
cost. The platform never sees any of that — it sees only noisy observables. Comparing the two
afterwards is what produces precision@12 and recall@72h.

Two rules keep this honest and must not be relaxed:

  1. **The ledger is write-only from the simulator and read-only from tests.** If production code
     ever reads it, every metric becomes circular and meaningless.
  2. **Four of the nine mechanisms are deliberately invisible in telemetry.** A tyre puncture, a
     collision, fuel theft and a random electronic failure produce no detectable precursor. They
     exist so that recall is bounded below 1.0 by physics rather than by our cleverness — a
     simulator in which everything is predictable would let us claim a recall we could never
     reproduce on real vehicles.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator

from .fleet import Vehicle


class Mechanism(str, Enum):
    # --- telemetry-visible: a precursor exists and a competent detector should find it
    AUX_BATTERY_DECAY = "AUX_BATTERY_DECAY"
    COOLANT_LEAK = "COOLANT_LEAK"
    BATTERY_SOH_CLIFF = "BATTERY_SOH_CLIFF"
    PACK_THERMAL = "PACK_THERMAL"
    BRAKE_WEAR = "BRAKE_WEAR"
    # --- telemetry-invisible: no precursor. These bound achievable recall.
    TYRE_PUNCTURE = "TYRE_PUNCTURE"
    COLLISION = "COLLISION"
    FUEL_THEFT = "FUEL_THEFT"
    RANDOM_ELECTRONIC = "RANDOM_ELECTRONIC"

    @property
    def is_visible(self) -> bool:
        return self in _VISIBLE


_VISIBLE = frozenset(
    {
        Mechanism.AUX_BATTERY_DECAY,
        Mechanism.COOLANT_LEAK,
        Mechanism.BATTERY_SOH_CLIFF,
        Mechanism.PACK_THERMAL,
        Mechanism.BRAKE_WEAR,
    }
)

# Annual probability per vehicle. Sums to ~1.8 events/vehicle-year, matching the assumption the
# impact model is built on (fleet-maintenance surveys cluster 1.4-2.6 for urban LCV duty).
ANNUAL_RATE: dict[Mechanism, float] = {
    Mechanism.AUX_BATTERY_DECAY: 0.32,
    Mechanism.COOLANT_LEAK: 0.14,
    Mechanism.BATTERY_SOH_CLIFF: 0.09,
    Mechanism.PACK_THERMAL: 0.04,
    Mechanism.BRAKE_WEAR: 0.26,
    Mechanism.TYRE_PUNCTURE: 0.44,
    Mechanism.COLLISION: 0.07,
    Mechanism.FUEL_THEFT: 0.11,
    Mechanism.RANDOM_ELECTRONIC: 0.33,
}

# How long the precursor is detectable before failure, in hours. Recall@72h is only achievable
# for mechanisms whose lead time exceeds 72 h.
LEAD_TIME_HOURS: dict[Mechanism, tuple[float, float]] = {
    Mechanism.AUX_BATTERY_DECAY: (168.0, 480.0),   # 7-20 days of falling resting voltage
    Mechanism.COOLANT_LEAK: (48.0, 240.0),
    Mechanism.BATTERY_SOH_CLIFF: (336.0, 1440.0),  # slow, very visible
    Mechanism.PACK_THERMAL: (6.0, 72.0),           # fast; partly inside the 72 h window
    Mechanism.BRAKE_WEAR: (240.0, 1200.0),
}

# Cost of the event if it happens unplanned, versus the same work done planned.
# Difference is the avoided cost the ledger attributes to a successful prediction.
COST_UNPLANNED: dict[Mechanism, float] = {
    Mechanism.AUX_BATTERY_DECAY: 1_250.0,
    Mechanism.COOLANT_LEAK: 1_900.0,
    Mechanism.BATTERY_SOH_CLIFF: 3_400.0,
    Mechanism.PACK_THERMAL: 5_200.0,
    Mechanism.BRAKE_WEAR: 1_450.0,
    Mechanism.TYRE_PUNCTURE: 410.0,
    Mechanism.COLLISION: 4_800.0,
    Mechanism.FUEL_THEFT: 260.0,
    Mechanism.RANDOM_ELECTRONIC: 980.0,
}

COST_PLANNED: dict[Mechanism, float] = {
    Mechanism.AUX_BATTERY_DECAY: 340.0,
    Mechanism.COOLANT_LEAK: 620.0,
    Mechanism.BATTERY_SOH_CLIFF: 2_100.0,
    Mechanism.PACK_THERMAL: 1_400.0,
    Mechanism.BRAKE_WEAR: 480.0,
    Mechanism.TYRE_PUNCTURE: 410.0,     # cannot be planned; no saving available
    Mechanism.COLLISION: 4_800.0,
    Mechanism.FUEL_THEFT: 260.0,
    Mechanism.RANDOM_ELECTRONIC: 980.0,
}


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One ground-truth fault. Written by the simulator; read only by tests and back-tests."""

    vin: str
    tenant_id: int
    mechanism: Mechanism
    onset_ts: float      # when degradation began, i.e. the earliest a detector could fire
    failure_ts: float    # when the vehicle actually fails
    visible: bool
    cost_unplanned: float
    cost_planned: float

    @property
    def avoidable_cost(self) -> float:
        return round(self.cost_unplanned - self.cost_planned, 2)

    @property
    def lead_time_hours(self) -> float:
        return (self.failure_ts - self.onset_ts) / 3600.0


@dataclass(slots=True)
class GroundTruthLedger:
    """The oracle. Never exposed through any API."""

    entries: list[LedgerEntry] = field(default_factory=list)

    def add(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

    def by_vin(self) -> dict[str, list[LedgerEntry]]:
        out: dict[str, list[LedgerEntry]] = {}
        for e in self.entries:
            out.setdefault(e.vin, []).append(e)
        return out

    def visible_failures_in(self, start_ts: float, end_ts: float) -> list[LedgerEntry]:
        """Failures a detector could in principle have caught in this window."""
        return [
            e for e in self.entries
            if e.visible and start_ts <= e.failure_ts <= end_ts
        ]

    @property
    def total_avoidable_cost(self) -> float:
        return round(sum(e.avoidable_cost for e in self.entries), 2)

    def summary(self) -> dict[str, object]:
        visible = sum(1 for e in self.entries if e.visible)
        return {
            "total_faults": len(self.entries),
            "visible": visible,
            "invisible": len(self.entries) - visible,
            "visible_fraction": round(visible / len(self.entries), 4) if self.entries else 0.0,
            "total_avoidable_cost": self.total_avoidable_cost,
        }


def schedule_faults(
    vehicles: Iterator[Vehicle],
    *,
    horizon_days: float,
    start_ts: float,
    seed: int = 20261008,
) -> tuple[GroundTruthLedger, dict[str, list[LedgerEntry]]]:
    """Decide, up front, which vehicles fail and when.

    Scheduling faults in advance rather than rolling dice per tick is deliberate: it gives the
    telemetry generator a known onset time to ramp degradation towards, and it means the ledger
    is complete before a single event is emitted, so a test can be written against it without
    running the stream.

    Time O(V * M). Space O(number of faults).
    """
    rng = random.Random(seed + 3)
    ledger = GroundTruthLedger()
    horizon_seconds = horizon_days * 86_400.0
    years = horizon_days / 365.0

    for vehicle in vehicles:
        for mechanism, annual in ANNUAL_RATE.items():
            if not _applies(mechanism, vehicle):
                continue
            # Poisson thinning: probability of at least one event in the horizon.
            p = 1.0 - pow(2.718281828459045, -annual * years * _risk_multiplier(mechanism, vehicle))
            if rng.random() >= p:
                continue
            failure_ts = start_ts + rng.uniform(0.0, horizon_seconds)
            if mechanism.is_visible:
                lo, hi = LEAD_TIME_HOURS[mechanism]
                onset_ts = failure_ts - rng.uniform(lo, hi) * 3600.0
            else:
                onset_ts = failure_ts  # no precursor at all
            ledger.add(
                LedgerEntry(
                    vin=vehicle.vin,
                    tenant_id=vehicle.tenant_id,
                    mechanism=mechanism,
                    onset_ts=onset_ts,
                    failure_ts=failure_ts,
                    visible=mechanism.is_visible,
                    cost_unplanned=COST_UNPLANNED[mechanism],
                    cost_planned=COST_PLANNED[mechanism],
                )
            )
    return ledger, ledger.by_vin()


def _applies(mechanism: Mechanism, vehicle: Vehicle) -> bool:
    """Some mechanisms are powertrain-specific. A diesel van has no traction pack."""
    is_ev = vehicle.model.battery_kwh is not None and vehicle.model.powertrain in ("BEV", "PHEV")
    if mechanism in (Mechanism.BATTERY_SOH_CLIFF, Mechanism.PACK_THERMAL):
        return is_ev
    if mechanism in (Mechanism.COOLANT_LEAK, Mechanism.FUEL_THEFT):
        return vehicle.model.tank_litres is not None
    return True


def _risk_multiplier(mechanism: Mechanism, vehicle: Vehicle) -> float:
    """Older, harder-worked vehicles fail more. Keeps the population non-uniform.

    Without this every vehicle has identical hazard and ranking becomes meaningless — any
    ordering would score the same, and Spearman rho against the ledger would be noise.
    """
    m = 1.0
    if mechanism == Mechanism.AUX_BATTERY_DECAY:
        m *= 1.0 + max(0.0, (12.7 - vehicle.aux_battery_v)) * 3.0
    if mechanism == Mechanism.BRAKE_WEAR:
        m *= 1.0 + vehicle.brake_wear_pct / 50.0
    if mechanism == Mechanism.BATTERY_SOH_CLIFF:
        m *= 1.0 + max(0.0, (90.0 - vehicle.soh_pct)) / 20.0
    m *= 1.0 + vehicle.odo_km / 300_000.0
    return m
