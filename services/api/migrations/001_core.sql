-- PostgreSQL: the CP side. Everything that must not diverge.
-- Third Normal Form throughout; the two deliberate denormalisations are marked DENORM and justified.
--
-- Tenancy model: the 100,000 vehicles are ~60 tenants, not one fleet. Every table that holds
-- tenant data carries tenant_id and is protected by row-level security, so a missing WHERE clause
-- in application code cannot leak across tenants.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------- tenancy & identity

CREATE TABLE tenant (
    tenant_id     INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          TEXT        NOT NULL UNIQUE,
    region        TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
    user_id       UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE RESTRICT,
    oidc_subject  TEXT        NOT NULL UNIQUE,     -- Keycloak `sub`; we never store a password
    email         TEXT        NOT NULL,
    display_name  TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE TABLE role (
    role_id       SMALLINT    PRIMARY KEY,
    code          TEXT        NOT NULL UNIQUE,     -- DISPATCHER, FLEET_ADMIN, FINANCE, AUDITOR
    description   TEXT        NOT NULL
);

CREATE TABLE user_role (
    user_id       UUID        NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
    role_id       SMALLINT    NOT NULL REFERENCES role(role_id)     ON DELETE RESTRICT,
    PRIMARY KEY (user_id, role_id)
);

-- ---------------------------------------------------------------- fleet structure

CREATE TABLE depot (
    depot_id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    name          TEXT        NOT NULL,
    lat           DOUBLE PRECISION NOT NULL,
    lon           DOUBLE PRECISION NOT NULL,
    geohash7      CHAR(7)     NOT NULL,            -- approved-location check for A8
    UNIQUE (tenant_id, name)
);

CREATE TABLE fleet (
    fleet_id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    depot_id      INTEGER     REFERENCES depot(depot_id) ON DELETE SET NULL,
    name          TEXT        NOT NULL,
    UNIQUE (tenant_id, name)
);

CREATE TABLE oem (
    oem_id        SMALLINT    PRIMARY KEY,
    name          TEXT        NOT NULL UNIQUE,
    dialect_code  TEXT        NOT NULL             -- selects the normalisation registry entry
);

CREATE TABLE vehicle_model (
    model_id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    oem_id            SMALLINT NOT NULL REFERENCES oem(oem_id) ON DELETE RESTRICT,
    name              TEXT     NOT NULL,
    powertrain        TEXT     NOT NULL CHECK (powertrain IN ('ICE','HEV','PHEV','BEV')),
    battery_kwh       NUMERIC(6,2),                -- NULL for pure ICE
    tank_litres       NUMERIC(6,2),                -- NULL for pure BEV
    wltp_range_km     NUMERIC(6,1),
    max_charge_kw     NUMERIC(6,2),
    CHECK (powertrain <> 'BEV' OR battery_kwh IS NOT NULL),
    CHECK (powertrain <> 'ICE' OR tank_litres  IS NOT NULL),
    UNIQUE (oem_id, name)
);

CREATE TABLE vehicle (
    vin           CHAR(17)    PRIMARY KEY
                  CHECK (vin ~ '^[A-HJ-NPR-Z0-9]{17}$'),   -- no I, O or Q (ISO 3779); A4
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id)    ON DELETE CASCADE,
    fleet_id      INTEGER     NOT NULL REFERENCES fleet(fleet_id)      ON DELETE RESTRICT,
    model_id      INTEGER     NOT NULL REFERENCES vehicle_model(model_id) ON DELETE RESTRICT,
    registration  TEXT        NOT NULL,
    in_service_on DATE        NOT NULL,
    retired_on    DATE,
    UNIQUE (tenant_id, registration),
    CHECK (retired_on IS NULL OR retired_on >= in_service_on)
);

-- ---------------------------------------------------------------- people, kept separable

-- Driver PII is isolated in its own table with a per-row encryption key id, so a DPDP/GDPR
-- erasure request is satisfied by destroying one key (crypto-shredding) rather than by chasing
-- rows across a 3 PB telemetry store we cannot practically rewrite.
CREATE TABLE driver (
    driver_id     UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    employee_ref  TEXT        NOT NULL,
    name_enc      BYTEA,                           -- NULL once erased
    phone_enc     BYTEA,
    dek_id        UUID        NOT NULL,            -- data-encryption-key id held in the vault
    erased_at     TIMESTAMPTZ,
    UNIQUE (tenant_id, employee_ref)
);

-- Temporal bridge. A vehicle is shared by several drivers and can change hands mid-day, so
-- "who was driving" is only answerable as a function of time. Without this, person-linked and
-- asset-linked data silently diverge.
CREATE TABLE driver_assignment (
    assignment_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    vin           CHAR(17)    NOT NULL REFERENCES vehicle(vin)      ON DELETE CASCADE,
    driver_id     UUID        NOT NULL REFERENCES driver(driver_id) ON DELETE CASCADE,
    started_at    TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ,
    CHECK (ended_at IS NULL OR ended_at > started_at)
);
-- No two drivers may hold the same vehicle at once.
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE driver_assignment ADD CONSTRAINT driver_assignment_no_overlap
    EXCLUDE USING gist (
        vin WITH =,
        tstzrange(started_at, COALESCE(ended_at, 'infinity'::timestamptz)) WITH &&
    );

