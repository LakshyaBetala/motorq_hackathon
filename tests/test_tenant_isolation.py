"""Integration tests for tenant isolation, against a real PostgreSQL.

These exist because the first version of the schema *looked* correct and was not. `ENABLE ROW
LEVEL SECURITY` does not apply to the table owner, so with the API connected as the owner every
policy was inert: a probe scoped to tenant 1 returned both tenants' vehicles. Nothing in a unit
test would have caught that, because the bug lived entirely in the database's privilege model.

Each test below maps to one hole that was actually open:
  * test_tenant_sees_only_own_vehicles      -> owner bypass (FORCE ROW LEVEL SECURITY)
  * test_cross_tenant_insert_is_rejected    -> policies had USING but no WITH CHECK
  * test_unscoped_connection_sees_nothing   -> fail-closed, and without raising
  * test_audit_log_cannot_be_mutated        -> evidence table must be append-only

Skipped automatically when no database is reachable, so the unit suite still runs standalone.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

DSN_OWNER = os.environ.get(
    "READYLINE_PG_OWNER_DSN",
    "postgresql://voltroute:devonly@127.0.0.1:5432/voltroute",
)
DSN_APP = os.environ.get(
    "READYLINE_PG_APP_DSN",
    "postgresql://readyline_app:devonly@127.0.0.1:5432/voltroute",
)

VIN_T1 = "1M8GDM9AXKP042788"
VIN_T2 = "1M8GDM9AXKP042771"


def _reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(DSN_OWNER),
    reason="PostgreSQL not reachable; run `make up` first",
)


@pytest.fixture(scope="module", autouse=True)
def seeded() -> None:
    """Two tenants, one vehicle each. Idempotent so the suite can be re-run."""
    with psycopg.connect(DSN_OWNER, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO tenant(name,region) VALUES ('T1','in') ON CONFLICT (name) DO NOTHING")
        cur.execute("INSERT INTO tenant(name,region) VALUES ('T2','in') ON CONFLICT (name) DO NOTHING")
        cur.execute("SELECT tenant_id FROM tenant WHERE name='T1'")
        t1 = cur.fetchone()[0]
        cur.execute("SELECT tenant_id FROM tenant WHERE name='T2'")
        t2 = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO depot(tenant_id,name,lat,lon,geohash7) VALUES (%s,'D1',12.9,77.5,'tdr1y4b')"
            " ON CONFLICT (tenant_id,name) DO NOTHING", (t1,))
        cur.execute(
            "INSERT INTO depot(tenant_id,name,lat,lon,geohash7) VALUES (%s,'D2',13.0,80.2,'tf2abcd')"
            " ON CONFLICT (tenant_id,name) DO NOTHING", (t2,))
        cur.execute("SELECT depot_id FROM depot WHERE tenant_id=%s", (t1,))
        d1 = cur.fetchone()[0]
        cur.execute("SELECT depot_id FROM depot WHERE tenant_id=%s", (t2,))
        d2 = cur.fetchone()[0]

        cur.execute("INSERT INTO fleet(tenant_id,depot_id,name) VALUES (%s,%s,'F1')"
                    " ON CONFLICT (tenant_id,name) DO NOTHING", (t1, d1))
        cur.execute("INSERT INTO fleet(tenant_id,depot_id,name) VALUES (%s,%s,'F2')"
                    " ON CONFLICT (tenant_id,name) DO NOTHING", (t2, d2))
        cur.execute("SELECT fleet_id FROM fleet WHERE tenant_id=%s", (t1,))
        f1 = cur.fetchone()[0]
        cur.execute("SELECT fleet_id FROM fleet WHERE tenant_id=%s", (t2,))
        f2 = cur.fetchone()[0]

        cur.execute("INSERT INTO oem(oem_id,name,dialect_code) VALUES (1,'TataMotors','tata_v2')"
                    " ON CONFLICT (oem_id) DO NOTHING")
        cur.execute(
            "INSERT INTO vehicle_model(oem_id,name,powertrain,battery_kwh,wltp_range_km,max_charge_kw)"
            " VALUES (1,'Nexon EV','BEV',40.5,312,50) ON CONFLICT (oem_id,name) DO NOTHING")
        cur.execute("SELECT model_id FROM vehicle_model WHERE name='Nexon EV'")
        model = cur.fetchone()[0]

        for vin, tenant, fleet, reg in ((VIN_T1, t1, f1, "KA01AB1234"), (VIN_T2, t2, f2, "TN02CD5678")):
            cur.execute(
                "INSERT INTO vehicle(vin,tenant_id,fleet_id,model_id,registration,in_service_on)"
                " VALUES (%s,%s,%s,%s,%s,'2024-01-01') ON CONFLICT (vin) DO NOTHING",
                (vin, tenant, fleet, model, reg))

        pytest.t1, pytest.t2 = t1, t2


def _as_tenant(tenant_id: int | None):
    conn = psycopg.connect(DSN_APP, autocommit=True)
    if tenant_id is not None:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant_id),))
    return conn


def test_app_role_is_not_the_table_owner():
    """The whole fix depends on this. If the API ever connects as the owner, RLS goes inert."""
    with psycopg.connect(DSN_OWNER) as conn, conn.cursor() as cur:
        cur.execute("SELECT tableowner FROM pg_tables WHERE tablename='vehicle'")
        assert cur.fetchone()[0] != "readyline_app"


def test_force_rls_is_set_on_every_tenant_scoped_table():
    """ENABLE alone is not enough; without FORCE the owner bypasses every policy."""
    expected = {
        "vehicle", "alert", "trip", "driver", "driver_assignment", "shift", "work_order",
        "depot", "fleet", "tariff_band", "subscription", "incident_signature", "audit_log",
    }
    with psycopg.connect(DSN_OWNER) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity"
            " AND relname = ANY(%s)", (list(expected),))
        forced = {r[0] for r in cur.fetchall()}
    assert forced == expected, f"missing FORCE RLS on {sorted(expected - forced)}"


def test_tenant_sees_only_own_vehicles():
    """The exact probe that returned 2 before the fix."""
    with _as_tenant(pytest.t1) as conn, conn.cursor() as cur:
        cur.execute("SELECT vin FROM vehicle")
        rows = [r[0].strip() for r in cur.fetchall()]
    assert rows == [VIN_T1]

    with _as_tenant(pytest.t2) as conn, conn.cursor() as cur:
        cur.execute("SELECT vin FROM vehicle")
        rows = [r[0].strip() for r in cur.fetchall()]
    assert rows == [VIN_T2]


def test_tenant_cannot_read_another_tenants_row_even_by_primary_key():
    """A direct lookup must not become a side channel around the list endpoint."""
    with _as_tenant(pytest.t1) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM vehicle WHERE vin = %s", (VIN_T2,))
        assert cur.fetchone()[0] == 0


def test_unscoped_connection_sees_nothing_and_does_not_raise():
    """Fail-closed. An unset scope must return zero rows, not error and not everything.

    ''::INTEGER raises, which is why the policy wraps current_setting in NULLIF -- otherwise a
    missing scope turns into a 500 instead of an empty result.
    """
    with _as_tenant(None) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM vehicle")
        assert cur.fetchone()[0] == 0


def test_cross_tenant_insert_is_rejected():
    """USING filters reads; only WITH CHECK stops a scoped caller writing into another tenant."""
    with _as_tenant(pytest.t1) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "INSERT INTO vehicle(vin,tenant_id,fleet_id,model_id,registration,in_service_on)"
                " SELECT '1M8GDM9AXKP042764', %s, fleet_id, 1, 'XX01ZZ9999', '2024-01-01'"
                " FROM fleet LIMIT 1", (pytest.t2,))


def test_cross_tenant_update_is_rejected():
    with _as_tenant(pytest.t1) as conn, conn.cursor() as cur:
        cur.execute("UPDATE vehicle SET registration='HACKED' WHERE vin=%s", (VIN_T2,))
        assert cur.rowcount == 0  # invisible, so nothing to update


def test_audit_log_cannot_be_updated_or_deleted():
    """The evidence table is append-only, enforced by trigger as well as by privilege."""
    with psycopg.connect(DSN_OWNER, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log(tenant_id,actor_type,actor_id,action,resource,row_hash)"
            " VALUES (%s,'USER','u1','READ','vehicle','\\x00')", (pytest.t1,))
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute("UPDATE audit_log SET action='TAMPERED' WHERE tenant_id=%s", (pytest.t1,))
    with psycopg.connect(DSN_OWNER, autocommit=True) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute("DELETE FROM audit_log WHERE tenant_id=%s", (pytest.t1,))


def test_vin_check_constraint_rejects_iso_excluded_letters():
    """The database is the last line of defence if an ingest adapter forgets to validate."""
    with psycopg.connect(DSN_OWNER, autocommit=True) as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO vehicle(vin,tenant_id,fleet_id,model_id,registration,in_service_on)"
                " SELECT '1M8GDM9AXKP04278I', %s, fleet_id, 1, 'ZZ01ZZ0001','2024-01-01'"
                " FROM fleet WHERE tenant_id=%s LIMIT 1", (pytest.t1, pytest.t1))


def test_driver_assignment_rejects_overlapping_custody():
    """Two drivers must never hold the same vehicle at once, or person-linked data diverges."""
    with psycopg.connect(DSN_OWNER, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO driver(tenant_id,employee_ref,dek_id) VALUES (%s,'E1',gen_random_uuid())"
            " ON CONFLICT (tenant_id,employee_ref) DO NOTHING", (pytest.t1,))
        cur.execute(
            "INSERT INTO driver(tenant_id,employee_ref,dek_id) VALUES (%s,'E2',gen_random_uuid())"
            " ON CONFLICT (tenant_id,employee_ref) DO NOTHING", (pytest.t1,))
        cur.execute("SELECT driver_id FROM driver WHERE tenant_id=%s ORDER BY employee_ref", (pytest.t1,))
        d1, d2 = [r[0] for r in cur.fetchall()[:2]]

        cur.execute("DELETE FROM driver_assignment WHERE vin=%s", (VIN_T1,))
        cur.execute(
            "INSERT INTO driver_assignment(tenant_id,vin,driver_id,started_at,ended_at)"
            " VALUES (%s,%s,%s,'2026-01-01T08:00Z','2026-01-01T17:00Z')",
            (pytest.t1, VIN_T1, d1))
        with pytest.raises(psycopg.errors.ExclusionViolation):
            cur.execute(
                "INSERT INTO driver_assignment(tenant_id,vin,driver_id,started_at,ended_at)"
                " VALUES (%s,%s,%s,'2026-01-01T12:00Z','2026-01-01T20:00Z')",
                (pytest.t1, VIN_T1, d2))
