"""ReadyLine API.

Exposes the depot commitment, the Morning Board, and -- the endpoint that matters most --
the randomised-holdout result with its confidence interval.

Every tenant-scoped read goes through `tenant_connection`, so PostgreSQL row-level security is
in force even if a handler forgets its WHERE clause (ADR-0004).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import clickhouse_connect
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from readyline.algorithms.depot import VehicleDemand, plan_depot, unmanaged_plan
from readyline.algorithms.vews import BoardItem, build_board, score
from readyline.experiment import (
    Observation,
    arm_balance,
    assign_arm,
    difference_in_differences,
)

from .deps import SETTINGS, Principal, current_principal, rate_limit, tenant_connection

app = FastAPI(
    title="ReadyLine API",
    version="1.0.0",
    description=(
        "Nightly depot charging commitment and fleet readiness for mixed ICE/EV fleets. "
        "All data is synthetic."
    ),
)

_STARTED = time.time()


def _as_vin(value: object) -> str:
    """Normalise a VIN coming back from either store.

    ClickHouse hands FixedString(17) back as bytes and pads to width; PostgreSQL CHAR(17) pads
    with spaces. Both need the same treatment before they can be used as a dictionary key, and
    mixing the two types silently produces lookups that never match.
    """
    if isinstance(value, (bytes, bytearray)):
        return value.decode("ascii", "replace").strip()
    return str(value).strip()


def _clickhouse():
    return clickhouse_connect.get_client(
        host=SETTINGS.ch_host, port=SETTINGS.ch_port, username=SETTINGS.ch_user,
        password=SETTINGS.ch_password, database=SETTINGS.ch_database,
    )


# ----------------------------------------------------------------- models


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    uptime_s: float
    postgres: bool
    clickhouse: bool


class VehicleOut(BaseModel):
    vin: str
    registration: str
    fleet_id: int
    powertrain: str
    in_service_on: str


class Page(BaseModel):
    items: list[Any]
    next_cursor: str | None = Field(
        default=None,
        description="Opaque keyset cursor. Absent when there are no further pages.",
    )


class PlanRequest(BaseModel):
    """Inputs for tonight's commitment. Tariff is per slot, in currency per kWh."""

    tariff_per_slot: list[float] = Field(min_length=1, max_length=96)
    site_cap_kw: float = Field(gt=0)
    slot_minutes: int = Field(default=30, ge=5, le=120)
    vehicles: list["PlanVehicle"] = Field(min_length=1, max_length=5000)


class PlanVehicle(BaseModel):
    vin: str
    soc_start_pct: float = Field(ge=0, le=100)
    soc_target_pct: float = Field(ge=0, le=100)
    battery_kwh: float = Field(gt=0)
    max_charge_kw: float = Field(gt=0)
    last_slot: int = Field(ge=0)
    depot_id: int = 1


class PlanVehicleOut(BaseModel):
    vin: str
    arm: str
    power_by_slot: list[float]
    final_soc_pct: float
    feasible: bool
    cost: float


class PlanOut(BaseModel):
    total_cost: float
    unmanaged_cost: float
    saving: float
    peak_kw: float
    cap_kw: float
    cap_respected: bool
    lower_bound: float
    duality_gap: float | None = Field(
        description="None when load had to be shed: the bound assumes every vehicle reaches "
                    "target, so it is not comparable to a plan that did not."
    )
    iterations: int
    site_load_by_slot: list[float]
    infeasible_vins: list[str]
    vehicles: list[PlanVehicleOut]


class ExperimentOut(BaseModel):
    metric: str
    n_treatment: int
    n_control: int
    treatment_change: float
    control_change: float
    did_estimate: float
    saving_per_100km: float
    standard_error: float
    t_statistic: float
    degrees_of_freedom: float
    p_value: float
    ci_low: float
    ci_high: float
    significant_at_05: bool
    interpretation: str


# ----------------------------------------------------------------- health


