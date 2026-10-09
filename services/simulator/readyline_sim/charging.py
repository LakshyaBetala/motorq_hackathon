"""Run the charging experiment: both arms, many nights, into ClickHouse `charge_session`.

This is what turns the holdout from a design claim into a measured number. Every night, for every
EV in the depot:

  * **Treatment** vehicles are scheduled by A0 under the site power cap.
  * **Control** vehicles charge unmanaged -- plug in on arrival, draw full power -- and their
    load is reserved against the cap before the optimiser runs, because they physically draw it.

Both arms are simulated by the *same* code against the *same* tariffs, arrival SoCs and
deadlines. The only difference is whether the plan is applied. That is the whole point: any bias
in how we model charging applies identically to both arms and cancels in the difference.

The pre-period is deliberately generated with **both arms unmanaged**. Difference-in-differences
needs a baseline in which the two groups behave the same; without it we could not distinguish a
treatment effect from a pre-existing difference between the groups.
"""

from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from readyline.algorithms.depot import (  # noqa: E402
    VehicleDemand,
    plan_depot,
    unmanaged_plan,
)
from readyline.experiment import assign_arm  # noqa: E402

from .fleet import generate_depots, generate_vehicles, plan_tenants  # noqa: E402

# A realistic Indian commercial time-of-use tariff, INR/kWh, in 30-minute slots from 18:00.
# Evening peak, cheap overnight trough, morning shoulder -- the shape that makes the whole
# optimisation worth doing.
TARIFF_30MIN: tuple[float, ...] = (
    10.8, 10.8, 10.8, 10.8,   # 18:00-20:00 evening peak
    9.4, 9.4, 7.6, 7.6,       # 20:00-22:00 shoulder
    5.2, 5.2, 4.1, 4.1,       # 22:00-00:00
    3.4, 3.4, 3.4, 3.4,       # 00:00-02:00 trough
    3.4, 3.4, 3.8, 3.8,       # 02:00-04:00
    4.6, 4.6, 6.2, 6.2,       # 04:00-06:00 morning shoulder
)
SLOT_MINUTES = 30


@dataclass(frozen=True, slots=True)
class SessionRow:
    fleet_id: int
    vin: str
    started_at: datetime
    ended_at: datetime
    soc_start: float
    soc_end: float
    kwh: float
    tariff_inr: float
    cost_inr: float
    was_optimised: int
    # Carried explicitly so the energy-conservation check has an INDEPENDENT third quantity.
    # Deriving the pack size back out of kwh and the SoC delta makes the check tautological:
    # it then reproduces the efficiency constant by construction and can never fail.
    battery_kwh: float


def _night_demands(
    evs: list, night: int, seed: int, treat: bool
) -> list[VehicleDemand]:
    """Arrival SoC and departure deadline for one night.

    Seeded per (vin, night) so the *same* vehicle sees the *same* conditions in both arms and
    across periods. If arrival SoC were redrawn independently, arm differences would be swamped
    by noise we introduced ourselves.
    """
    demands = []
    for v in evs:
        rng = random.Random(f"{seed}:{v.vin}:{night}")
        soc_start = rng.uniform(12.0, 55.0)
        # Most vehicles leave at the end of the window; a minority depart early.
        last_slot = len(TARIFF_30MIN) - 1 if rng.random() > 0.18 else rng.randint(12, 19)
        demands.append(
            VehicleDemand(
                vin=v.vin,
                soc_start_pct=soc_start,
                soc_target_pct=85.0,
                battery_kwh=float(v.model.battery_kwh or 40.0),
                max_charge_kw=float(v.model.max_charge_kw or 11.0),
                last_slot=last_slot,
                is_control=not treat,
            )
        )
    return demands


def _to_sessions(
    plans, demands_by_vin, fleet_id: int, night_start: datetime, optimised: bool
) -> list[SessionRow]:
    slot_hours = SLOT_MINUTES / 60.0
    rows: list[SessionRow] = []
    for p in plans:
        d = demands_by_vin.get(p.vin)
        if d is None:
            continue
        active = [i for i, kw in enumerate(p.power_by_slot) if kw > 1e-9]
        if not active:
            continue
        grid_kwh = sum(kw * slot_hours for kw in p.power_by_slot)
        cost = sum(
            kw * slot_hours * TARIFF_30MIN[i] for i, kw in enumerate(p.power_by_slot)
        )
        rows.append(
            SessionRow(
                fleet_id=fleet_id,
                vin=p.vin,
                started_at=night_start + timedelta(minutes=SLOT_MINUTES * active[0]),
                ended_at=night_start + timedelta(minutes=SLOT_MINUTES * (active[-1] + 1)),
                soc_start=round(d.soc_start_pct, 2),
                soc_end=round(p.final_soc_pct, 2),
                kwh=round(grid_kwh, 4),
                tariff_inr=round(cost / grid_kwh, 4) if grid_kwh > 0 else 0.0,
                cost_inr=round(cost, 4),
                was_optimised=1 if (optimised and not p.is_control) else 0,
                battery_kwh=d.battery_kwh,
            )
        )
    return rows


