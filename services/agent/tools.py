"""Tools the copilot may call.

Every read tool takes `tenant_id` as its first argument, injected by the agent loop from the
authenticated principal rather than from the model. Each one opens a connection scoped by
`app.tenant_id`, so PostgreSQL row-level security is the backstop even if a tool forgets a WHERE
clause (ADR-0004).

Write tools carry `effect="write"` and are never executed by the loop -- they exist so the model
can *propose* a concrete, structured change that a dispatcher approves.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

import psycopg  # noqa: E402

from copilot import Tool  # noqa: E402

DSN = os.environ.get(
    "READYLINE_PG_APP_DSN",
    "postgresql://readyline_app:devonly@127.0.0.1:5432/voltroute",
)


def _scoped(tenant_id: int):
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))
    return conn


# ----------------------------------------------------------------- read tools


def fleet_summary(*, tenant_id: int) -> dict[str, Any]:
    with _scoped(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT m.powertrain, count(*) FROM vehicle v"
            " JOIN vehicle_model m USING (model_id) GROUP BY m.powertrain ORDER BY 2 DESC"
        )
        by_powertrain = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT count(*) FROM depot")
        depots = cur.fetchone()[0]
    return {
        "vehicles_total": sum(by_powertrain.values()),
        "by_powertrain": by_powertrain,
        "depots": depots,
    }


def list_depots(*, tenant_id: int) -> list[dict[str, Any]]:
    with _scoped(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT d.depot_id, d.name, count(v.vin) FROM depot d"
            " LEFT JOIN vehicle v ON v.fleet_id = d.depot_id"
            " GROUP BY d.depot_id, d.name ORDER BY count(v.vin) DESC LIMIT 25"
        )
        return [{"depot_id": r[0], "name": r[1], "vehicles": r[2]} for r in cur.fetchall()]


def vehicle_detail(*, tenant_id: int, vin: str) -> dict[str, Any]:
    with _scoped(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT v.vin, v.registration, m.name, m.powertrain, m.battery_kwh,"
            " m.max_charge_kw, m.wltp_range_km, d.name"
            " FROM vehicle v JOIN vehicle_model m USING (model_id)"
            " LEFT JOIN depot d ON d.depot_id = v.fleet_id WHERE v.vin = %s",
            (vin.strip().upper(),),
        )
        row = cur.fetchone()
    if row is None:
        return {"found": False, "vin": vin}
    return {
        "found": True, "vin": row[0].strip(), "registration": row[1], "model": row[2],
        "powertrain": row[3], "battery_kwh": float(row[4]) if row[4] else None,
        "max_charge_kw": float(row[5]) if row[5] else None,
        "wltp_range_km": float(row[6]) if row[6] else None, "depot": row[7],
    }


def open_alerts(*, tenant_id: int, limit: int = 12) -> list[dict[str, Any]]:
    """The Morning Board, capped the same way the UI caps it."""
    with _scoped(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT vin, rule_code, severity, vews_score, dollars_at_risk, raised_at"
            " FROM alert WHERE state = 'OPEN'"
            " ORDER BY severity, dollars_at_risk DESC LIMIT %s",
            (min(int(limit), 12),),
        )
        return [
            {"vin": r[0].strip(), "rule": r[1], "severity": r[2], "vews": r[3],
             "dollars_at_risk": float(r[4]), "raised_at": r[5].isoformat()}
            for r in cur.fetchall()
        ]


def tariff_bands(*, tenant_id: int) -> list[dict[str, Any]]:
    with _scoped(tenant_id) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, start_minute, end_minute, price_per_kwh FROM tariff_band"
            " ORDER BY start_minute"
        )
        return [
            {"band": r[0], "from": f"{r[1]//60:02d}:{r[1]%60:02d}",
             "to": f"{r[2]//60:02d}:{r[2]%60:02d}", "price_per_kwh": float(r[3])}
            for r in cur.fetchall()
        ]


def charging_saving(*, tenant_id: int, days: int = 30) -> dict[str, Any]:
    """The measured holdout result, so the agent quotes the real number with its interval."""
    import clickhouse_connect

    from readyline.experiment import Observation, assign_arm, difference_in_differences

    ch = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "voltroute"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "devonly"),
        database=os.environ.get("CLICKHOUSE_DB", "voltroute"),
    )
    rows = ch.query(
        "SELECT vin, if(started_at >= now() - INTERVAL {h:UInt32} DAY,'post','pre') AS period,"
        " sum(cost_inr), sum(greatest(soc_end - soc_start, 0))"
        " FROM charge_session WHERE started_at >= now() - INTERVAL {d:UInt32} DAY"
        " GROUP BY vin, period",
        parameters={"d": days, "h": days // 2},
    ).result_rows
    if not rows:
        return {"available": False, "reason": "no charging sessions in the window"}

    with _scoped(tenant_id) as conn, conn.cursor() as cur:
        cur.execute("SELECT vin, fleet_id FROM vehicle")
        depot_of = {r[0].strip(): r[1] for r in cur.fetchall()}

    obs = []
    for raw_vin, period, cost, delta in rows:
        vin = raw_vin.decode().strip() if isinstance(raw_vin, (bytes, bytearray)) else str(raw_vin).strip()
        depot = depot_of.get(vin)
        if depot is None:
            continue
        obs.append(Observation(vin, assign_arm(vin, depot), period, float(cost), float(delta)))

    if not obs:
        return {"available": False, "reason": "no sessions matched this tenant's vehicles"}

    r = difference_in_differences(obs)
    if r.n_control == 0:
        return {"available": False, "reason": "control arm empty"}
    return {
        "available": True,
        "treatment_vehicles": r.n_treatment,
        "control_vehicles": r.n_control,
        "saving_per_100_units": r.saving_per_100km,
        "ci_95": [-r.ci_high, -r.ci_low],
        "p_value": r.p_value,
        "statistically_significant": r.significant_at_05,
        "method": "randomised holdout, difference-in-differences, Welch t-test",
    }


# ----------------------------------------------------------------- write tools (proposed only)


def _never_called(**_: Any) -> None:
    raise AssertionError("write tools are proposed, never executed by the agent loop")


TOOLS: list[Tool] = [
    Tool("fleet_summary", "Vehicle counts by powertrain and number of depots for this tenant.",
         {"type": "object", "properties": {}}, fleet_summary, "read"),
    Tool("list_depots", "Depots for this tenant with vehicle counts.",
         {"type": "object", "properties": {}}, list_depots, "read"),
    Tool("vehicle_detail", "Model, battery size, charge rate and depot for one VIN.",
         {"type": "object",
          "properties": {"vin": {"type": "string", "description": "17-character VIN"}},
          "required": ["vin"]}, vehicle_detail, "read"),
    Tool("open_alerts", "Open readiness alerts on the Morning Board, highest severity first.",
         {"type": "object",
          "properties": {"limit": {"type": "integer", "description": "max 12"}}},
         open_alerts, "read"),
    Tool("tariff_bands", "Time-of-use electricity tariff bands for this tenant.",
         {"type": "object", "properties": {}}, tariff_bands, "read"),
    Tool("charging_saving",
         "The MEASURED saving from the randomised holdout experiment, with confidence interval. "
         "Use this whenever asked how much the platform saves; never estimate it yourself.",
         {"type": "object",
          "properties": {"days": {"type": "integer", "description": "window, default 30"}}},
         charging_saving, "read"),

    # Write tools. The loop collects these as proposals and a human approves them.
    Tool("propose_swap_vehicle",
         "Propose swapping a vehicle's assigned route to a spare. Requires human approval.",
         {"type": "object",
          "properties": {"vin": {"type": "string"}, "spare_vin": {"type": "string"},
                         "reason": {"type": "string"}},
          "required": ["vin", "spare_vin", "reason"]}, _never_called, "write"),
    Tool("propose_book_bay",
         "Propose booking a maintenance bay slot for a vehicle. Requires human approval.",
         {"type": "object",
          "properties": {"vin": {"type": "string"}, "when": {"type": "string"},
                         "reason": {"type": "string"}},
          "required": ["vin", "when", "reason"]}, _never_called, "write"),
]
