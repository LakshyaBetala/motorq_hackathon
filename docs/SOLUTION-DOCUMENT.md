# Connected Vehicle Intelligence Hackathon — Solution Document

**Team Name:** Lakshya Betala (solo entry)
**Team Members & Roles:** Lakshya Betala — sole contributor: problem framing, architecture, implementation, testing and documentation — lakshbeta2006@gmail.com
**Problem Space Chosen:** EV charging and battery health — specifically the nightly depot charging commitment for mixed ICE/EV fleets
**Repository URL:** https://github.com/LakshyaBetala/motorq_hackathon
**Demo Video URL (≤ 5 min):** _[fill in]_
**Date of Submission:** 13/10/2026

---

## 1. Executive Summary

**The problem.** A depot charging operations manager running 400–1,200 mixed vehicles has no way
to commit a nightly charging plan. Drivers plug in at shift end (17:00–21:00, the on-peak
window) and every charger draws full power at once. Energy is bought at the most expensive hour,
the simultaneous draw sets the monthly demand charge, packs sit at 100% SoC for nine hours
calendar-ageing, and vehicles still get swapped in the morning because nobody knew their state
of charge would not cover tomorrow's route.

**What we built.** ReadyLine commits tonight's depot plan by 16:00 — which vehicle charges on
which charger, starting when, to what SoC — so every route tomorrow completes, under the site's
electrical cap, at minimum cost. The Morning Board then shows only the *exceptions* to that
commitment, hard-capped at 12 cards.

**Measured results** (all reproduced by `make` targets, not estimated):

| Result | Value | How |
| --- | --- | --- |
| Charging cost vs charging on arrival | **60.8% lower** | Live API call, 40-vehicle depot |
| Distance from proven optimum | **0.00% duality gap** | Lagrangian lower bound, cross-checked against brute force |
| Saving, randomised holdout | **186.35 per 100 units of energy, 95% CI [152.72, 219.98], p < 0.0001** | 108 treatment vs 12 control, difference-in-differences |
| Ingest throughput | **24,224 events/sec** on 4 consumers, 6,050/consumer | 578,276 events, exact accounting |
| Storage compression | **12.89 : 1**, 8.2 bytes/row | 72,000 real rows |
| Seed time, 100,000 vehicles | **4.88 s** | PostgreSQL `COPY` |
| Tests | **200 passing, 88% coverage** | Target was 80% |

**What is distinctive.** Three things, each of which exists to make a claim falsifiable rather
than merely impressive:

1. **A randomised holdout arm.** 10% of vehicles are randomised at onboarding, charge unmanaged,
   and receive no recommendations. The reported saving is a difference-in-differences with a
   t-test and confidence interval, served from an API endpoint. Both arms run against the same
   simulator, so generator bias cancels in the difference — the only reason a number measured on
   synthetic data means anything.
2. **A depot-level scheduler with a provable bound.** Charging under a shared site power cap is
   NP-hard. Lagrangian relaxation prices depot power as a congestion charge and yields a *lower
   bound*, so we state how close to optimal the plan provably is instead of asserting it is good.
3. **Rules decide; ML only re-ranks.** And we report that the re-ranker **lost** — precision@12
   0.656 for rules vs 0.625 for the model — so it ships disabled.

---

## 2. Problem Statement & Validation

### 2.1 Problem Statement

> A **depot charging operations manager** needs a way to **commit a nightly charging plan that
> guarantees tomorrow's routes**, because today every vehicle charges on arrival at the on-peak
> rate and nobody knows which vehicles will fall short — which costs avoidable energy spend, a
> demand charge set by the simultaneous draw, accelerated pack degradation, and 2–4 unplanned
> vehicle swaps per week.

- **Primary user:** depot charging operations manager. 400–1,200 mixed vehicles, 3–9 depots,
  60–180 AC chargers per depot, and a hard site electrical capacity limit.
- **Secondary stakeholders:** dispatcher (morning exceptions), fleet finance (cost per vehicle),
  sustainability (kWh and emissions per route), OEM platform team (onboarding a new data format).

### 2.2 Evidence & Validation

| Evidence / Assumption | Source or Method | What It Shows | Confidence |
| --- | --- | --- | --- |
| Connected cars reach ~25 GB/hour by 2030; ~95% of new vehicles connected | Brief, citing McKinsey and S&P | The volume is real and forces the architecture | High |
| Energy is 25–30% of fleet TCO and the largest controllable line | Industry assumption, stated not measured | The problem is worth solving | **Low — unvalidated** |
| Time-of-use spread is large enough to arbitrage | Our tariff model has a 3.2× peak-to-trough ratio | Shifting load has headroom | Medium |
| Optimised charging beats charging on arrival | **Our own randomised holdout**, p < 0.0001 | The mechanism works | High (on simulated data) |
| Per-vehicle optimisation overloads the site | Measured: 10 vehicles × 20 kW all chose the same hour, 200 kW against a 60 kW cap | The shared cap is the real constraint | High |

**Existing alternatives and why they fall short.** OEM portals show one brand each, with no
cross-fleet view and no scheduling. Aftermarket OBD-II dongles cost $8–15/vehicle/month and
cannot see pack-level data. Generic telematics dashboards report what happened rather than
committing a plan. Smart-charging products from charger vendors optimise per charger, not across
a depot under one electrical cap — which is the constraint that actually binds.

**The honest part.** We validated the *mechanism* rigorously and the *market* not at all. We have
not interviewed a depot manager. Everything in §2.3 about money is modelled.

### 2.3 Impact & Success Metrics

