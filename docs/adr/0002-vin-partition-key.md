# ADR-0002: Partition telemetry by VIN, not fleet_id

**Status:** Accepted · 2026-10-08

## Context

`telemetry.raw` needs a partition key. The obvious candidate is `fleet_id`, since almost every
query is tenant-scoped and co-locating a tenant's data looks efficient.

Our tenancy model makes that a trap: ~60 tenants, the largest holding 18,000 vehicles and the
median 900. Keying by `fleet_id` routes 18% of all traffic — roughly 18,000 events/sec — onto a
single partition, and a Kafka partition is consumed by exactly one consumer in a group. That
partition becomes the throughput ceiling of the whole platform, and horizontal scaling cannot help.

## Options considered

1. **`fleet_id`.** Tenant locality. Rejected: guarantees a hot partition proportional to the
   largest customer — who is also the customer we can least afford to degrade.
2. **Round-robin / no key.** Perfect balance. Rejected: destroys per-vehicle ordering, and both
   trip segmentation (A3) and sequence-based de-duplication (A6) require one vehicle's events to
   arrive in order on one partition.
3. **`VIN`.** Chosen.
4. **Composite `fleet_id:VIN`.** Same balance as VIN, larger key, no added benefit.

## Decision

Key by **VIN**, 64 partitions.

VINs distribute effectively uniformly across the hash space, so load balances regardless of how
lopsided tenant sizes are. VIN also preserves per-vehicle ordering, which is what the algorithms
actually depend on — tenant-level ordering was never required by anything.

## Consequences

- A tenant-scoped query reads from all 64 partitions rather than one. Acceptable: those queries
  are served from ClickHouse, ordered by `(fleet_id, vin, ts)`, not from Kafka.
- Repartitioning later would break ordering mid-flight, so 64 is chosen with headroom rather than
  sized to today's load.
- A single pathological vehicle (a stuck sensor emitting at 1 kHz) can still hot-spot one
  partition. The ingest service rate-limits per VIN to bound this.
