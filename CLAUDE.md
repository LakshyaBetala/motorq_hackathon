# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**ReadyLine** — a submission for the Talenciaglobal / Motorq "Connected Vehicle Intelligence"
hackathon. At 06:10 it tells a fleet dispatcher which of her ~250 vehicles will not finish today's
shift, ranked by attributed dollars, and acts on each before the drivers arrive.

`docs/arena-designs/` holds four competing designs generated during planning. They are **reference
material, superseded by `docs/ARCHITECTURE.md`** — read them for depth on a specific section, not
for decisions.

Two source documents in the repo root define the grading and must not be contradicted:
- `Motorq_Hackathon_Problem_Statement.pdf` — the brief, NFR targets, and what reviewers score
- `Motorq_Hackathon_Solution_Document_Template.docx` — the exact submission structure

**Read `docs/ARCHITECTURE.md` before changing anything.** The problem space and stack are locked;
re-opening them wastes the timebox. It also records the known weaknesses we have agreed to state
openly rather than hide — do not quietly "fix" those by overclaiming.

## Commands

```bash
docker compose -f infra/compose/docker-compose.yml up -d    # full stack, local profile
make seed                                                    # seed 100K vehicles into Postgres + ClickHouse
make simulate                                                # start the telemetry simulator
make test                                                    # all suites
make load                                                    # k6 load test, scale profile
```

Single test:
```bash
pytest services/api/tests/test_schedule.py::test_tou_dp_respects_deadline -x   # Python
go test ./internal/dedup -run TestBloomFalsePositiveRate                        # Go
```

## Three things that are easy to get wrong

**1. The 100,000 vehicles are multi-tenant** — ~60 tenants, largest 18,000, one dispatcher owns
~250. Not one giant fleet. Every query, index and API route must carry tenant scope; tenant
isolation is graded.

**2. Rules decide, ML only re-ranks.** VEWS is an additive, transparent 0–3-per-signal score. The
ML model may reorder what the rules already surfaced; it may never surface an alert on its own.
This is the structural defence against synthetic-data circularity — don't "improve" it by letting
the model gate alerts, and never quote offline AUC as a product claim.

**3. The board is hard-capped at 12 alerts per dispatcher per shift.** The 13th increments a
suppressed counter. Raising the cap to improve recall defeats the entire product thesis — the pain
being solved is alarm fatigue, not detection.

## Architecture in one pass

```
vehicles ─┬─ MQTT 5 + mTLS ────────┐
          └─ OEM cloud webhook/poll┘→ Redpanda[telemetry.raw, key=VIN, 64 parts]
                   ├→ ingest(Go)   → normalise → ClickHouse (AP) + MongoDB (raw envelopes)
                   └→ detector(Go) → EWMA → VEWS → alerts → PostgreSQL (CP) + Redis pub/sub
                                                       ↓
                             FastAPI ← Redis(live state, rate limit, Bloom dedup)
                                ↓
                             React + MapLibre (Morning Board)
```

The split that matters: **ClickHouse is AP and holds telemetry; PostgreSQL is CP and holds
fleet/billing/RBAC/audit.** Never put billing or audit rows in ClickHouse, never put raw telemetry
in Postgres — the whole design argument rests on that boundary.

**MongoDB holds un-normalised OEM envelopes** so that when an OEM renames a field via OTA, we fix
the mapping and *replay*. A new dialect is a versioned registry entry promoted out of shadow mode —
not a deploy.

`pgvector` lives inside that same Postgres rather than a separate Qdrant; deliberate, justified by
corpus size.

### Two profiles, one codebase

100K events/sec does not run on a laptop. `local` (100K seeded, ~2K live ev/s) and `scale`
(measured per-partition × partitions) differ **only by environment variables**. If you find
yourself adding a code branch on profile, you have broken 12-factor and the cloud-agnostic claim.

### Service internals

Every service is ports & adapters: `domain/` (pure) ← `app/` (use cases) ← `adapters/` (Kafka, DB,
HTTP). Dependencies point inward only, enforced by an import-linter rule in CI. Domain code
importing a driver or framework will fail the build.

### Keying

Topic key is **VIN**, never `fleet_id` — one 40,000-vehicle customer would hot-spot a single
partition. VIN also gives the per-vehicle ordering that trip segmentation depends on.

## The graded core

The brief weights **testing** and **algorithms** heavily. The nine algorithms in
`docs/ARCHITECTURE.md` §5 (A*, DP charging schedule, DP/PELT trip segmentation, VIN/DTC parsing,
Bloom dedup, Count-Min top-K, union-find depot clustering, VEWS banding) each need pseudocode, a
proven complexity bound, and a **measured** runtime at a stated input size. Claims without a
measurement lose marks.

**The simulator emits a ground-truth fault and cost ledger.** This is what makes the acceptance
tests real rather than smoke tests — they assert against known truth:

| Claim | Target |
| --- | --- |
| precision@12 | ≥ 0.50 |
| recall@72h (telemetry-visible mechanisms) | ≥ 0.30 |
| alerts per dispatcher per shift | ≤ 12 |
| cost-leak ranking, Spearman ρ vs ledger | ≥ 0.85 |
| dollar attribution vs ledger, per vehicle-month | within ±15% |

Keep the ledger out of every read path the product uses; it exists only for tests and back-tests.
If production code reads it, the metrics become meaningless.

Coverage target is 80%+ on core services and algorithms, with integration tests against real
infrastructure via Testcontainers — not mocks.

## Conventions

- All data is synthetic. Never commit anything resembling real VINs, owner data, or location traces.
- Secrets go in `.env` (gitignored); `.env.example` stays current.
- Protobuf schema changes must pass the CI compatibility check — schema evolution is part of the grade.
- Final submission is tagged `v1.0-submission`.
