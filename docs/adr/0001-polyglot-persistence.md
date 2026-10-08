# ADR-0001: Polyglot persistence, split by consistency need

**Status:** Accepted · 2026-10-08

## Context

Telemetry arrives at ~100,000 events/sec (~8.6 TB/day raw). The same platform must also hold fleet
ownership, subscriptions, billing, access control and audit logs, where a divergent read is a
commercial or legal incident. A single store cannot serve both: B-tree write amplification
collapses under the telemetry write rate, while a store tuned for that rate cannot give us
transactional guarantees.

## Options considered

1. **PostgreSQL alone** (with TimescaleDB). One system to operate, full SQL, real transactions.
   Rejected: this is precisely the write ceiling we are trying to escape, and long analytical scans
   would starve the dashboard queries sharing the primary.
2. **Cassandra alone.** Handles the write rate. Rejected: no cheap analytics, and no transactions
   for billing, so we would end up adding a relational store anyway.
3. **ClickHouse alone.** Handles writes and analytics. Rejected: no foreign keys, no real
   transactions, no row-level security — unacceptable for billing and audit.
4. **ClickHouse + PostgreSQL + MongoDB + Redis**, split by consistency requirement. Chosen.

## Decision

| Data | Store | CAP | Reasoning |
| --- | --- | --- | --- |
| Raw telemetry | ClickHouse | AP | A replica loss must never block ingest |
| Raw OEM envelopes | MongoDB | AP | Schema-less landing zone; makes format-change replay possible |
| Live vehicle state | Redis | AP | Stale by seconds is fine for a map pin |
| Fleet, billing, RBAC, assignments | PostgreSQL | CP | A double invoice or stale permission is unacceptable |
| Audit log | PostgreSQL, append-only | CP | Compliance evidence must be totally ordered |
| Embeddings | pgvector, same PostgreSQL | CP | Corpus is thousands of docs, not billions |

PACELC: for the AP stores, **else-latency** — we accept staleness to keep write latency flat. For
PostgreSQL, **else-consistency** — we accept latency to keep reads correct.

## Consequences

- Four stores to operate, back up and monitor. Mitigated by all four being self-hostable with no
  managed-service dependency, which also buys the cloud-agnosticism the brief rewards.
- No cross-store transactions. Writes spanning ClickHouse and PostgreSQL use the outbox pattern
  with at-least-once delivery plus an idempotency key, never a distributed transaction.
- Analysts must learn two SQL dialects.
- pgvector rather than a dedicated Qdrant is a bet that the corpus stays small. If bulletin volume
  grows by two orders of magnitude, revisit.
