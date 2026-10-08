# Measured performance

Every number here was produced by running the pipeline, not by estimating it. Where a figure is
extrapolated, the arithmetic is shown and it is labelled as an extrapolation.

**Hardware:** one Windows 11 laptop, 16 vCPU, 7.9 GB allocated to Docker. Redpanda, ClickHouse,
PostgreSQL, MongoDB and Redis all run on this same machine **concurrently with the producers and
consumers being measured**. This is a worst case, not a benchmark rig: in any real deployment the
broker and the database are not competing with the application for the same cores.

Reproduce with `make bench`.

---

## Ingest throughput

### Producers (simulator → Redpanda)

| Processes | Events | Wall time | Aggregate |
| --- | --- | --- | --- |
| 1 | 36,122 | 2.69 s | **13,455 ev/s** |
| 4 (concurrent) | 578,276 | 18.5 s | **~31,800 ev/s** |

### Consumers (Redpanda → normalise → dedup → ClickHouse)

| Processes | Consumed | Inserted | Duplicates | Rejected | Aggregate |
| --- | --- | --- | --- | --- | --- |
| 1 | 72,280 | 72,000 | 280 | 0 | 4,667 ev/s |
| 4 (one group, 64 partitions) | 578,276 | 576,000 | 2,276 | 0 | **24,224 ev/s** |

Per-consumer rate held at ~6,050 ev/s across all four processes — scaling was linear at this
width, which is what the partitioned design predicts.

**Accounting is exact**: 576,000 inserted + 2,276 duplicates = 578,276 consumed. No events were
lost or double-counted.

### The 100,000 events/sec claim, honestly

We do **not** demonstrate 100k ev/s on a laptop; that is not physically available here. What we
measured is **6,050 ev/s per consumer process, scaling linearly to 4 processes** while sharing a
machine with the broker and three databases.

```
100,000 ev/s ÷ 6,050 ev/s per consumer ≈ 17 consumers
topic has 64 partitions → up to 64 consumers in the group
headroom at the measured per-consumer rate ≈ 387,000 ev/s
```

The caveats that make this an extrapolation rather than a result:

- Per-consumer throughput was measured while the same 16 cores ran the broker, ClickHouse,
  Postgres, Mongo, Redis and four producers. Dedicated nodes would raise it, not lower it.
- Linearity is verified to 4 consumers only. Coordination cost grows with group size; we expect
  degradation well before 64.
- ClickHouse insert cost grows with part count. Sustained operation needs merge throughput to
  keep up, which we have not soak-tested.

A five-minute burst test at full rate on cloud hardware is the honest way to close this gap and
is the next benchmark to run.

---

## Storage

Measured on 72,000 real rows:

| Metric | Value |
| --- | --- |
| Uncompressed | 105 bytes/row |
| On disk (ZSTD, columnar) | **8.2 bytes/row** |
| Compression ratio | **12.89 : 1** |

This is better than the 10:1 the capacity model originally assumed, and the reason is worth
stating: the brief's ~1 KB/event figure is the **JSON wire format**. ClickHouse stores typed
columns, not documents, so the 1 KB never reaches disk.

### Revised capacity model

```
wire:    100,000 veh x 1 ev/s x 86,400 s x ~1 KB JSON  = ~8.6 TB/day in flight
at rest: 8.64 x 10^9 events/day x 8.2 bytes           = ~71 GB/day on disk
                                                       = ~25.9 TB/year
```

The original estimate of ~860 GB/day at rest was an order of magnitude too pessimistic because it
applied the wire-format size to the stored row. The hot/warm/cold tiering in
`docs/ARCHITECTURE.md` §6 still holds; it is simply much cheaper than first costed.

---

## Correctness observed under load

| Property | Evidence |
| --- | --- |
| Duplicate suppression | 2,276 duplicates caught; Bloom false-positive rate 0.000 at this volume |
| Tenancy attribution | 8 distinct `fleet_id` values preserved end to end |
| Normalisation | 0 rejects across 578,276 events in three OEM dialects |
| Rollup MV | populated at insert time, one row per vehicle-minute |

### Two bugs this benchmark found

1. **`soh_pct: 100.2`.** Gaussian noise on a healthy pack pushed state of health above 100%, and
   the normaliser's range check correctly rejected 0.3% of the stream to the DLQ. State of health
   is a fraction of original capacity and cannot exceed it. Fixed in the generator, not by
   loosening the check.

2. **Every event landed in `fleet_id` 1.** OEM payloads carry no tenancy — correctly, since an
   OEM does not know our customer IDs — so the consumer was defaulting. Tenant isolation in
   ClickHouse was silently void. Tenancy now rides in Kafka headers, attached by the gateway
   after it resolves ownership, and an event arriving without it is rejected to the DLQ rather
   than stored unattributed.