@app.get("/healthz", response_model=Health, tags=["ops"])
def healthz() -> Health:
    """Liveness plus dependency reachability. Unauthenticated by design."""
    pg_ok = ch_ok = False
    try:
        from .deps import get_pool
        with get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            pg_ok = cur.fetchone()[0] == 1
    except Exception:  # noqa: BLE001 - health must never raise
        pg_ok = False
    try:
        ch_ok = _clickhouse().query("SELECT 1").result_rows[0][0] == 1
    except Exception:  # noqa: BLE001
        ch_ok = False

    return Health(
        status="ok" if (pg_ok and ch_ok) else "degraded",
        uptime_s=round(time.time() - _STARTED, 1),
        postgres=pg_ok,
        clickhouse=ch_ok,
    )


# ----------------------------------------------------------------- vehicles


@app.get("/api/v1/vehicles", response_model=Page, tags=["fleet"])
def list_vehicles(
    principal: Annotated[Principal, Depends(current_principal)],
    _: Annotated[None, Depends(rate_limit)],
    limit: int = Query(default=50, ge=1, le=SETTINGS.max_page_size),
    cursor: str | None = Query(default=None, description="VIN to resume after"),
) -> Page:
    """Keyset pagination, not OFFSET.

    OFFSET re-scans and discards every skipped row, so page 10,000 of a 100k-vehicle fleet costs
    100x page 1. Keyset seeks straight to the position using the primary-key index and is flat
    in page depth.
    """
    with tenant_connection(principal) as conn, conn.cursor() as cur:
        if cursor:
            cur.execute(
                "SELECT v.vin, v.registration, v.fleet_id, m.powertrain, v.in_service_on"
                " FROM vehicle v JOIN vehicle_model m USING (model_id)"
                " WHERE v.vin > %s ORDER BY v.vin LIMIT %s",
                (cursor, limit + 1),
            )
        else:
            cur.execute(
                "SELECT v.vin, v.registration, v.fleet_id, m.powertrain, v.in_service_on"
                " FROM vehicle v JOIN vehicle_model m USING (model_id)"
                " ORDER BY v.vin LIMIT %s",
                (limit + 1,),
            )
        rows = cur.fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        VehicleOut(
            vin=r[0].strip(), registration=r[1], fleet_id=r[2],
            powertrain=r[3], in_service_on=str(r[4]),
        ).model_dump()
        for r in rows
    ]
    return Page(items=items, next_cursor=items[-1]["vin"] if (has_more and items) else None)


@app.get("/api/v1/vehicles/{vin}/telemetry", tags=["fleet"])
def vehicle_telemetry(
    vin: str,
    principal: Annotated[Principal, Depends(current_principal)],
    _: Annotated[None, Depends(rate_limit)],
    minutes: int = Query(default=60, ge=1, le=1440),
) -> dict[str, Any]:
    """Recent telemetry from the 1-minute rollup, never from raw.

    Dashboards read the materialised view: scanning raw for a 24 h window is ~86,400 rows per
    vehicle against ~1,440 pre-aggregated ones.
    """
    # Tenant check happens in PostgreSQL first: ClickHouse has no row-level security, so the
    # authoritative "do you own this VIN" answer must come from the CP store.
    with tenant_connection(principal) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM vehicle WHERE vin = %s", (vin,))
        if cur.fetchone() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "vehicle not found")

    client = _clickhouse()
    rows = client.query(
        """
        SELECT bucket,
               countMerge(samples)        AS samples,
               round(avgMerge(avg_speed), 2) AS avg_speed,
               maxMerge(max_speed)        AS max_speed,
               argMaxMerge(last_soc)      AS last_soc
        FROM telemetry_1m
        WHERE vin = {vin:String} AND bucket >= now() - INTERVAL {mins:UInt32} MINUTE
        GROUP BY bucket ORDER BY bucket
        """,
        parameters={"vin": vin, "mins": minutes},
    ).result_rows

    return {
        "vin": vin,
        "minutes": minutes,
        "points": [
            {"bucket": str(r[0]), "samples": r[1], "avg_speed": r[2],
             "max_speed": r[3], "soc_pct": r[4]}
            for r in rows
        ],
    }


