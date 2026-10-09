"""Seed PostgreSQL with the generated fleet.

Uses COPY, not INSERT. At 100,000 vehicles plus depots and fleets, row-by-row INSERT takes
minutes and generates one WAL record per row; COPY streams the whole table in one statement and
finishes in seconds. This is the difference between `make seed` being part of the demo and being
something you start and then go for coffee.

Runs as the migration owner, not `readyline_app`: seeding writes across every tenant, which the
application role is correctly forbidden from doing by row-level security (ADR-0004).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

import psycopg  # noqa: E402

from .fleet import MODELS, generate_depots, generate_vehicles, plan_tenants  # noqa: E402
from .telemetry import DIALECTS  # noqa: E402

DSN = os.environ.get(
    "READYLINE_PG_OWNER_DSN", "postgresql://voltroute:devonly@127.0.0.1:5432/voltroute"
)

RULES = [
    ("AUX_BATT_DECAY", "Auxiliary battery decay",
     "12 V resting voltage falling; no-crank risk", "CRITICAL"),
    ("COOLANT_TREND", "Coolant temperature trend",
     "Coolant climbing across trips; leak or thermostat", "HIGH"),
    ("SOH_CLIFF", "Battery state-of-health cliff",
     "Pack capacity dropping faster than the fleet cohort", "HIGH"),
    ("PACK_THERMAL", "Pack thermal event",
     "Pack temperature outside safe envelope", "CRITICAL"),
    ("BRAKE_WEAR", "Brake pad wear",
     "Pad wear past service threshold", "MEDIUM"),
    ("RANGE_SHORTFALL", "Range shortfall against route",
     "Committed SoC will not cover tomorrow's assigned route", "HIGH"),
    ("UNAPPROVED_DEPOT", "Parked at an unapproved location",
     "Vehicle cluster away from any registered depot", "MEDIUM"),
]


def seed(total_vehicles: int = 100_000, tenants: int = 60, seed_value: int = 20261008) -> dict:
    started = time.perf_counter()
    tenant_list = plan_tenants(total_vehicles, tenants, seed=seed_value)
    depot_list = generate_depots(tenant_list, seed=seed_value)

    with psycopg.connect(DSN, autocommit=False) as conn:
        with conn.cursor() as cur:
            # Idempotent: a reviewer may run `make seed` twice. Truncate in dependency order.
            cur.execute(
                "TRUNCATE vehicle, fleet, depot, driver_assignment, driver, shift, trip,"
                " alert, alert_feedback, work_order, incident_signature, subscription,"
                " tariff_band, charger, vehicle_model, oem, tenant RESTART IDENTITY CASCADE"
            )

            cur.executemany(
                "INSERT INTO rule(rule_code, title, description, default_band)"
                " VALUES (%s,%s,%s,%s) ON CONFLICT (rule_code) DO NOTHING",
                RULES,
            )

            cur.executemany(
                "INSERT INTO tenant(name, region) VALUES (%s,%s)",
                [(t.name, "in") for t in tenant_list],
            )

            with cur.copy(
                "COPY depot(tenant_id, name, lat, lon, geohash7) FROM STDIN"
            ) as copy:
                from readyline.algorithms.streaming import geohash_encode
                for d in depot_list:
                    copy.write_row(
                        (d.tenant_id, d.name, d.lat, d.lon, geohash_encode(d.lat, d.lon, 7))
                    )

            # One fleet per depot, so fleet_id lines up with depot_id as the generator assumes.
            with cur.copy("COPY fleet(tenant_id, depot_id, name) FROM STDIN") as copy:
                for d in depot_list:
                    copy.write_row((d.tenant_id, d.depot_id, f"{d.name} fleet"))

            oems = sorted({m.oem for m in MODELS})
            dialect_of = {m.oem: m.dialect for m in MODELS}
            cur.executemany(
                "INSERT INTO oem(oem_id, name, dialect_code) VALUES (%s,%s,%s)",
                [(i + 1, name, dialect_of[name]) for i, name in enumerate(oems)],
            )
            oem_id = {name: i + 1 for i, name in enumerate(oems)}

            cur.executemany(
                "INSERT INTO vehicle_model"
                "(oem_id, name, powertrain, battery_kwh, tank_litres, wltp_range_km, max_charge_kw)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [
                    (oem_id[m.oem], m.name, m.powertrain, m.battery_kwh,
                     m.tank_litres, m.wltp_range_km, m.max_charge_kw)
                    for m in MODELS
                ],
            )
            cur.execute("SELECT model_id, name FROM vehicle_model")
            model_id = {name: mid for mid, name in cur.fetchall()}

            count = 0
            with cur.copy(
                "COPY vehicle(vin, tenant_id, fleet_id, model_id, registration, in_service_on)"
                " FROM STDIN"
            ) as copy:
                for v in generate_vehicles(tenant_list, depot_list, seed=seed_value):
                    copy.write_row(
                        (v.vin, v.tenant_id, v.fleet_id, model_id[v.model.name],
                         v.registration, "2024-01-01")
                    )
                    count += 1

            # A realistic time-of-use tariff for every tenant, which A0 reads.
            bands = []
            for t in tenant_list:
                for name, start, end, price in (
                    ("evening_peak", 18 * 60, 22 * 60, 10.8),
                    ("night_trough", 22 * 60, 24 * 60, 4.1),
                    ("early_trough", 0, 4 * 60, 3.4),
                    ("morning", 4 * 60, 8 * 60, 5.4),
                    ("day", 8 * 60, 18 * 60, 7.9),
                ):
                    bands.append((t.tenant_id, name, start, end, price))
            cur.executemany(
                "INSERT INTO tariff_band(tenant_id, name, start_minute, end_minute, price_per_kwh)"
                " VALUES (%s,%s,%s,%s,%s)",
                bands,
            )

        conn.commit()

    elapsed = time.perf_counter() - started
    return {
        "tenants": len(tenant_list),
        "depots": len(depot_list),
        "vehicles": count,
        "models": len(MODELS),
        "dialects": len(DIALECTS),
        "elapsed_s": round(elapsed, 2),
        "rows_per_sec": round(count / elapsed) if elapsed > 0 else 0,
    }


if __name__ == "__main__":
    total = int(os.environ.get("SIM_VEHICLES", "100000"))
    tenants = int(os.environ.get("SIM_TENANTS", "60"))
    stats = seed(total, tenants)
    print(
        f"seeded {stats['vehicles']:,} vehicles across {stats['tenants']} tenants "
        f"and {stats['depots']} depots in {stats['elapsed_s']}s "
        f"({stats['rows_per_sec']:,} rows/s)"
    )
