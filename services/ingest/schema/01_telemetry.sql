-- ClickHouse: the AP side. Raw telemetry + rollups computed at insert time.
-- Nothing here is authoritative for billing, access control or audit — that lives in Postgres.

CREATE DATABASE IF NOT EXISTS voltroute;

-- Raw telemetry. ORDER BY puts fleet first so per-tenant scans are a contiguous range read,
-- then vin, then ts — which is also the prefix trip segmentation reads by.
CREATE TABLE IF NOT EXISTS voltroute.telemetry
(
    fleet_id      UInt32,
    vin           FixedString(17),
    ts            DateTime64(3, 'UTC'),
    seq           UInt64,
    lat           Float64,
    lon           Float64,
    geohash7      FixedString(7),          -- precision 7 ~= 153m, the depot-clustering bucket
    speed_kmh     Float32,
    soc_pct       Nullable(Float32),       -- NULL for ICE vehicles
    soh_pct       Nullable(Float32),
    pack_temp_c   Nullable(Float32),
    odo_km        Float64,
    fuel_lvl_pct  Nullable(Float32),
    evt           LowCardinality(String),
    dtc           Array(LowCardinality(String)),
    ingested_at   DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)            -- retention becomes a partition DROP, not a row delete
ORDER BY (fleet_id, vin, ts)
-- Hot tier is 7 days of full-resolution raw. After that the 1-minute rollup below carries the
-- warm tier (90 d) and Parquet on S3/MinIO carries cold (7 y), so the raw rows are redundant and
-- are dropped. Replay after a format change is served from the Mongo envelope store, not here.
--
-- NB: an earlier version of this said `TO VOLUME 'default'`, which reads like tiering but moves
-- data to the volume it is already on -- a no-op that quietly kept 3 PB/yr of raw telemetry.
-- With a real multi-disk storage policy configured, swap DELETE for `TO VOLUME 'cold'`.
TTL toDateTime(ts) + INTERVAL 7 DAY DELETE
SETTINGS index_granularity = 8192;

-- Skip index: most alert queries filter on a DTC being present.
ALTER TABLE voltroute.telemetry
    ADD INDEX IF NOT EXISTS idx_dtc dtc TYPE bloom_filter(0.01) GRANULARITY 4;

-- Warm tier: 1-minute rollup. ~98% smaller than raw. Dashboards read THIS, never telemetry.
CREATE TABLE IF NOT EXISTS voltroute.telemetry_1m
(
    fleet_id      UInt32,
    vin           FixedString(17),
    bucket        DateTime('UTC'),
    samples       AggregateFunction(count, UInt64),
    avg_speed     AggregateFunction(avg, Float32),
    max_speed     AggregateFunction(max, Float32),
    min_soc       AggregateFunction(min, Float32),
    last_soc      AggregateFunction(argMax, Nullable(Float32), DateTime64(3, 'UTC')),
    last_lat      AggregateFunction(argMax, Float64, DateTime64(3, 'UTC')),
    last_lon      AggregateFunction(argMax, Float64, DateTime64(3, 'UTC')),
    distance_km   AggregateFunction(max, Float64)
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(bucket)
ORDER BY (fleet_id, vin, bucket)
TTL bucket + INTERVAL 90 DAY;

CREATE MATERIALIZED VIEW IF NOT EXISTS voltroute.telemetry_1m_mv
TO voltroute.telemetry_1m AS
SELECT
    fleet_id,
    vin,
    toStartOfMinute(ts) AS bucket,
    countState()                  AS samples,
    avgState(speed_kmh)           AS avg_speed,
    maxState(speed_kmh)           AS max_speed,
    minState(assumeNotNull(soc_pct)) AS min_soc,
    argMaxState(soc_pct, ts)      AS last_soc,
    argMaxState(lat, ts)          AS last_lat,
    argMaxState(lon, ts)          AS last_lon,
    maxState(odo_km)              AS distance_km
FROM voltroute.telemetry
GROUP BY fleet_id, vin, bucket;

-- Charging sessions, derived at insert time from SoC rising while stationary.
CREATE TABLE IF NOT EXISTS voltroute.charge_session
(
    fleet_id     UInt32,
    vin          FixedString(17),
    started_at   DateTime64(3, 'UTC'),
    ended_at     DateTime64(3, 'UTC'),
    soc_start    Float32,
    soc_end      Float32,
    kwh          Float32,
    tariff_inr   Float32,
    cost_inr     Float32,
    charger_id   Nullable(UInt32),
    was_optimised UInt8                   -- 0 = charged on arrival, 1 = followed our schedule
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(started_at)
ORDER BY (fleet_id, vin, started_at);

-- Centralised application logs. Replaces an ELK stack — one fewer system to operate.
CREATE TABLE IF NOT EXISTS voltroute.app_log
(
    ts       DateTime64(3, 'UTC'),
    service  LowCardinality(String),
    level    LowCardinality(String),
    trace_id String,
    msg      String,
    attrs    Map(String, String)
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (service, ts)
TTL toDateTime(ts) + INTERVAL 30 DAY;