# ----------------------------------------------------------------- the depot commitment


@app.post("/api/v1/depot/plan", response_model=PlanOut, tags=["charging"])
def commit_depot_plan(
    body: PlanRequest,
    principal: Annotated[Principal, Depends(current_principal)],
    _: Annotated[None, Depends(rate_limit)],
) -> PlanOut:
    """Compute tonight's charging commitment under the site power cap (A0).

    Control-arm vehicles are excluded from optimisation and charge unmanaged, but their load is
    reserved against the cap because they physically draw it.
    """
    principal.require("DISPATCHER")

    demands = [
        VehicleDemand(
            vin=v.vin,
            soc_start_pct=v.soc_start_pct,
            soc_target_pct=v.soc_target_pct,
            battery_kwh=v.battery_kwh,
            max_charge_kw=v.max_charge_kw,
            last_slot=min(v.last_slot, len(body.tariff_per_slot) - 1),
            is_control=assign_arm(v.vin, v.depot_id) == "control",
        )
        for v in body.vehicles
    ]

    plan = plan_depot(
        demands, body.tariff_per_slot, body.site_cap_kw, slot_minutes=body.slot_minutes
    )

    # The counterfactual, computed on the same inputs so the saving is comparable.
    unmanaged_total = sum(
        unmanaged_plan(d, body.tariff_per_slot, body.slot_minutes).cost for d in demands
    )

    arms = {d.vin: ("control" if d.is_control else "treatment") for d in demands}
    return PlanOut(
        total_cost=plan.total_cost,
        unmanaged_cost=round(unmanaged_total, 4),
        saving=round(unmanaged_total - plan.total_cost, 4),
        peak_kw=plan.peak_kw,
        cap_kw=plan.cap_kw,
        cap_respected=plan.cap_respected,
        lower_bound=plan.lower_bound,
        duality_gap=plan.duality_gap,
        iterations=plan.iterations,
        site_load_by_slot=list(plan.site_load_by_slot),
        infeasible_vins=list(plan.infeasible_vins),
        vehicles=[
            PlanVehicleOut(
                vin=p.vin, arm=arms.get(p.vin, "treatment"),
                power_by_slot=list(p.power_by_slot),
                final_soc_pct=p.final_soc_pct, feasible=p.feasible, cost=p.cost,
            )
            for p in plan.plans
        ],
    )


# ----------------------------------------------------------------- the holdout result


