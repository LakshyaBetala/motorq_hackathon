# ADR-0004: Tenant isolation enforced in the database, not the application

**Status:** Accepted · 2026-10-08 · Amends the RLS approach introduced in migration 001

## Context

The 100,000 vehicles belong to ~60 tenants with genuinely opposed interests — a lender and a fleet
operator must not see each other's data. Relying on every query carrying `WHERE tenant_id = ?` is
one forgotten clause away from a cross-tenant leak, and that clause is easy to forget in a
hackathon timebox.

Migration 001 enabled row-level security and *looked* correct. Running it against a live database
showed it was not. A connection scoped to tenant 1 returned **both** tenants' vehicles.

Two distinct holes, neither visible to a unit test because both lived in the database's privilege
model rather than in application code:

1. `ENABLE ROW LEVEL SECURITY` **does not apply to the table owner.** The API connected as the
   owning role, so every policy was silently inert.
2. Policies declared `USING` but not `WITH CHECK`. `USING` filters reads, so a caller scoped to
   tenant 1 could still **INSERT or UPDATE rows carrying tenant 2's id** — writing into another
   customer's data.

## Options considered

1. **Application-layer scoping only.** Rejected: one missing clause is a breach, and nothing fails
   loudly when it is missing.
2. **Schema-per-tenant.** Strong isolation. Rejected: 60 schemas turn every migration into a
   fan-out that is unworkable in the timebox.
3. **RLS with FORCE, plus a least-privilege non-owner role.** Chosen.

## Decision

- `FORCE ROW LEVEL SECURITY` on every tenant-scoped table, so even the owner is subject to policy.
- A dedicated `readyline_app` login role owning nothing and holding no DDL rights. The API
  connects as this role; migrations run as the owner.
- Every policy carries both `USING` and `WITH CHECK`.
- The scope expression is `NULLIF(current_setting('app.tenant_id', true), '')::INTEGER`. The
  `NULLIF` matters: after `RESET`, `current_setting` returns an empty string, and casting `''` to
  integer raises — turning a missing scope into a 500 rather than an empty result.
- Policies are dropped and recreated in a single loop over one table list, because maintaining a
  separate hand-written DROP list is exactly what left stale policies behind the first time.
- `audit_log` is append-only by privilege **and** by trigger, since a future `GRANT` could
  silently undo the `REVOKE`.

## Consequences

- An unscoped connection sees **zero rows** — not an error, not everything. Fail-closed.
- Every request must set `app.tenant_id` within the transaction. Forgetting it yields an obviously
  empty result rather than a silent leak.
- Policies add a predicate to every query. The partial index `idx_alert_board` keeps the hot
  Morning Board path cheap.
- Verified by `tests/test_tenant_isolation.py` against a real PostgreSQL. CI fails if those tests
  *skip*, because a silent skip would mean a green build with no isolation coverage at all.
