"""Request-scoped dependencies: settings, auth, tenant scoping, rate limiting.

The tenant-scoping dependency is the security-critical one. Row-level security in PostgreSQL is
driven by `app.tenant_id`, and that setting is per-connection. If a pooled connection is handed
to the next request without resetting it, that request inherits the previous tenant's scope --
a cross-tenant read with no bug visible anywhere in the application code.

`tenant_connection` therefore sets the scope inside a transaction and always resets it, even on
error. ADR-0004 has the detail.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg
from fastapi import Depends, Header, HTTPException, Request, status
from psycopg_pool import ConnectionPool


@dataclass(frozen=True, slots=True)
class Settings:
    pg_dsn: str = os.environ.get(
        "READYLINE_PG_APP_DSN",
        "postgresql://readyline_app:devonly@127.0.0.1:5432/voltroute",
    )
    ch_host: str = os.environ.get("CLICKHOUSE_HOST", "localhost")
    ch_port: int = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
    ch_user: str = os.environ.get("CLICKHOUSE_USER", "voltroute")
    ch_password: str = os.environ.get("CLICKHOUSE_PASSWORD", "devonly")
    ch_database: str = os.environ.get("CLICKHOUSE_DB", "voltroute")
    oidc_issuer: str = os.environ.get("OIDC_ISSUER", "http://localhost:8080/realms/readyline")
    # Local profile accepts a plain tenant header so the stack runs without Keycloak. The scale
    # and production profiles must not: a header is not an identity.
    allow_header_auth: bool = os.environ.get("ALLOW_HEADER_AUTH", "true").lower() == "true"
    rate_limit_per_minute: int = int(os.environ.get("API_RATE_LIMIT_PER_MIN", "600"))
    max_page_size: int = int(os.environ.get("API_MAX_PAGE_SIZE", "200"))


SETTINGS = Settings()

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(SETTINGS.pg_dsn, min_size=1, max_size=10, open=True)
    return _pool


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant_id: int
    roles: frozenset[str]

    def require(self, role: str) -> None:
        if role not in self.roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"role '{role}' required"
            )


def current_principal(
    authorization: str | None = Header(default=None),
    x_tenant_id: str | None = Header(default=None),
    x_roles: str | None = Header(default=None),
) -> Principal:
    """Resolve the caller.

    Production path is an OIDC bearer token from Keycloak, whose `sub`, `tenant_id` and `roles`
    claims are verified against the realm's JWKS. The local profile accepts X-Tenant-Id so the
    stack is usable without standing Keycloak up first -- gated behind ALLOW_HEADER_AUTH, which
    is false everywhere except local, because a header is an assertion, not an identity.
    """
    if authorization and authorization.lower().startswith("bearer "):
        return _principal_from_token(authorization.split(None, 1)[1])

    if not SETTINGS.allow_header_auth:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")

    if not x_tenant_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing tenant scope")
    try:
        tenant_id = int(x_tenant_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid tenant id") from exc

    roles = frozenset(filter(None, (x_roles or "DISPATCHER").split(",")))
    return Principal(subject=f"local:{tenant_id}", tenant_id=tenant_id, roles=roles)


def _principal_from_token(token: str) -> Principal:
    """Verify an OIDC access token and extract the tenant scope.

    Signature verification against the realm JWKS is required before this is used outside the
    local profile; it is deliberately left as an explicit failure rather than a silent
    accept-anything, so that deploying with ALLOW_HEADER_AUTH=false cannot quietly become
    "trust any bearer string".
    """
    from jose import jwt  # imported lazily; not needed on the local path

    try:
        claims = jwt.get_unverified_claims(token)
    except Exception as exc:  # noqa: BLE001 - any malformed token is simply unauthorised
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed token") from exc

    if not SETTINGS.allow_header_auth:
        # TODO(security): fetch JWKS from {issuer}/protocol/openid-connect/certs and verify.
        # Failing closed until that exists.
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "JWKS verification not configured; refusing to trust an unverified token",
        )

    tenant = claims.get("tenant_id")
    if tenant is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "token carries no tenant scope")
    realm_roles = claims.get("realm_access", {}).get("roles", [])
    return Principal(
        subject=str(claims.get("sub", "unknown")),
        tenant_id=int(tenant),
        roles=frozenset(realm_roles or ["DISPATCHER"]),
    )


@contextmanager
def tenant_connection(principal: Principal) -> Iterator[psycopg.Connection]:
    """A pooled connection scoped to the caller's tenant for the duration of one transaction.

    `set_config(..., true)` makes the setting transaction-local, so it unwinds automatically on
    commit or rollback. That matters more than it looks: with a session-level setting, a pooled
    connection returned mid-request would carry the previous caller's tenant into the next one.
    """
    pool = get_pool()
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.tenant_id', %s, true)",
                    (str(principal.tenant_id),),
                )
            yield conn


# ----------------------------------------------------------------- rate limiting

_buckets: dict[str, tuple[float, float]] = {}


def rate_limit(request: Request, principal: Principal = Depends(current_principal)) -> None:
    """Token bucket per principal.

    In-process on purpose for the local profile. Behind more than one replica this must move to
    Redis, or each replica enforces its own limit and the effective ceiling is N times the
    configured one -- the classic distributed rate-limiting mistake.
    """
    capacity = float(SETTINGS.rate_limit_per_minute)
    refill_per_sec = capacity / 60.0
    key = f"{principal.tenant_id}:{principal.subject}"
    now = time.monotonic()

    tokens, last = _buckets.get(key, (capacity, now))
    tokens = min(capacity, tokens + (now - last) * refill_per_sec)

    if tokens < 1.0:
        retry_after = max(1, int((1.0 - tokens) / refill_per_sec))
        _buckets[key] = (tokens, now)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )

    _buckets[key] = (tokens - 1.0, now)
    request.state.rate_tokens_left = int(tokens - 1.0)


def reset_rate_limits() -> None:
    """Test hook. Production never calls this."""
    _buckets.clear()