def simulate(
    *,
    nights: int = 30,
    evs_per_depot: int = 60,
    depots: int = 2,
    cap_kw: float = 220.0,
    seed: int = 20261008,
    tenant_vehicles: int = 100_000,
    tenants: int = 60,
) -> list[SessionRow]:
    """Generate `nights` of sessions: first half pre-period, second half post-period.

    Pre-period: every vehicle unmanaged, both arms alike -- this is the DiD baseline.
    Post-period: treatment vehicles follow the committed plan, control vehicles do not.
    """
    tenant_list = plan_tenants(tenant_vehicles, tenants, seed=seed)
    depot_list = generate_depots(tenant_list, seed=seed)
    all_vehicles = generate_vehicles(tenant_list, depot_list, seed=seed)

    # Take EVs from the first tenant only, so everything lands under one tenant scope.
    #
    # Pick the target depots FIRST, then fill only those. Filling every depot of the tenant and
    # taking the first two afterwards spread the vehicles thin: the loop stopped as soon as the
    # overall count was reached, leaving ~15 per depot instead of 60, and the experiment ran on
    # a third of the intended fleet.
    target_depots = [d.depot_id for d in depot_list if d.tenant_id == 1][:depots]
    if not target_depots:
        raise RuntimeError("tenant 1 has no depots; cannot run the experiment")

    evs_by_depot: dict[int, list] = {d: [] for d in target_depots}
    for v in all_vehicles:
        if v.tenant_id != 1 or v.model.battery_kwh is None:
            continue
        bucket = evs_by_depot.get(v.depot_id)
        if bucket is None or len(bucket) >= evs_per_depot:
            continue
        bucket.append(v)
        if all(len(b) >= evs_per_depot for b in evs_by_depot.values()):
            break

    chosen = {d: evs for d, evs in evs_by_depot.items() if evs}
    rows: list[SessionRow] = []
    base = datetime.now(timezone.utc).replace(
        hour=18, minute=0, second=0, microsecond=0
    ) - timedelta(days=nights)
    half = nights // 2

    for night in range(nights):
        night_start = base + timedelta(days=night)
        is_post = night >= half

        for depot_id, evs in chosen.items():
            if not evs:
                continue
            demands = _night_demands(evs, night, seed, treat=is_post)

            if not is_post:
                # Baseline period: nobody is optimised.
                plans = [unmanaged_plan(d, TARIFF_30MIN, SLOT_MINUTES) for d in demands]
                rows.extend(
                    _to_sessions(plans, {d.vin: d for d in demands}, depot_id,
                                 night_start, optimised=False)
                )
                continue

            # Post period: arms diverge. Arm membership is the pre-registered hash.
            scoped = [
                VehicleDemand(
                    vin=d.vin, soc_start_pct=d.soc_start_pct, soc_target_pct=d.soc_target_pct,
                    battery_kwh=d.battery_kwh, max_charge_kw=d.max_charge_kw,
                    last_slot=d.last_slot,
                    is_control=assign_arm(d.vin, depot_id) == "control",
                )
                for d in demands
            ]
            plan = plan_depot(scoped, TARIFF_30MIN, cap_kw, slot_minutes=SLOT_MINUTES)
            rows.extend(
                _to_sessions(plan.plans, {d.vin: d for d in scoped}, depot_id,
                             night_start, optimised=True)
            )

    return rows


def write_clickhouse(rows: list[SessionRow]) -> int:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "voltroute"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "devonly"),
        database=os.environ.get("CLICKHOUSE_DB", "voltroute"),
    )
    client.insert(
        table="charge_session",
        data=[
            [r.fleet_id, r.vin, r.started_at, r.ended_at, r.soc_start, r.soc_end,
             r.kwh, r.tariff_inr, r.cost_inr, None, r.was_optimised]
            for r in rows
        ],
        column_names=[
            "fleet_id", "vin", "started_at", "ended_at", "soc_start", "soc_end",
            "kwh", "tariff_inr", "cost_inr", "charger_id", "was_optimised",
        ],
    )
    return len(rows)


if __name__ == "__main__":
    nights = int(os.environ.get("SIM_NIGHTS", "30"))
    sessions = simulate(nights=nights)
    written = write_clickhouse(sessions)
    optimised = sum(1 for r in sessions if r.was_optimised)
    print(
        f"wrote {written} charge sessions over {nights} nights "
        f"({optimised} optimised, {written - optimised} unmanaged)"
    )