| Metric | Baseline Today | Target | How Measured |
| --- | --- | --- | --- |
| Energy cost per unit charged | charge-on-arrival | **−60.8% in the plan; −186.35/100 units measured in the holdout** | Live API + randomised experiment |
| Site peak demand | N × max_kW simultaneous | within the configured cap, 100% of runs | Every plan asserts `cap_respected` |
| Routes failing for charge shortfall | 2–4 per week (assumed) | 0 committed-route failures | Simulator replay; a missed route is a hard test failure |
| Distance from optimal | unknown | duality gap reported on every plan | Lagrangian lower bound |

**Scale of impact.** The measured saving is a rate, not a total, and we deliberately do not
multiply it up to a headline annual figure — that multiplication is where credibility goes to
die. At 10,000 EVs the mechanism is the same as at 100; what changes is the number of depots
solved in parallel, and the scheduler is per-depot by construction.

**Wider impact.** Shifting load off-peak reduces both cost and grid carbon intensity, and
avoiding 100%-SoC dwell extends pack life, which is the largest embedded-carbon asset in an EV.
Neither is claimed as measured.

---

## 3. Solution Description

### 3.1 Solution Overview & User Journey

**What it does.** Every afternoon, ReadyLine reads each returning vehicle's state of charge and
tomorrow's assigned route, then solves one optimisation for the whole depot: which vehicle draws
how much power in each half-hour slot, under the site cap, before each vehicle's departure
deadline, at minimum tariff cost. It commits that plan. Overnight, telemetry streams in and
vehicle readiness is scored. At 06:10 the dispatcher sees only the exceptions.

**User journey:**

```
vehicle returns → telemetry (MQTT / OEM cloud) → normalise → ClickHouse
                                                      ↓
  16:00  depot scheduler (A0) commits tonight's plan under the site cap
                                                      ↓
  overnight  VEWS scores readiness from streaming signals
                                                      ↓
  06:10  Morning Board shows ≤12 exceptions, each with evidence and an action
                                                      ↓
  dispatcher acts (swap / book bay) — or asks the copilot, which proposes and never executes
```

**Screenshot:** `docs/screenshots/depot-commitment.png`

### 3.2 Key Value Proposition

- **Customer job:** commit a plan that guarantees tomorrow's routes at the lowest energy cost.
- **Pain relieved:** no more paying peak rates by default, no more demand charges set by
  accident, no more morning surprises.
- **Gain created:** a *provable* plan — the duality gap says how close to optimal it is — and a
  *measured* saving with a confidence interval rather than a vendor estimate.
- **Differentiation:** per-charger smart charging optimises the wrong unit. The depot's
  electrical capacity is shared and contended, which makes this a scheduling problem, not N
  independent ones.

### 3.3 Innovative Ideas

**1. The randomised holdout arm.** 10% of vehicles, stratified by depot, assigned by a
pre-registered hash of the VIN, charge unmanaged forever. Savings are difference-in-differences
with a Welch t-test, exposed at `/api/v1/experiments/charging-arbitrage`.
*What is new:* telematics vendors report modelled savings. We report a measured one with an
interval, and the endpoint **refuses to return a number when it cannot estimate one**.
*Evidence:* control arm moved +0.0012 between periods against −186.35 for treatment. A
near-zero control change is what makes the difference attributable.

**2. Lagrangian relaxation with a reported duality gap.** The shared power cap is dualised into a
time-varying congestion price; each vehicle then solves independently, and a subgradient step
raises the price where the cap is breached.
*What is new:* the lower bound. We can state "this plan is within X% of the true optimum" and
show the arithmetic, rather than asserting a heuristic is good.
*Evidence:* 0.00% gap on the demo instance; cross-checked against exhaustive search on small
instances; tested that the gap is reported as `None` when load had to be shed.

**3. Prompt-injection screening on tool results, not just prompts.** The copilot screens every
tool result with Meta's `llama-prompt-guard-2`, because hostile text arrives from a database
field another user wrote, not from the dispatcher.
*Evidence:* 0.0004 on benign input, 0.998 on an attack; a test asserts the model never sees a
poisoned tool result.

---

## 4. Feature List

| ID | Feature | User Story | Priority | Status | Code Path |
| --- | --- | --- | --- | --- | --- |
| F-01 | Depot commitment under a site power cap | As a depot manager I want one plan for the whole site so I never trip the main breaker | Must | **Done** | `services/api/readyline/algorithms/depot.py` |
| F-02 | Nearest reachable charger (A*) | As a dispatcher I want to know where a low-SoC vehicle can still reach | Must | **Done** | `.../algorithms/energy.py` |
| F-03 | Single-vehicle ToU schedule (DP) | …so each vehicle charges in its cheapest feasible window | Must | **Done** | `.../algorithms/energy.py` |
| F-04 | Randomised holdout + DiD endpoint | As a finance analyst I want a measured saving, not a modelled one | Must | **Done** | `.../experiment.py` |
| F-05 | 100k-vehicle multi-tenant simulator | …so the platform is exercised at the stated scale | Must | **Done** | `services/simulator/readyline_sim/` |
| F-06 | Telemetry stream with bursts, dupes, out-of-order | …so the pipeline is tested on realistic input | Must | **Done** | `.../telemetry.py` |
| F-07 | Multi-OEM normalisation registry (3 dialects) | As a platform engineer I want to onboard an OEM without a deploy | Must | **Done** | `services/api/readyline/normalise.py` |
| F-08 | Ingest: dedup, DLQ, batched ClickHouse insert | …so at-least-once delivery doesn't double-count | Must | **Done** | `services/ingest/consumer.py` |
| F-09 | Tenant isolation (FORCE RLS + app role) | As a customer I need certainty another tenant cannot see my fleet | Must | **Done** | `services/api/migrations/002_security.sql` |
| F-10 | VEWS readiness score | As a dispatcher I want a reason I can disagree with, not a probability | Must | **Done** | `.../algorithms/vews.py` |
| F-11 | Morning Board capped at 12 | …so I am not back to 40 emails a day | Must | **Done** | `.../algorithms/vews.py`, `web/src/App.tsx` |
| F-12 | Secure API: OIDC, RBAC, keyset pagination, rate limit | | Must | **Done** | `services/api/readyline/api/` |
| F-13 | Web UI: plan, load-vs-cap chart, CI visualisation | | Must | **Done** | `web/src/App.tsx` |
| F-14 | Charging copilot with 4 guardrails | As a dispatcher I want to ask questions in plain English, safely | Should | **Done** | `services/agent/copilot.py` |
| F-15 | ML re-ranker, evaluated honestly | | Should | **Done (ships disabled)** | `.../reranker.py` |
| F-16 | Trip segmentation, Bloom dedup, CMS top-K, geohash clustering | | Should | **Done** | `.../algorithms/` |
| F-17 | Helm chart, Terraform, STRIDE model | | Should | **Written, not deployed** | `infra/`, `docs/THREAT-MODEL.md` |
| F-18 | Location masking by scope | As a driver I want my precise location not visible to everyone | Should | **Not built** | — |
| F-19 | Multi-region failover | | Won't (this timebox) | **Not built** | — |

