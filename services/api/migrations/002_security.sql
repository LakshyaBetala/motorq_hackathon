-- Fixes two real tenant-isolation holes found by testing 001 against a live Postgres.
--
-- Hole 1: ENABLE ROW LEVEL SECURITY does not apply to the table OWNER. The migration role owns
--         these tables, so if the API connected as that role, every policy was silently inert.
--         A probe with app.tenant_id = '1' returned rows from both tenants.
--
-- Hole 2: The policies in 001 declared only USING, which filters reads. Without WITH CHECK a
--         caller scoped to tenant 1 could INSERT or UPDATE a row carrying tenant_id = 2 —
--         writing into another customer's data.
--
-- Fixed with both belt and braces: FORCE RLS so the owner is subject to policies too, and a
-- dedicated least-privilege application role that is not the owner and holds no DDL rights.

-- ---------------------------------------------------------------- the application role

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'readyline_app') THEN
        -- Password is injected by the deploy; this default exists only so the local stack boots.
        CREATE ROLE readyline_app LOGIN PASSWORD 'devonly';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE voltroute TO readyline_app;
GRANT USAGE  ON SCHEMA public       TO readyline_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO readyline_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO readyline_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO readyline_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO readyline_app;

-- The audit log stays append-only even for the application role (restated: 001 revoked this
-- from PUBLIC, but the explicit GRANT above would otherwise hand it back).
REVOKE UPDATE, DELETE ON audit_log FROM readyline_app;

-- ---------------------------------------------------------------- force RLS on every scoped table

ALTER TABLE vehicle           FORCE ROW LEVEL SECURITY;
ALTER TABLE alert             FORCE ROW LEVEL SECURITY;
ALTER TABLE trip              FORCE ROW LEVEL SECURITY;
ALTER TABLE driver            FORCE ROW LEVEL SECURITY;
ALTER TABLE driver_assignment FORCE ROW LEVEL SECURITY;
ALTER TABLE shift             FORCE ROW LEVEL SECURITY;
ALTER TABLE work_order        FORCE ROW LEVEL SECURITY;

-- Tables that carry tenant data but were missed entirely in 001.
ALTER TABLE depot             ENABLE ROW LEVEL SECURITY;
ALTER TABLE depot             FORCE  ROW LEVEL SECURITY;
ALTER TABLE fleet             ENABLE ROW LEVEL SECURITY;
ALTER TABLE fleet             FORCE  ROW LEVEL SECURITY;
ALTER TABLE tariff_band       ENABLE ROW LEVEL SECURITY;
ALTER TABLE tariff_band       FORCE  ROW LEVEL SECURITY;
ALTER TABLE subscription      ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscription      FORCE  ROW LEVEL SECURITY;
ALTER TABLE incident_signature ENABLE ROW LEVEL SECURITY;
ALTER TABLE incident_signature FORCE  ROW LEVEL SECURITY;
ALTER TABLE audit_log         ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log         FORCE  ROW LEVEL SECURITY;

-- ---------------------------------------------------------------- policies with WITH CHECK
--
-- current_setting('app.tenant_id', true) returns NULL when never set, but an EMPTY STRING after
-- RESET -- and ''::INTEGER raises, which would turn a missing scope into a 500 instead of an
-- empty result. NULLIF collapses both cases to NULL, and `tenant_id = NULL` is NULL rather than
-- TRUE, so the policy denies everything. That fail-closed behaviour is deliberate: an unscoped
-- connection must see nothing, never everything.

-- Drop and recreate in one loop over the SAME list, so adding a table below can never leave a
-- stale policy behind (which is exactly what happened when the drop list was maintained by hand).
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'vehicle', 'alert', 'trip', 'driver', 'driver_assignment', 'shift', 'work_order',
        'depot', 'fleet', 'tariff_band', 'subscription', 'incident_signature', 'audit_log'
    ]
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
                USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::INTEGER)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::INTEGER)
        $f$, t);
    END LOOP;
END
$$;

-- ---------------------------------------------------------------- audit integrity

-- Append-only is enforced by a trigger as well as by privileges, because a future GRANT could
-- quietly undo the REVOKE above. Defence in depth for the one table whose job is to be evidence.
CREATE OR REPLACE FUNCTION audit_log_is_append_only() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_mutate ON audit_log;
CREATE TRIGGER audit_log_no_mutate
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();