@app.get(
    "/api/v1/experiments/charging-arbitrage",
    response_model=ExperimentOut,
    tags=["experiment"],
)
def charging_arbitrage(
    principal: Annotated[Principal, Depends(current_principal)],
    _: Annotated[None, Depends(rate_limit)],
    days: int = Query(default=30, ge=7, le=365),
) -> ExperimentOut:
    """The measured saving, with its confidence interval.

    This is the platform's headline claim and the only one that synthetic data cannot
    invalidate: both arms run against the same simulator, so generator bias cancels in the
    difference. A point estimate without this interval would be an assertion, not a measurement.
    """
    client = _clickhouse()
    rows = client.query(
        """
        SELECT vin,
               if(started_at >= now() - INTERVAL {half:UInt32} DAY, 'post', 'pre') AS period,
               sum(cost_inr)                       AS cost,
               sum(greatest(soc_end - soc_start, 0)) AS soc_delta
        FROM charge_session
        WHERE started_at >= now() - INTERVAL {days:UInt32} DAY
        GROUP BY vin, period
        """,
        parameters={"days": days, "half": days // 2},
    ).result_rows

    if not rows:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no charging sessions in the window; run the simulator and ingest first",
        )

    # Depot is needed for stratified arm assignment.
    with tenant_connection(principal) as conn, conn.cursor() as cur:
        cur.execute("SELECT vin, fleet_id FROM vehicle")
        depot_of = {r[0].strip(): r[1] for r in cur.fetchall()}

    observations: list[Observation] = []
    assignments: dict[str, tuple[int, str]] = {}
    for raw_vin, period, cost, soc_delta in rows:
        # ClickHouse returns FixedString(17) as BYTES, not str. Without decoding, every lookup
        # against the Postgres-derived map missed, every observation was dropped, and the
        # endpoint reported an empty control arm -- correct refusal, wrong cause.
        vin = _as_vin(raw_vin)
        depot = depot_of.get(vin)
        if depot is None:
            continue  # not this tenant's vehicle; RLS already excluded it
        arm = assign_arm(vin, depot)
        assignments[vin] = (depot, arm)
        # SoC delta stands in for route distance: energy actually put into the pack.
        observations.append(
            Observation(vin=vin, arm=arm, period=period, cost=float(cost),
                        route_km=float(soc_delta))
        )

    result = difference_in_differences(observations)
    balance = arm_balance(assignments)
    empty = [d for d, counts in balance.items() if counts["control"] == 0]

    if result.n_control == 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "control arm is empty; the experiment cannot be estimated",
        )

    if result.significant_at_05:
        verdict = (
            f"Treatment reduced cost by {result.saving_per_100km:.2f} per 100 units of energy "
            f"(95% CI {-result.ci_high:.2f} to {-result.ci_low:.2f}, p={result.p_value:.4f})."
        )
    else:
        verdict = (
            f"No statistically significant effect yet (p={result.p_value:.4f}). "
            f"Point estimate {result.saving_per_100km:+.2f}; the interval includes zero."
        )
    if empty:
        verdict += f" Warning: {len(empty)} depot(s) have an empty control arm."

    return ExperimentOut(
        metric="energy cost per 100 units of charge delivered",
        n_treatment=result.n_treatment,
        n_control=result.n_control,
        treatment_change=result.treatment_change,
        control_change=result.control_change,
        did_estimate=result.did_estimate,
        saving_per_100km=result.saving_per_100km,
        standard_error=result.standard_error,
        t_statistic=result.t_statistic,
        degrees_of_freedom=result.degrees_of_freedom,
        p_value=result.p_value,
        ci_low=result.ci_low,
        ci_high=result.ci_high,
        significant_at_05=result.significant_at_05,
        interpretation=verdict,
    )


# ----------------------------------------------------------------- the morning board


@app.get("/api/v1/board", tags=["board"])
def morning_board(
    principal: Annotated[Principal, Depends(current_principal)],
    _: Annotated[None, Depends(rate_limit)],
    response: Response = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Exceptions to the plan we committed last night. Hard-capped at 12.

    The cap is the product, not a pagination default: a 13th card recreates the 40-emails-a-day
    portal the user already muted. Overflow is reported as a count.
    """
    with tenant_connection(principal) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.vin, a.rule_code, a.severity, a.vews_score, a.dollars_at_risk,
                   a.evidence, a.raised_at
            FROM alert a
            WHERE a.state = 'OPEN'
            ORDER BY a.severity, a.dollars_at_risk DESC, a.raised_at DESC
            LIMIT 200
            """
        )
        rows = cur.fetchall()

    candidates = [
        BoardItem(
            vin=r[0].strip(),
            result=score(dict((r[5] or {}).get("signals", {}))),
            dollars_at_risk=float(r[4]),
        )
        for r in rows
    ]
    board = build_board(candidates)
    by_vin = {r[0].strip(): r for r in rows}

    return {
        "items": [
            {
                "vin": item.vin,
                "rule_code": by_vin[item.vin][1],
                "severity": by_vin[item.vin][2],
                "vews_score": by_vin[item.vin][3],
                "dollars_at_risk": item.dollars_at_risk,
                "evidence": by_vin[item.vin][5],
                "raised_at": by_vin[item.vin][6].isoformat(),
                "triggered_by": list(item.result.triggered_by),
            }
            for item in board.items
            if item.vin in by_vin
        ],
        "suppressed_count": board.suppressed_count,
        "cap": 12,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