---

## 5. Solution Architecture (High-Level Design)

### 5.1 Architecture Overview

**C4 L1 — system context.** ReadyLine sits between OEM clouds and vehicles on one side, and the
depot manager, dispatcher and finance analyst on the other. External systems: OEM telematics
clouds (HTTPS webhook + poll), vehicles (MQTT 5 + mTLS), Keycloak (OIDC), Groq (LLM, HTTPS),
and S3-compatible object storage for the cold tier.

**C4 L2 — containers.**

```
vehicles ──MQTT 5 + mTLS──┐
OEM clouds ──HTTPS poll───┴─▶ ingest gateway ──Kafka proto──▶ Redpanda
                                                              [telemetry.raw, key=VIN, 64 parts]
                                   ┌──────────────────────────────┴──────────────┐
                        ingest consumer (xN)                          detector
                        normalise → dedup → batch                     EWMA → VEWS
                                   │                                          │
                     ┌─────────────┴────────────┐                   alerts topic
                     ▼                          ▼                             │
              ClickHouse (AP)            MongoDB (AP)                          ▼
           telemetry + rollup MVs     raw OEM envelopes            PostgreSQL (CP)
                     │                   (replay source)        fleet/billing/RBAC/audit
                     └──────────┬────────────────────────────────────┘
                                ▼
                     FastAPI ──▶ Redis (live state, rate limit, Bloom)
                        │   └──▶ Groq (copilot, HTTPS)
                        ▼
                  React + MapLibre
```

**Latency budget, ingest to dashboard (target < 2 s):**

| Hop | Protocol | Budget |
| --- | --- | --- |
| vehicle → gateway | MQTT 5 / mTLS | 150 ms |
| gateway → Redpanda | Kafka, lz4, linger 20 ms | 40 ms |
| Redpanda → consumer | Kafka fetch | 200 ms |
| normalise + dedup | in-process | < 5 ms |
| batch → ClickHouse | HTTP insert, 2 s max linger | ≤ 2,000 ms worst case |
| ClickHouse → API | HTTP, rollup MV read | 60 ms |
| API → browser | WebSocket / HTTP | 50 ms |

The batch linger dominates and is the honest reason the dashboard figure is "≤ 2 s worst case,
typically ~500 ms". Critical alerts bypass the batch entirely via the alerts topic and Redis
pub/sub, which is how the < 5 s critical-alert target is met independently of insert batching.

### 5.2 Technology Stack & Justification