-- ---------------------------------------------------------------- operations

CREATE TABLE shift (
    shift_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    vin           CHAR(17)    NOT NULL REFERENCES vehicle(vin)      ON DELETE CASCADE,
    planned_start TIMESTAMPTZ NOT NULL,
    planned_end   TIMESTAMPTZ NOT NULL,
    planned_km    NUMERIC(7,1) NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'PLANNED'
                  CHECK (status IN ('PLANNED','ACTIVE','DONE','REASSIGNED','CANCELLED')),
    CHECK (planned_end > planned_start)
);

CREATE TABLE trip (
    trip_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    vin           CHAR(17)    NOT NULL REFERENCES vehicle(vin)      ON DELETE CASCADE,
    driver_id     UUID        REFERENCES driver(driver_id) ON DELETE SET NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ NOT NULL,
    distance_km   NUMERIC(8,2) NOT NULL CHECK (distance_km >= 0),
    idle_seconds  INTEGER      NOT NULL DEFAULT 0 CHECK (idle_seconds >= 0),
    energy_kwh    NUMERIC(8,3),
    fuel_litres   NUMERIC(8,3),
    start_geohash7 CHAR(7),
    end_geohash7   CHAR(7),
    CHECK (ended_at > started_at)
);

CREATE TABLE charger (
    charger_id    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     REFERENCES tenant(tenant_id) ON DELETE CASCADE,  -- NULL = public
    depot_id      INTEGER     REFERENCES depot(depot_id)   ON DELETE CASCADE,
    lat           DOUBLE PRECISION NOT NULL,
    lon           DOUBLE PRECISION NOT NULL,
    power_kw      NUMERIC(6,2) NOT NULL CHECK (power_kw > 0),
    connector     TEXT        NOT NULL
);

-- Time-of-use tariff bands. Input to the DP charging schedule (A2).
CREATE TABLE tariff_band (
    tariff_band_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id      INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    name           TEXT        NOT NULL,
    day_of_week    SMALLINT    CHECK (day_of_week BETWEEN 0 AND 6),  -- NULL = every day
    start_minute   SMALLINT    NOT NULL CHECK (start_minute BETWEEN 0 AND 1439),
    end_minute     SMALLINT    NOT NULL CHECK (end_minute   BETWEEN 1 AND 1440),
    price_per_kwh  NUMERIC(8,4) NOT NULL CHECK (price_per_kwh >= 0),
    CHECK (end_minute > start_minute)
);

-- ---------------------------------------------------------------- alerts

CREATE TABLE rule (
    rule_code     TEXT        PRIMARY KEY,         -- AUX_BATT_DECAY, SOH_CLIFF, COOLANT_TREND...
    title         TEXT        NOT NULL,
    description   TEXT        NOT NULL,
    default_band  TEXT        NOT NULL CHECK (default_band IN ('CRITICAL','HIGH','MEDIUM','LOW'))
);

CREATE TABLE alert (
    alert_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    vin           CHAR(17)    NOT NULL REFERENCES vehicle(vin)      ON DELETE CASCADE,
    rule_code     TEXT        NOT NULL REFERENCES rule(rule_code)   ON DELETE RESTRICT,
    severity      TEXT        NOT NULL CHECK (severity IN ('CRITICAL','HIGH','MEDIUM','LOW')),
    vews_score    SMALLINT    NOT NULL CHECK (vews_score BETWEEN 0 AND 27),
    -- DENORM: dollars_at_risk is computed by the detector and stored rather than recomputed on
    -- read. Justified: the Morning Board ranks by it on every poll, and recomputing a cost model
    -- across 100k vehicles per request would blow the p95 budget. Recomputed nightly in batch.
    dollars_at_risk NUMERIC(10,2) NOT NULL DEFAULT 0,
    evidence      JSONB       NOT NULL,            -- the SINCE/WHY slots, as structured facts
    raised_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Idempotency: the detector may legitimately re-fire. One open alert per (vin, rule).
    dedup_key     TEXT        NOT NULL,
    state         TEXT        NOT NULL DEFAULT 'OPEN'
                  CHECK (state IN ('OPEN','ACKED','ACTIONED','DISMISSED','EXPIRED')),
    resolved_at   TIMESTAMPTZ,
    UNIQUE (dedup_key)
);

