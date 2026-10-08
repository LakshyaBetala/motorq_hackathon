"""API integration tests against a live PostgreSQL and ClickHouse.

These cover the HTTP surface the reviewers actually touch, and in particular that tenant
isolation survives the trip through FastAPI's connection pool -- the place it is easiest to lose
it, because `app.tenant_id` is per-connection and a pooled connection handed on without a reset
silently inherits the previous caller's scope.

Skipped automatically when the stack is not up, so the unit suite still runs standalone.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

DSN_OWNER = os.environ.get(
    "READYLINE_PG_OWNER_DSN", "postgresql://voltroute:devonly@127.0.0.1:5432/voltroute"
)

VIN_T1 = "1M8GDM9AXKP042788"
VIN_T2 = "1M8GDM9AXKP042771"


def _reachable() -> bool:
    try:
        with psycopg.connect(DSN_OWNER, connect_timeout=3):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="stack not up; run `make up`")


@pytest.fixture(scope="module")
def client():
    from readyline.api.deps import reset_rate_limits
    from readyline.api.main import app

    reset_rate_limits()
    with TestClient(app) as c:
        yield c
    reset_rate_limits()


def _t(tenant: int) -> dict[str, str]:
    return {"X-Tenant-Id": str(tenant), "X-Roles": "DISPATCHER"}


# --------------------------------------------------------------------- health & auth


def test_healthz_reports_dependencies(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok", body
    assert body["postgres"] is True
    assert body["clickhouse"] is True


def test_missing_tenant_scope_is_rejected(client):
    assert client.get("/api/v1/vehicles").status_code == 401


def test_invalid_tenant_scope_is_rejected(client):
    assert client.get("/api/v1/vehicles", headers={"X-Tenant-Id": "abc"}).status_code == 400


def test_openapi_is_served(client):
    spec = client.get("/openapi.json").json()
    assert "/api/v1/depot/plan" in spec["paths"]


# --------------------------------------------------------------------- tenant isolation


def test_each_tenant_sees_only_its_own_vehicles(client):
    """The pooled-connection failure mode: if app.tenant_id leaks between requests, this flips."""
    t1 = client.get("/api/v1/vehicles", headers=_t(1)).json()["items"]
    t2 = client.get("/api/v1/vehicles", headers=_t(2)).json()["items"]

    assert [v["vin"] for v in t1] == [VIN_T1]
    assert [v["vin"] for v in t2] == [VIN_T2]


def test_scope_does_not_leak_across_sequential_requests(client):
    """Alternate tenants on the same pool repeatedly; each must stay correctly scoped."""
    for _ in range(6):
        assert [v["vin"] for v in client.get("/api/v1/vehicles", headers=_t(1)).json()["items"]] == [VIN_T1]
        assert [v["vin"] for v in client.get("/api/v1/vehicles", headers=_t(2)).json()["items"]] == [VIN_T2]


def test_cannot_read_another_tenants_vehicle_by_vin(client):
    """A direct lookup must not become a side channel around the list endpoint."""
    r = client.get(f"/api/v1/vehicles/{VIN_T2}/telemetry", headers=_t(1))
    assert r.status_code == 404


def test_own_vehicle_telemetry_is_reachable(client):
    r = client.get(f"/api/v1/vehicles/{VIN_T1}/telemetry", headers=_t(1))
    assert r.status_code == 200
    assert r.json()["vin"] == VIN_T1


# --------------------------------------------------------------------- pagination


def test_pagination_is_keyset_and_terminates(client):
    """No OFFSET: page cost must not grow with depth, and the cursor must end cleanly."""
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        url = "/api/v1/vehicles?limit=1" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(url, headers=_t(1)).json()
        seen.extend(v["vin"] for v in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None, "cursor never terminated"
    assert len(seen) == len(set(seen)), "keyset pagination returned duplicates"


def test_page_size_is_capped(client):
    assert client.get("/api/v1/vehicles?limit=99999", headers=_t(1)).status_code == 422


# --------------------------------------------------------------------- the depot plan


def _plan_body(n: int = 6, cap: float = 100.0) -> dict:
    return {
        "tariff_per_slot": [9.0, 9.0, 3.0, 3.0, 3.0, 9.0],
        "site_cap_kw": cap,
        "slot_minutes": 60,
        "vehicles": [
            {
                "vin": f"EV{i:03d}", "soc_start_pct": 25.0, "soc_target_pct": 80.0,
                "battery_kwh": 40.5, "max_charge_kw": 22.0, "last_slot": 5, "depot_id": 1,
            }
            for i in range(n)
        ],
    }


def test_depot_plan_respects_the_cap(client):
    r = client.post("/api/v1/depot/plan", json=_plan_body(8, cap=100.0), headers=_t(1))
    assert r.status_code == 200
    body = r.json()
    assert body["cap_respected"] is True
    assert max(body["site_load_by_slot"]) <= body["cap_kw"] + 1e-6


def test_depot_plan_beats_the_unmanaged_baseline(client):
    """The business case, over HTTP. If this inverts there is nothing to sell."""
    body = client.post("/api/v1/depot/plan", json=_plan_body(8), headers=_t(1)).json()
    assert body["total_cost"] < body["unmanaged_cost"]
    assert body["saving"] > 0


def test_depot_plan_reports_a_bound_and_never_beats_it(client):
    body = client.post("/api/v1/depot/plan", json=_plan_body(6), headers=_t(1)).json()
    assert body["lower_bound"] > 0
    assert body["total_cost"] >= body["lower_bound"] - 1e-6
    if not body["infeasible_vins"]:
        assert body["duality_gap"] is not None


def test_depot_plan_assigns_a_control_arm(client):
    """~10% of vehicles must charge unmanaged, or there is no experiment."""
    body = client.post("/api/v1/depot/plan", json=_plan_body(60), headers=_t(1)).json()
    arms = [v["arm"] for v in body["vehicles"]]
    control = arms.count("control")
    assert 0 < control < len(arms), f"degenerate arm split: {control}/{len(arms)}"


def test_depot_plan_rejects_an_empty_fleet(client):
    body = _plan_body(1)
    body["vehicles"] = []
    assert client.post("/api/v1/depot/plan", json=body, headers=_t(1)).status_code == 422


def test_depot_plan_rejects_a_non_positive_cap(client):
    body = _plan_body(2)
    body["site_cap_kw"] = 0
    assert client.post("/api/v1/depot/plan", json=body, headers=_t(1)).status_code == 422


def test_depot_plan_requires_the_dispatcher_role(client):
    headers = {"X-Tenant-Id": "1", "X-Roles": "AUDITOR"}
    r = client.post("/api/v1/depot/plan", json=_plan_body(2), headers=headers)
    assert r.status_code == 403


# --------------------------------------------------------------------- the experiment


def test_experiment_endpoint_is_explicit_when_it_cannot_estimate(client):
    """It must refuse rather than return a fabricated effect when data is absent."""
    r = client.get("/api/v1/experiments/charging-arbitrage", headers=_t(1))
    assert r.status_code in (200, 409)
    if r.status_code == 409:
        assert "no charging sessions" in r.json()["detail"] or "control arm" in r.json()["detail"]
    else:
        body = r.json()
        assert "ci_low" in body and "ci_high" in body
        assert body["ci_low"] <= body["ci_high"]


# --------------------------------------------------------------------- the board


def test_board_is_capped_at_twelve(client):
    body = client.get("/api/v1/board", headers=_t(1)).json()
    assert body["cap"] == 12
    assert len(body["items"]) <= 12
    assert body["suppressed_count"] >= 0