| Layer | Choice | Why this, and what we rejected |
| --- | --- | --- |
| Transport | MQTT 5 (EMQX) + OEM HTTP connectors | Vehicles speak MQTT and it gives mTLS device identity; Motorq's model is device-free OEM clouds. Both paths are real and their latency profiles differ. |
| Log | **Redpanda** | Kafka API-compatible so there is zero client lock-in, but a single binary with no ZooKeeper — the difference between a demo that boots in 40 s and one that doesn't. Rejected Kafka (heavy locally), RabbitMQ (no replay, no partitioned ordering). |
| Telemetry store | **ClickHouse** | Columnar, measured 12.89:1 compression, materialised views roll up at insert so dashboards read pre-aggregated rows. Rejected Cassandra (no cheap analytics), TimescaleDB (same write ceiling we're escaping). |
| Relational | **PostgreSQL 16 + pgvector** | 3NF core, FORCE RLS for tenant isolation, append-only audit. pgvector in the same instance rather than a separate Qdrant — a few thousand documents does not justify another service. |
| Raw envelopes | **MongoDB** | Schema-less landing zone so a mid-flight OEM format change can be fixed and **replayed**. This is the genuine NoSQL justification, not a checkbox. |
| Cache | **Redis** | Live state, rate limits, Bloom dedup, pub/sub for critical alerts. |
| Backend | **FastAPI** (Python) | OpenAPI for free, and it sits next to the ML and agent code. |
| Frontend | **React + Vite + TypeScript, MapLibre** | MapLibre over Google Maps: no API key, no vendor lock. |
| Auth | **Keycloak** (OIDC) | Self-hosted, runs on any cloud. Rejected Auth0/Cognito — the brief rewards cloud-agnosticism. |
| ML | scikit-learn GBT | Twelve engineered features and a few thousand rows. A neural network would be slower, less explainable, and no more accurate. |
| Agent | Groq (`gpt-oss-120b`) + `llama-prompt-guard-2` | Rejected LangGraph: the graph is one bounded loop, and a framework would add a dependency without changing behaviour. |
| Observability | OpenTelemetry → Prometheus + Grafana; logs → ClickHouse | Logs in ClickHouse is one fewer system than ELK, and we already operate it. |
| Deploy | Docker Compose / Helm / Terraform | Plain Kubernetes with no managed service on the critical path. |

### 5.3 Data Architecture

**ER / 3NF core** (`services/api/migrations/001_core.sql`): `tenant`, `app_user`, `role`,
`user_role`, `depot`, `fleet`, `oem`, `vehicle_model`, `vehicle`, `driver`,
`driver_assignment`, `shift`, `trip`, `charger`, `tariff_band`, `rule`, `alert`,
`alert_feedback`, `work_order`, `subscription`, `audit_log`, `bulletin`, `incident_signature`.

Two deliberate denormalisations, both documented in the schema:
1. `alert.dollars_at_risk` is stored rather than recomputed — the Morning Board ranks by it on
   every poll, and recomputing a cost model across 100k vehicles per request would blow the p95
   budget. Recomputed nightly in batch.
2. `fleet_id` is carried on telemetry rows in ClickHouse so tenant-scoped scans are a contiguous
   range read rather than a join.

**Polyglot map and CAP:**

| Data | Store | CAP | PACELC reasoning |
| --- | --- | --- | --- |
| Raw telemetry | ClickHouse | **AP** | A replica loss must never block ingest. Else-Latency. |
| Raw OEM envelopes | MongoDB | **AP** | Replayable landing zone. |
| Live vehicle state | Redis | **AP** | Seconds-stale is fine for a map pin. |
| Fleet, billing, RBAC, assignments | PostgreSQL | **CP** | A double invoice or a stale permission is unacceptable. Else-Consistency. |
| Audit log | PostgreSQL, append-only | **CP** | Compliance evidence must be totally ordered. |

**Partitioning.** `telemetry.raw` keyed by **VIN**, 64 partitions (ADR-0002). Keying by
`fleet_id` would route 18% of traffic — the largest tenant — onto one partition, and a partition
is consumed by exactly one group member, so that becomes the platform's throughput ceiling. VIN
hashes uniformly and preserves the per-vehicle ordering that trip segmentation and sequence
dedup require. ClickHouse: `PARTITION BY toYYYYMM(ts) ORDER BY (fleet_id, vin, ts)`.

**Capacity, revised from measurement:**

```
wire:     100,000 veh × 1 ev/s × 86,400 s × ~1 KB JSON  = ~8.6 TB/day in flight
at rest:  8.64 × 10⁹ events/day × 8.2 bytes measured    = ~71 GB/day  →  ~25.9 TB/year
```

Our first estimate of ~860 GB/day at rest was an order of magnitude pessimistic: it applied the
JSON wire size to the stored row, but ClickHouse stores typed columns, so the 1 KB never reaches
disk. Hot 7 days full resolution; warm 90 days at 1-minute rollup; cold 7 years as Parquet on
S3 with a lifecycle policy. Retention is a partition `DROP`, not a row delete.

**Query optimisation:**

| Query | Change made | Effect |
| --- | --- | --- |
| Morning Board (open alerts, ranked) | Partial index `WHERE state='OPEN'` on `(tenant_id, severity, dollars_at_risk DESC)` | Index covers a few thousand open rows instead of all alerts ever raised |
| Vehicle list pagination | Keyset (`WHERE vin > ?`) instead of `OFFSET` | `OFFSET` re-scans and discards every skipped row, so page 10,000 costs 100× page 1; keyset is flat in depth |
| 24 h vehicle telemetry | Read `telemetry_1m` rollup MV, never raw | ~1,440 pre-aggregated rows instead of ~86,400 |

> **Gap, stated:** we have **not** captured `EXPLAIN ANALYZE` before/after output. The index and
> pagination choices are principled and the rollup is measured, but the brief asks for query
> plans and we do not have them. This is a known deliverable gap.

### 5.4 Deployment View

Docker Compose for local (one command, nine services). Helm for Kubernetes: API with HPA 3→20
and a PodDisruptionBudget, ingest sized at one consumer per ~6,050 ev/s measured and capped at
the partition count, web behind a Service, and a default-deny egress NetworkPolicy on ingest.
Terraform provisions EKS across three AZs with a private control plane and an S3 cold tier with
lifecycle tiering.

**Cloud-agnostic approach.** Nothing in the application talks to a cloud provider API. No managed
service is on the critical path — we deliberately do not use RDS, MSK or ElastiCache, each of
which would be operationally better and would weld us to AWS. Porting is a sibling Terraform
directory producing the same four outputs; the Helm chart, images and application code are
unchanged. `PROFILE` changes values only, never behaviour.

---

## 6. Low-Level Design

### 6.1 Layering & Separation of Concerns

Ports and adapters. `domain/` (pure) ← `app/` (use cases) ← `adapters/` (Kafka, DB, HTTP).

| Layer | Responsibility | Must not |
| --- | --- | --- |
| Presentation / API | HTTP, validation, auth, DTO mapping | Contain business rules or SQL |
| Application | Use cases, transactions, orchestration | Depend on a specific database or broker |
| Domain | Algorithms, scoring, entities | Import framework or infrastructure code |
| Infrastructure | Repositories, broker clients, external APIs | Leak vendor types into the domain |

The algorithm modules (`readyline/algorithms/`) are the domain layer and import nothing but the
standard library and, in one case, scikit-learn. That is what makes 200 tests runnable without
infrastructure.

```
services/
  simulator/readyline_sim/   fleet, faults, telemetry, charging, stream, seed
  ingest/                    consumer.py, schema/
  api/readyline/             algorithms/, api/, normalise.py, experiment.py, reranker.py
  agent/                     copilot.py, tools.py
web/src/                     App.tsx, api.ts, styles.css
infra/                       compose/, helm/, terraform/
docs/                        ARCHITECTURE, BENCHMARKS, THREAT-MODEL, SOLUTION-DOCUMENT, adr/
tests/                       9 suites, 200 tests
```

### 6.2 Design Principles Applied

- **SOLID.** Single responsibility: `depot.py` schedules, `energy.py` solves one vehicle,
  `experiment.py` measures — none knows about HTTP. Dependency inversion: `Tool` is a protocol
  the agent loop depends on, so tools can be swapped in tests without a database.
- **12-Factor.** Config strictly in the environment; `PROFILE` changes values, never code paths.
  Stateless API and ingest processes; disposability proven by the consumer committing offsets
  only after ClickHouse acknowledges, so a kill mid-batch replays safely.
- **Idempotency.** `(vin, seq)` dedup via Bloom filter; `alert.dedup_key` unique constraint.
- **Fail-fast.** Normalisation rejects out-of-range values to the DLQ rather than guessing.
- **Least privilege.** `readyline_app` owns nothing and holds no DDL rights.
- **Fail-closed, and fail-open where that is the right trade.** Unscoped DB connections see zero
  rows. The injection classifier fails *open*, because the allowlist and human approval gate
  still hold and taking the copilot down for a defence-in-depth component is worse.

### 6.3 Design Patterns Used

| Pattern | Problem it solves here | Location |
| --- | --- | --- |
| Adapter | Maps three OEM payload dialects to one canonical event | `normalise.py` |
| Registry | A dialect is versioned data, so onboarding an OEM is not a deploy | `normalise.DIALECTS` |
| Strategy | Managed vs unmanaged charging share an interface, which is what makes the holdout comparable | `depot.plan_depot` / `unmanaged_plan` |
| Repository | Tool functions isolate SQL from the agent loop | `agent/tools.py` |
| Outbox-style | Offsets commit only after the sink acknowledges | `ingest/consumer.py` |
| Circuit-breaker-ish | Guard failure degrades to allowlist-only rather than hard-failing | `copilot.injection_score` |

### 6.4 Interfaces, Contracts & Runtime Flows

**API contract.** OpenAPI at `/openapi.json`, served by FastAPI. Versioned path prefix
`/api/v1`. Keyset pagination via opaque `next_cursor`. Errors as `{"detail": "..."}` with
standard status codes. Rate limit 600 req/min per principal with `Retry-After` on 429.

**Event schemas.** Topic `telemetry.raw`, key = VIN, 64 partitions, 7-day retention. Platform
metadata (`dialect`, `tenant_id`, `fleet_id`) travels in **headers**, never in the body, so the
body stays the OEM's bytes verbatim and can be replayed through a corrected mapping. Schema
evolution: a new dialect is a registry entry promoted out of shadow mode after
`shadow_compare()` shows an empty or explained diff against live traffic.

**Flow 1 — happy path.** Vehicle → MQTT → gateway resolves ownership from the mTLS certificate
and attaches tenancy → Redpanda (keyed by VIN) → consumer normalises per dialect → Bloom dedup
on `(vin, seq)` → buffered → batch insert to ClickHouse → MV rolls up at insert → offsets commit
→ dashboard reads the rollup.

**Flow 2 — failure path: an OEM renames a field mid-flight.** The normaliser's required-field
check fails → the event is rejected to `telemetry.dlq` **with the raw bytes and a reason header**
→ the raw envelope is already in MongoDB → an engineer adds a registry entry → `shadow_compare`
runs it against live traffic → once the diff is clean the mapping is promoted → the DLQ is
replayed through the corrected mapping. No data is lost and no deploy is required.

This is not hypothetical: the DLQ caught a real defect on first run (`soh_pct: 100.2`, state of
health above 100%), and 0.3% of the stream was correctly quarantined rather than silently stored.

### 6.5 Algorithms & Data Structures

| # | Problem | Algorithm | Time | Space |
| --- | --- | --- | --- | --- |
| **A0** | Depot commitment under a shared power cap | **Lagrangian relaxation**; per-vehicle subproblem solved exactly by price-sorted greedy | O(K·N·T log T), K≈24 | O(N·T) |
| A1 | Nearest *reachable* charger given SoC | **A\*** over energy, admissible heuristic, energy-feasibility edge pruning | O(E log V) | O(V) |
| A2 | Cheapest single-vehicle ToU schedule | **DP** over (slot × SoC bucket) | O(T·S·R) | O(T·S) |
| A3 | Noisy GPS → stops and trips | **Viterbi DP** with switch penalty | O(n) | O(n) |
| A4 | VIN validation | Regex + ISO 3779 check digit | O(1) | O(1) |
| A5 | DTC parsing | Regex + severity map | O(1) | O(1) |
| A6 | Duplicate suppression | **Bloom filter**, Kirsch-Mitzenmacher double hashing | O(k), k=7 | m bits, n-independent |
| A7 | Top-K cost leaks | **Count-Min Sketch** + bounded candidate set | O(d) update | O(w·d) |
| A8 | Unapproved-depot clustering | **Geohash-7 + union-find** | ~O(n·α(n)) | O(n) |
| A9 | VEWS banding | Additive band table over EWMA | O(1)/event | O(1) |

**A0 in detail — why it is the centrepiece.** Scheduling N vehicles onto a shared power cap with
hard deadlines is NP-hard (it contains bin-packing). Both naive approaches fail measurably:

- *Greedy by deadline* fills the cheap window with whoever departs first, then forces the rest
  onto peak.
- *N independent DPs* is optimal per vehicle and infeasible in aggregate — measured: ten
  vehicles at 20 kW all chose the same hour and drew 200 kW against a 60 kW cap.

```
λ(t) ← 0 for all slots
repeat K times:
    for each treatment vehicle v:                       # exactly solvable
        sort slots before v.deadline by (tariff + λ)
        fill greedily at max rate until energy need met # continuous knapsack: optimal
    load(t) ← Σ power + reserved control-arm load
    dual   ← Σ cost + Σ λ(t)·load(t)·h − Σ λ(t)·cap·h   # valid LOWER BOUND
    if load(t) ≤ cap for all t: break
    λ(t) ← max(0, λ(t) + step/iteration · (load(t) − cap))
repair: shed proportionally, then re-place shed energy into slots with headroom
```

The per-vehicle subproblem is solved by **greedy, not DP**, and that is load-bearing: the DP
discretises SoC into buckets, so its optimum is the best point *on that grid* and strictly
costlier than the continuous optimum. Summing those inflated costs produced a "lower bound" the
continuous repair could legitimately beat — i.e. not a bound at all. A test comparing committed
cost against the bound caught it.

**Scale tested.** 120 vehicles × 24 half-hour slots, cap respected, converged in ≤24 iterations.
Cross-checked against exhaustive search on small instances.

---

## 7. Non-Functional Requirements & Performance Benchmarks

| NFR | Target | Achieved | How measured |
| --- | --- | --- | --- |
| Ingest throughput | 100,000+ ev/s | **24,224 ev/s on 4 consumers; 6,050/consumer, linear** | 578,276 events, exact accounting |
| 3× burst, no loss | survive 5 min | Burst injected by the simulator; **0 events lost** | 576,000 + 2,276 dupes = 578,276 consumed |
| Ingest → dashboard | < 2 s | ≤ 2 s worst case (batch linger), ~500 ms typical | Hop budget §5.1 |
| Critical alert | < 5 s | Bypasses batching via alerts topic + Redis pub/sub | Design; **not load-tested** |
| API p95 / p99 | < 200 / 500 ms | **Not measured** | — |
| Resilience | recovers after broker/pod kill | Offsets commit after sink ack; replay absorbed by dedup | Design + unit tests; **chaos test not run** |
| Availability | 99.9%, no SPOF | 3 AZs, HPA, PDB in Helm | **Not deployed** |
| Security | OIDC, RBAC, tenant isolation, TLS, secrets | RLS verified at DB **and** HTTP layers | 10 isolation tests; CI fails if they skip |
| Compliance | audit every access and agent action | Hash-chained append-only log | Tamper test |

**Load test setup.** 4 producer and 4 consumer processes on one Windows laptop (16 vCPU, 7.9 GB
to Docker) running Redpanda, ClickHouse, PostgreSQL, MongoDB and Redis *concurrently*. This is a
worst case, not a benchmark rig.

**The 100k claim, honestly.** We do not demonstrate 100,000 ev/s. We measured 6,050 ev/s per
consumer, linear to 4 consumers, while sharing cores with the broker and four databases:

```
100,000 ÷ 6,050 ≈ 17 consumers;  topic has 64 partitions
headroom at the measured rate ≈ 387,000 ev/s
```

Caveats: linearity verified only to 4 consumers; coordination cost grows with group size; and
ClickHouse merge throughput has not been soak-tested. A five-minute burst on cloud hardware is
the honest way to close this and is the next benchmark to run.

**Results that are measured:** 12.89:1 compression at 8.2 bytes/row; 100,000 vehicles seeded in
4.88 s; depot plan 60.8% cheaper with a 0.00% duality gap; holdout saving 186.35 per 100 units,
95% CI [152.72, 219.98], p < 0.0001.

---

## 8. Security & Compliance

Full model in `docs/THREAT-MODEL.md`. Top five threats, each with its control and the test:

| # | Threat | Control | Evidence |
| --- | --- | --- | --- |
| T1 | Spoofed vehicle injects telemetry | mTLS identity; gateway attaches tenancy; untenanted events rejected to DLQ | Found in benchmarking — events were defaulting to `fleet_id` 1 |
| T2 | Cross-tenant read or write | **FORCE** RLS + non-owner app role + `WITH CHECK` + per-transaction scope | 10 tests; CI fails if they skip |
| T3 | Prompt injection redirects the copilot | Classifier on prompts **and tool results**; write tools proposed not executed; tenant injected by caller | 11 tests; 0.0004 benign vs 0.998 attack |
| T4 | Location / driver data disclosure | Crypto-shredding for PII; temporal assignment bridge; egress NetworkPolicy | Partial — masking by scope **not built** |
| T5 | Repudiation of an agent action | Hash-chained append-only audit, enforced by trigger *and* privilege | Tamper test |

**Authentication and authorisation.** OIDC via Keycloak; RBAC roles `DISPATCHER`, `FLEET_ADMIN`,
`FINANCE`, `AUDITOR`. Tenant isolation at the database, not the application.
**Known gap:** JWKS signature verification is **not implemented**. The code fails closed — with
`ALLOW_HEADER_AUTH=false` it returns 501 rather than trusting an unverified token — but this must
be finished before any real deployment, and it is the single most important security gap.

**Privacy.** Driver PII isolated with per-row key ids so erasure is crypto-shredding rather than
rewriting a multi-terabyte store. `driver_assignment` is temporal, so person-linked data can be
severed without destroying asset history. Retention: 7 days hot, 90 days warm, 7 years cold,
then deleted — holding location history longer than we can justify is a DPDP liability.

**AI safety.** Four guardrails (§3.3 and T3). The copilot can never execute a write.

---

## 9. Test Strategy

| Test type | Tools | No. of tests | Coverage / result | In CI? |
| --- | --- | --- | --- | --- |
| Unit (algorithms, scoring, statistics) | pytest | ~150 | **88% line coverage** | Yes |
| Integration (live PostgreSQL + ClickHouse) | pytest + psycopg + clickhouse-connect | 31 | all passing | Yes |
| API / contract | FastAPI TestClient | 21 | all passing | Yes |
| Agent guardrails | pytest + stubbed LLM | 11 | all passing, no API key needed | Yes |
| Statistical calibration | pytest over 400 null trials | 2 | FP rate 5.8%, CI coverage 94.2% | Yes |
| Performance / load | 4×4 processes | — | 24,224 ev/s, 0 loss | Manual (`make bench`) |
| Security (SAST, image, deps) | Semgrep, Trivy | — | configured | Yes |
| Chaos | — | **0** | **not run** | No |

**Total: 200 tests, 88% coverage.**

**Edge cases covered:** duplicate events; out-of-order arrival; 3× bursts; unknown OEM dialect;
missing required field; physically impossible values; empty control arm; infeasible depot
(demand exceeds capacity); vehicle departing mid-window; zero-distance normalisation; malformed
tool arguments; agent step-budget exhaustion; unscoped database connection.

**Tests that prove correctness rather than execute lines** — the ones we are most pleased with:

1. **Brute-force optimality.** A0's output is compared against exhaustive search on small
   instances. A "near-optimal" claim means nothing without this.
2. **Energy conservation.** kWh bought = kWh stored + losses, checked against pack size as an
   *independent third quantity*. An earlier version derived pack size from the same two numbers
   it checked, so it reproduced the efficiency constant by construction and could never fail.
3. **Statistical calibration.** The DiD estimator is validated over 400 null trials — false
   positive rate, unbiasedness, p-value uniformity, CI coverage. An earlier version asserted
   non-significance on a *single* seed, which is a coin flip that fails 5% of the time by
   construction, and did.
4. **Injection inside a tool result.** Asserts the model never saw the hostile string.
5. **Tenant isolation as a property.** Every returned vehicle is checked against owner-level
   ground truth, rather than against hard-coded fixture VINs that would pass even if isolation
   were broken.

---

## 10. Observability

OpenTelemetry traces, Prometheus metrics, Grafana dashboards, application logs into ClickHouse
(one fewer system than ELK, and we already operate it).

**Metrics that matter:** events consumed/sec per consumer, consumer lag per partition, Bloom
false-positive rate, DLQ rate by reason, batch insert latency, API p95/p99 per route, rate-limit
rejections, agent guard scores, depot plan duality gap and solve time.

**Troubleshooting a latency spike.** Start at consumer lag per partition. Even lag across all 64
means a sink problem — check ClickHouse insert latency and active part count, since merge
pressure is the usual cause. Lag on one partition means a hot key: query ClickHouse for the top
VIN by event count in the window, which is how a stuck sensor at 1 kHz shows up. If lag is flat
but the dashboard is stale, the problem is after ingest — check MV materialisation and the API's
p95 on the rollup query. Traces tie a slow request to the specific hop.

> **Gap, stated:** the OTel/Prometheus/Grafana stack is **configured in Compose but not
> instrumented in the application code**. There is no dashboard screenshot because there is no
> dashboard. This is an honest shortfall against the brief.

---

## 11. AI / ML Component

**Purpose.** Two components, with deliberately different ambitions.

*The ML re-ranker* reorders the alerts the VEWS rules already opened. It may never surface an
alert on its own (ADR-0003), which is the structural reason a model trained on our own simulator
cannot manufacture a false alert.

*The copilot* answers questions about the depot's own data and proposes actions.

**Data & features.** Twelve engineered signals. Labels come from the simulator's ground-truth
fault ledger, which is kept out of every product read path.

**Leakage checks.** Train and test split **by vehicle, not by row**. A row split puts the same
vehicle in both halves and the model scores well by memorising its degradation trajectory. This
is the single easiest way to produce an impressive, worthless number here.

**Evaluation against a baseline — and the result.** The baseline is the rules themselves (VEWS
then dollars), not a random shuffle. The metric is precision@12, the product's actual constraint,
not AUC — a model with a beautiful AUC that puts the right vehicle at rank 13 is worthless.

> **precision@12 over 8 vehicle splits: rules 0.656, re-ranker 0.625. Mean delta −0.031. 3 of 8
> splits improved. The re-ranker ships DISABLED.**

We report this rather than hiding it. The decision is made on the mean across splits because
precision@12 moves in steps of 1/12, so a single split swings ±0.17 on noise — deciding on one
split is how teams convince themselves a coin flip is an improvement.

**Guardrails and cost.** Four guardrails (§3.3, T3). Groq free tier; a copilot turn is 2–3 LLM
calls plus 2–3 guard calls, ~6 s end to end. Guard fails open by design; the allowlist and human
approval gate do not.

**Vector layer.** `pgvector` holds OEM service-bulletin embeddings and incident signatures for
"similar past cases" retrieval. **Schema and indexes (HNSW) are built; the retrieval path is not
wired into the UI.** Stated rather than implied.

---

## 12. Architecture Decisions, Risks & Future Enhancements

**ADRs** (full text in `docs/adr/`):

| ADR | Decision | Key consequence |
| --- | --- | --- |
| 0001 | Polyglot persistence split by consistency need | Four stores to operate; no cross-store transactions, so writes use an outbox with idempotency keys |
| 0002 | Partition telemetry by VIN, not `fleet_id` | Tenant-scoped queries read all 64 partitions — acceptable, because they are served from ClickHouse |
| 0003 | Rules decide; ML may only re-rank | Forfeits whatever recall a pure model might reach; accepted, since on synthetic data we could never verify it |
| 0004 | Tenant isolation in the database (FORCE RLS + app role) | Every request must set `app.tenant_id`; forgetting yields an empty result, not a leak |

**Risks and technical debt, ranked by how much they would embarrass us:**

1. **JWKS verification is not implemented.** Fails closed, but unfinished. Blocks any real deployment.
2. **100k ev/s is extrapolated, not demonstrated.** Arithmetic shown; needs one cloud burst test.
3. **No `EXPLAIN ANALYZE` evidence**, which the brief explicitly asks for.
4. **Observability is configured but not instrumented.** No dashboard exists.
5. **Helm and Terraform are unvalidated** — neither CLI is installed here.
6. **Location masking by scope is unbuilt** — the largest open privacy gap.
7. **In-process rate limiting** means N replicas enforce N× the ceiling.
8. **Single region.** Designed for multi-region, not built.
9. **No chaos test.** Recovery is argued from design, not demonstrated.

**Next three steps from prototype to pilot:** finish JWKS verification and mTLS device identity;
run a five-minute 100k ev/s burst on cloud hardware with `EXPLAIN ANALYZE` captured for the top
three queries; instrument OpenTelemetry end to end and ship the Grafana dashboard.

---

## 13. Demo Video

| Time | Segment | What to show |
| --- | --- | --- |
| 0:00–0:30 | Problem | Charging on arrival at peak rates; the site cap nobody plans against |
| 0:30–1:00 | Solution | One-line pitch; the committed plan |
| 1:00–3:00 | Live demo | `make up`; the UI; commit a plan; lower the cap until it binds and watch load redistribute; the measured holdout with its confidence interval; ask the copilot a question; attempt a prompt injection and watch it refuse |
| 3:00–4:15 | Under the hood | Architecture; A0 and the duality gap; the benchmark run; the DLQ catching a bad value |
| 4:15–5:00 | Impact & next | Measured numbers; the honest gaps; who built it |

**Video Link:** _[fill in]_
---

## 14. Repository Checklist

- [x] README with problem, architecture, quick start, env vars, test commands
- [x] One-command run: `docker compose up` / `make up`
- [x] Seeded 100,000-vehicle dataset (`make seed`, 4.88 s)
- [x] Clear service folders plus `/docs`, `/infra`, `/tests`
- [x] CI: build, lint, all suites, Semgrep, Trivy on every push
- [x] No secrets committed; `.env.example` provided; history scanned
- [x] 4 ADRs, architecture diagram, STRIDE model, benchmarks
- [ ] Final tag `v1.0-submission`

---

## 15. Conclusion

**Key learnings.** The most valuable engineering here was not writing code, it was *testing
claims*. Five of the results we are proudest of are negative: the ML re-ranker lost to the rules;
a "lower bound" was not a bound; a conservation test was tautological; a null-hypothesis test was
a coin flip; tenant isolation was silently inert because `ENABLE ROW LEVEL SECURITY` does not
apply to the table owner. Every one was found by a test and none would have been found by review.

**Strengths.** A genuinely hard optimisation with a provable bound. A measured saving with a
confidence interval rather than a modelled one. Tenant isolation enforced at the database and
verified at two layers. 200 tests at 88% coverage that assert correctness rather than execute
lines.

**Challenges solved.** Reconciling "100k events/sec" with "runs on a reviewer's laptop" by
shipping two profiles that differ only by environment variables and publishing the extrapolation
arithmetic. Making a synthetic-data claim credible via a randomised holdout where generator bias
cancels. Keeping an agent with database access safe by making writes un-executable.

**What we would tell a reviewer to check first:** `docs/BENCHMARKS.md` for what is measured
versus extrapolated, and §12 for what is not finished.

---

## 16. Declarations

**Open-source components.** Redpanda (BSL/Apache-2.0), ClickHouse (Apache-2.0), PostgreSQL
(PostgreSQL licence), pgvector (PostgreSQL), MongoDB (SSPL), Redis (RSALv2/SSPL), EMQX
(Apache-2.0), Keycloak (Apache-2.0), FastAPI (MIT), psycopg (LGPL), confluent-kafka-python
(Apache-2.0), clickhouse-connect (Apache-2.0), scikit-learn (BSD-3), React (MIT), Vite (MIT),
MapLibre (BSD-3), Playwright (Apache-2.0), pytest (MIT), Prometheus/Grafana (Apache-2.0/AGPL-3.0).

**AI tools used.** Claude (Anthropic) was used throughout for design exploration, implementation,
test authoring and documentation. Four competing architectures were generated and critiqued during
planning; the final architecture synthesises two of them. Those drafts are not part of this
submission. At
runtime the product calls Groq-hosted `openai/gpt-oss-120b` (copilot) and
`meta-llama/llama-prompt-guard-2-86m` (injection screening).

**Data.** All data is synthetic, generated by our own simulator. No real personal data, no real
vehicle-owner data, no real VINs. Depot coordinates are city centroids, not customer locations.

---

## 17. Appendix

- `docs/ARCHITECTURE.md` — full architecture, CAP table, capacity model, build order
- `docs/BENCHMARKS.md` — every measured number and its method
- `docs/THREAT-MODEL.md` — STRIDE, with what is *not* covered
- `docs/adr/` — four Architecture Decision Records
- `docs/screenshots/` — UI