-- Dismiss-with-reason: the only labelled feedback a real user will ever give us.
CREATE TABLE alert_feedback (
    alert_id      BIGINT      PRIMARY KEY REFERENCES alert(alert_id) ON DELETE CASCADE,
    user_id       UUID        NOT NULL REFERENCES app_user(user_id)  ON DELETE RESTRICT,
    reason_code   TEXT        NOT NULL,            -- NOT_REAL, KNOWN, NOT_TODAY, ALREADY_FIXED
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE work_order (
    work_order_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    alert_id      BIGINT      REFERENCES alert(alert_id) ON DELETE SET NULL,
    vin           CHAR(17)    NOT NULL REFERENCES vehicle(vin) ON DELETE CASCADE,
    action        TEXT        NOT NULL CHECK (action IN ('SWAP','BAY','CHARGE','INSPECT')),
    scheduled_for TIMESTAMPTZ,
    est_cost      NUMERIC(10,2),
    created_by    UUID        REFERENCES app_user(user_id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- commercial (strict ACID)

CREATE TABLE subscription (
    subscription_id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE RESTRICT,
    plan_code       TEXT        NOT NULL,
    seats           INTEGER     NOT NULL CHECK (seats > 0),
    price_per_vehicle NUMERIC(8,2) NOT NULL,
    started_on      DATE        NOT NULL,
    ended_on        DATE,
    CHECK (ended_on IS NULL OR ended_on > started_on)
);

-- ---------------------------------------------------------------- audit (append-only)

-- Every data access and every AI-agent action. INSERT only; UPDATE/DELETE revoked below.
-- prev_hash chains the rows so tampering is detectable.
CREATE TABLE audit_log (
    audit_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     NOT NULL,
    actor_type    TEXT        NOT NULL CHECK (actor_type IN ('USER','AGENT','SYSTEM')),
    actor_id      TEXT        NOT NULL,
    action        TEXT        NOT NULL,
    resource      TEXT        NOT NULL,
    detail        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    prev_hash     BYTEA,
    row_hash      BYTEA       NOT NULL
);

-- ---------------------------------------------------------------- vector (pgvector)

-- OEM service bulletins for RAG, and incident signatures for the "7 similar past cases" kNN.
CREATE TABLE bulletin (
    bulletin_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    oem_id        SMALLINT    NOT NULL REFERENCES oem(oem_id) ON DELETE CASCADE,
    title         TEXT        NOT NULL,
    body          TEXT        NOT NULL,
    embedding     vector(384) NOT NULL
);

CREATE TABLE incident_signature (
    signature_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     INTEGER     NOT NULL REFERENCES tenant(tenant_id) ON DELETE CASCADE,
    vin           CHAR(17)    NOT NULL,
    rule_code     TEXT        NOT NULL REFERENCES rule(rule_code) ON DELETE CASCADE,
    outcome       TEXT        NOT NULL CHECK (outcome IN ('FAILED','SURVIVED')),
    occurred_at   TIMESTAMPTZ NOT NULL,
    embedding     vector(64)  NOT NULL             -- 9-signal trajectory, windowed + normalised
);

-- ---------------------------------------------------------------- indexes

-- The Morning Board query: open alerts for my tenant, ranked. Partial index keeps it tiny —
-- only OPEN rows are ever ranked, which is a few thousand out of millions.
CREATE INDEX idx_alert_board ON alert (tenant_id, severity, dollars_at_risk DESC, raised_at DESC)
    WHERE state = 'OPEN';

CREATE INDEX idx_alert_vin        ON alert (vin, raised_at DESC);
CREATE INDEX idx_trip_vin_time    ON trip (tenant_id, vin, started_at DESC);
CREATE INDEX idx_trip_keyset      ON trip (tenant_id, started_at DESC, trip_id DESC); -- keyset pagination
CREATE INDEX idx_assign_lookup    ON driver_assignment (vin, started_at DESC);
CREATE INDEX idx_vehicle_fleet    ON vehicle (tenant_id, fleet_id);
CREATE INDEX idx_audit_lookup     ON audit_log (tenant_id, occurred_at DESC);
CREATE INDEX idx_shift_today      ON shift (tenant_id, planned_start) WHERE status IN ('PLANNED','ACTIVE');

-- Approximate-NN indexes. HNSW over IVFFlat: no training step, better recall at our corpus size.
CREATE INDEX idx_bulletin_vec  ON bulletin           USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_signature_vec ON incident_signature USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------- row-level security

-- Defence in depth: even if application code forgets a WHERE tenant_id = ..., the database
-- refuses to return another tenant's rows.
ALTER TABLE vehicle           ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert             ENABLE ROW LEVEL SECURITY;
ALTER TABLE trip              ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver            ENABLE ROW LEVEL SECURITY;
ALTER TABLE driver_assignment ENABLE ROW LEVEL SECURITY;
ALTER TABLE shift             ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_order        ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON vehicle
    USING (tenant_id = current_setting('app.tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation ON alert
    USING (tenant_id = current_setting('app.tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation ON trip
    USING (tenant_id = current_setting('app.tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation ON driver
    USING (tenant_id = current_setting('app.tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation ON driver_assignment
    USING (tenant_id = current_setting('app.tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation ON shift
    USING (tenant_id = current_setting('app.tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation ON work_order
    USING (tenant_id = current_setting('app.tenant_id', true)::INTEGER);

-- Audit log is append-only even for the application role.
REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;
