# Joule — Fleet Energy Cost Recovery Hub
### Solution architecture & design document (v1.0, build-ready)

---

## 0. Assumptions (stated, because the brief is open-ended)

| # | Assumption | Why this reading |
|---|---|---|
| A1 | Team = 5 students, 3 calendar weeks, ~250 effective person-hours. | "Student team, hackathon timebox." Every scope decision in §18–19 is sized against this. |
| A2 | Reviewer laptop = 8 cores / 16 GB RAM / 100 GB free SSD, Docker Desktop. | "Reviewers will run `docker compose up` on a laptop." 16 GB is the hard ceiling the design must fit. |
| A3 | Data arrives two ways: (a) **OEM cloud pull/webhook** (primary, device-free, Motorq-style), (b) **MQTT direct-connect** for fleets with their own telematics units (secondary). | Motorq is device-free, but the brief mandates mTLS **for devices**, so a device path must exist. |
| A4 | "100,000 vehicles" = one tenant-group; the platform is multi-tenant from day one. | Tenant isolation is a scored security requirement; retro-fitting it is the classic trap. |
| A5 | Currency USD; tariffs and fuel prices are synthetic but modelled on real US TOU structures. | Impact numbers must be checkable. |
| A6 | Cloud cost figures are AWS `us-east-1` on-demand/Graviton list prices, Oct 2026, ±20%. | Needed for the retention cost estimate. |
| A7 | All data is synthetic. No real VINs, no real people. The simulator is the only data source. | Hard constraint. §15 addresses the circularity this creates. |

**Reading order for a builder:** §19 (build order) → §7 (folder tree) → §6 (schemas) → §8 (algorithms). Everything else is reference.

---

## 1. Problem space chosen, and why it wins

### 1.1 The choice

**Mixed-fleet energy cost recovery: find, quantify in dollars, and eliminate avoidable fuel and electricity spend across a mixed ICE/EV fleet — then prove the saving.**

This is *not* the brief's "fuel, idling and utilisation cost" row restated. It is that row fused with "EV charging and battery health", unified by one invariant: **every litre and every kWh has a price, a time, a place and a cause.** One canonical quantity — `avoidable_cost_usd` — is computed on every trip segment and rolls up to vehicle, driver, depot, fleet and tenant.

### 1.2 Primary user and job to be done

| | |
|---|---|
| **Primary user** | Fleet Operations Manager, 100k mixed vehicles across ~600 depots, 12 OEMs. |
| **Secondary users** | Depot supervisor (daily action list), Fleet finance analyst (monthly variance), Sustainability lead (reuses the same energy ledger), Platform engineer (OEM onboarding). |
| **Job to be done** | *"Every Monday, tell me the 50 things that will save me the most money this week, in dollars, ranked, with the action already drafted — and prove last month's savings were real."* |
| **Pain today** | Energy is 25–30% of fleet TCO and the single largest controllable line. It is invisible because (a) each OEM reports fuel/SoC/odometer differently and at different cadence, (b) idling, pre-conditioning, peak-rate charging and degraded batteries all look like "normal consumption" in an OEM portal, (c) nobody can separate *bad route* from *bad behaviour* from *bad battery*. Managers get dashboards, not ranked dollar-denominated actions. |

### 1.3 Why it wins against the *grading criteria* (not against interest)

| Criterion the judges score | Why this problem space maximises the score |
|---|---|
| **Scale** | Energy attribution needs **every** event, not sampled ones: a 40-second idle at a traffic light must be distinguished from a 4-minute idle with HVAC on. That forces the honest 100k events/sec design instead of a design that could cheat by sampling. |
| **Data engineering** | Needs all four temperatures of data: per-event (idle detection), 1-minute (trends), daily (ledger), 400-day (battery degradation, seasonal baselines). Needs OEM normalisation, units conversion, late data, and backfill-on-algorithm-change. This is the richest possible data-engineering surface. |
| **Algorithms (heavily weighted)** | Maps 1:1 onto all four families the brief names, with *no* contrivance: graph (reachable-charger), DP (TOU schedule under a depot power cap; GPS stop/trip segmentation), parsing (VIN, DTC), streaming (sliding-window idle, Bloom dedup, CMS top-K, geohash). Driver-safety scoring would have given me streaming only; predictive maintenance would have given me ML only. |
| **System design** | The *same* event stream must serve a <5 s alert (CP-ish, ordered) and a 400-day analytical history (AP, eventually consistent). That is a genuine, unavoidable CAP split — not a manufactured one. |
| **Security / compliance** | Location + driver behaviour + fuel cards = personal data under GDPR and India DPDP. Masking, retention, right-to-erasure are *load-bearing*, not bolted on. |
| **Testing (heavily weighted)** | Cost arithmetic and optimisation are **deterministically verifiable**: a DP schedule can be checked against brute force, a check digit against its own inverse, an energy ledger against a conservation identity. This problem space lets me write tests that *prove correctness*, not tests that merely execute lines. That is the single biggest scoring lever and it drove the choice. |
| **Honesty** | The impact claim is measurable with a randomised holdout, so I can state a number that is auditable rather than asserted. |

**Rejected alternatives, with reasons:**

- **Predictive maintenance** — rejected: the headline metric ("breakdowns in 7 days") is unverifiable on synthetic data within a hackathon. We would be grading our own simulator's failure labels. Algorithmically it is one supervised model; the graph and DP requirements would have to be bolted on.
- **Driver safety scoring** — rejected: strong streaming story, near-zero graph/DP story, and the "insight" is a score, not a dollar. Scores are hard to defend; dollars are not.
- **Multi-OEM normalisation as the whole product** — rejected as the *product*, **kept as a first-class subsystem** (§6.1 `oem_field_mapping`, §10.2 failure path). It is infrastructure, not a user outcome; a judge asking "who pays for this?" has no answer.
- **Asset recovery** — rejected: compelling demo, but it is fundamentally a geofence + last-known-position problem. Thin data engineering, trivial algorithms.

### 1.4 The impact number, and the arithmetic behind it

Fleet mix: **70,000 ICE, 30,000 EV**.

**ICE — avoidable idle:**

```
measured avoidable idle (idle > 180 s, HVAC-adjusted, not traffic-attributed) = 42 min/veh/day
fuel burn at idle (light commercial, HVAC on)                                 = 0.55 gal/hr
diesel/gasoline blended price                                                 = $3.80/gal
gross avoidable idle spend = 70,000 × 0.70 hr × 0.55 × $3.80  = $102,410 /day
                                                              = $37.4 M /year
capturable fraction (coaching + auto-shutdown policy, lit. 15–30%)  → take 20%
ICE capture = $7.5 M/year  =  $8.92 /vehicle/month
```

**EV — TOU arbitrage + avoided demand charge:**

```
energy per EV per day            = 25 kWh  (≈120 km at 0.21 kWh/km)
peak vs off-peak spread          = $0.12 /kWh (conservative US blended)
shiftable share (overnight dwell ≥ 6 h)  = 70%
arbitrage = 30,000 × 25 × 0.70 × $0.12   = $63,000 /day = $23.0 M/year gross
realistic capture (charger availability, depot power cap, driver override) → 25%
EV energy capture        = $5.75 M/year  =  $15.97 /EV/month
depot demand-charge shaving: 600 depots × 18 kW peak reduction × $14/kW-month
                         = $151 k/month = $1.8 M/year
```

**Headline, stated three ways so it can be checked:**

| Claim | Value | How it is verified |
|---|---|---|
| Avoidable spend **identified** | **$18.40 / vehicle / month** ($22.1 M/yr) | Sum of `avoidable_cost_usd` on the ledger; reconciled to total energy spend by a conservation test (§13.4). |
| Spend **verifiably eliminated** | **≥ $6.50 / vehicle / month** ($7.8 M/yr) at 90 days | **10% randomised holdout fleet.** Vehicles are randomised to `treatment` / `holdout` at onboarding, stratified by model and depot. Holdout vehicles are measured identically but produce no recommendations. Reported saving = difference in energy-cost-per-100-km between arms, difference-in-differences against the pre-period. |
| Platform cost | **$0.124 / vehicle / month** (§6.4) | AWS list prices, itemised. |
| Return | **52:1 on verified savings**, 148:1 on identified | — |

**Sensitivity (the honest part):** the verified number is linear in the capture fraction, which is the one input we cannot observe in a hackathon.

| Capture fraction | Verified $/veh/mo | Return |
|---|---|---|
| 10% (pessimistic) | $2.60 | 21:1 |
| 20% (modelled) | $6.50 | 52:1 |
| 35% (strong adoption) | $11.40 | 92:1 |

Even at the pessimistic end the economics hold, which is why this is the claim we make. **We report the identified number as computed and the eliminated number as measured; we never report a modelled saving as realised.** The holdout is built in Phase 1 (§19), not retro-fitted, because a saving claim without a control arm is the first thing a hostile reviewer attacks.

---

## 2. Solution overview and user journey

### 2.1 One-sentence architecture

Events land once, are normalised to one canonical schema, are **priced** in-stream, and fan out to four stores of different temperature; a ranked dollar-denominated action list is the only user-facing artefact that matters; an agent drafts the action and a human approves it.

### 2.2 The journey, end to end, with the code path and the real latency

| Stage | What happens | Service / code path | Latency (p95) |
|---|---|---|---|
| **1. Event** | `VEH-0042` reports `speed=0, rpm=780, hvac=ON, fuel_rate=0.58 gal/hr` at 10:14:03. OEM-C posts a 240-event batch to the webhook endpoint. | `ingest-gateway` → `OemWebhookAdapter` | 40 ms (internet) |
| **2. Normalise + price** | Auth (mTLS or OIDC), VIN check-digit validation, `oem_field_mapping` lookup (cached), unit conversion, canonical Protobuf, `idempotency_key = sha256(vin|ts|seq)`, produce to `telemetry.normalized.v1` keyed by VIN. Critical classes are *also* forked to `telemetry.critical.v1`. | `ingest-gateway/domain/Normalizer` | 18 ms |
| **3. Detect** | Kafka Streams: Bloom dedup → session window on `speed<2 ∧ engine_on` → when the window crosses 180 s, emit `IdleEvent` with `cost_usd = duration_hr × burn_rate × price_at(ts, depot)`. Redis `vehicle_state` updated per event. | `stream-processor/domain/IdleDetector` | 120 ms |
| **4. Insight** | Idle event joined to trip, driver assignment, depot, tariff window → `avoidable_cost_usd` written to `trip_segment` and the 1-minute rollup hypertable. ML residual model scores "explained vs avoidable". | `stream-processor` → `insight-api/EnergyLedger` | +380 ms |
| **5. Surface** | Live tile on the depot dashboard updates over WebSocket. Monday ranked list is a keyset-paginated read of a continuous aggregate. | `insight-api` → `web` | +45 ms |
| **6. Act** | Copilot drafts: *"VEH-0042: 11.2 h avoidable idle/week at Depot DAL-3, $23.40/wk. Draft: enable 3-min auto-shutdown policy + driver coaching card. Est. saving $18.70/wk."* Written to `recommendation` as `PROPOSED`. Supervisor approves in one click (or it auto-applies if impact < $50 and ≤25 vehicles, reversible for 24 h). | `copilot-agent` (LangGraph) → `insight-api` | agent: 4–9 s (async, not on the alert path) |
| **7. Outcome** | Policy pushed to the OEM policy API / coaching card to the driver app. 14 days later the ledger shows idle down 61% on that vehicle; the holdout arm shows −4%; the delta is booked as verified saving. | `action-dispatcher` → `savings-attribution` nightly job | — |

**Live-tile total: 40+18+120+45 = 223 ms p95** (budget 2 s). **Critical alert: 290 ms p95 / ~700 ms p99** (budget 5 s). Windowed *aggregates* are explicitly a separate, slower SLO — see §11, because conflating them is the easiest way to claim a latency you do not have.

---

## 3. Feature list (MoSCoW), each traceable to a code path

| # | Feature | Pri | Service / module | Built or stubbed? |
|---|---|---|---|---|
| F1 | Multi-OEM ingest: webhook + MQTT, mTLS/OIDC, Protobuf normalisation | **M** | `ingest-gateway/adapter/{OemWebhookAdapter,MqttAdapter}` | **Built** (3 synthetic OEM formats) |
| F2 | VIN + DTC validation and quarantine | **M** | `libs/algo-core-java/vin`, `dtc` | **Built** |
| F3 | Dedup (Bloom + Redis exact) and ordering by `(vin, seq)` | **M** | `stream-processor/domain/Deduper` | **Built** |
| F4 | Stop/trip/idle segmentation (DP) | **M** | `stream-processor/domain/Segmenter` | **Built** |
| F5 | Energy ledger: `avoidable_cost_usd` per segment, rolled up | **M** | `stream-processor/domain/EnergyPricer` | **Built** |
| F6 | Live depot dashboard (map + tiles over WebSocket) | **M** | `web/src/features/depot` | **Built** |
| F7 | Monday ranked action list, keyset-paginated | **M** | `insight-api/read/LeaderboardQuery` | **Built** |
| F8 | Critical alerts (<5 s) + webhook subscriptions with retry | **M** | `stream-processor/RuleEngine`, `notifier` | **Built** |
| F9 | Secure REST API: OAuth2/OIDC, RBAC, tenant RLS, rate limits | **M** | `insight-api/adapter/web`, Keycloak | **Built** |
| F10 | Batch analytics over Iceberg/Parquet (daily ledger, backfill) | **M** | `batch-jobs` (DuckDB local / Trino cloud) | **Built** |
| F11 | 100k-vehicle simulator, `realistic` + `blast` modes | **M** | `apps/simulator` | **Built** |
| F12 | Audit log (hash-chained) + right-to-erasure flow | **M** | `insight-api/compliance` | **Built** |
| F13 | TOU charging schedule DP under depot power cap | **S** | `insight-api/domain/ChargePlanner` | **Built** |
| F14 | Nearest reachable charger (precomputed multi-source Dijkstra + A* for display) | **S** | `insight-api/domain/ChargerRouter` | **Built** (1 metro graph) |
| F15 | ML residual model: explained vs avoidable energy | **S** | `energy-brain` (LightGBM) | **Built** |
| F16 | Copilot agent: draft → approve → apply → audit | **S** | `copilot-agent` (LangGraph) | **Built**, 6 tools |
| F17 | Unapproved-depot clustering (union-find) | **S** | `batch-jobs/depot_discovery` | **Built** |
| F18 | Top-K wasteful vehicles / DTC codes via Count-Min Sketch | **S** | `stream-processor/domain/TopK` | **Built** |
| F19 | Battery state-of-health regression | **C** | `energy-brain/soh` | **Designed + stubbed** (returns nominal fade curve) |
| F20 | Kafka egress feed for customers | **C** | `egress-bridge` | **Designed + stubbed** (topic + ACL exist, no billing) |
| F21 | OEM policy push (auto-shutdown enable) | **C** | `action-dispatcher` | **Stubbed** (mock OEM API container, records calls) |
| F22 | Savings attribution vs holdout | **C** | `batch-jobs/attribution` | **Built** (simple DiD) |
| F23 | Carbon reporting, route optimisation, insurance scoring | **W** | — | Out of scope. The energy ledger is the substrate they would sit on; saying so is better than half-building them. |

MoSCoW is enforced in the build order (§19): every Must lands by Phase 3, so the demo is complete even if Phases 4–5 slip.

---

## 4. High-level architecture

### 4.1 C4 L1 — System context

```mermaid
graph TB
  FM["Fleet Ops Manager<br/>(person)"]
  DS["Depot Supervisor<br/>(person)"]
  AN["Finance Analyst<br/>(person)"]
  PE["Platform Engineer<br/>(person)"]

  J["<b>Joule</b><br/>Fleet Energy Cost Recovery Hub"]

  OEM["OEM Clouds (12)<br/>external"]
  TU["Telematics Units<br/>external devices"]
  UTIL["Utility / TOU Tariff API<br/>external"]
  WX["Weather API<br/>external"]
  IDP["Keycloak (OIDC)<br/>external"]
  CRM["Customer systems<br/>(webhooks / Kafka)"]

  FM -->|"HTTPS/TLS1.3 + OIDC, React SPA"| J
  DS -->|"HTTPS/TLS1.3 + WebSocket (wss)"| J
  AN -->|"HTTPS REST /v1 + CSV export"| J
  PE -->|"HTTPS admin API + SQL mapping rows"| J

  OEM -->|"HTTPS webhook POST (mTLS or OIDC client-creds), Protobuf/JSON batch"| J
  J -->|"HTTPS GET poll (OAuth2 client-creds) for pull-only OEMs"| OEM
  TU -->|"MQTT 5 over TLS 1.3 + mTLS client cert, QoS 1"| J
  UTIL -->|"HTTPS GET, cached 6 h"| J
  WX -->|"HTTPS GET, cached 1 h"| J
  J -->|"OIDC discovery + JWKS over HTTPS"| IDP
  J -->|"HTTPS webhook POST (HMAC-SHA256 signed) / Kafka SASL_SSL"| CRM
```

### 4.2 C4 L2 — Container view, every arrow carries protocol + p95 hop latency

```mermaid
graph TB
  subgraph EDGE["Edge"]
    MQ["EMQX 5<br/>MQTT broker"]
    GW["ingest-gateway<br/>Java 21 / Spring Boot<br/>stateless, HPA 3-12"]
  end

  subgraph BUS["Event backbone"]
    RP["Redpanda 6 brokers<br/>Kafka API, RF=3<br/>tiered storage -> S3"]
    SR["Schema Registry<br/>(Redpanda built-in)"]
  end

  subgraph STREAM["Stream processing"]
    SP["stream-processor<br/>Kafka Streams<br/>HPA 12-40"]
    RE["rule-engine<br/>stateless, critical path<br/>HPA 2-8"]
  end

  subgraph STORE["Stores"]
    RD["Redis 7<br/>state, Bloom, locks, RL"]
    PG["PostgreSQL 16<br/>+ TimescaleDB + pgvector<br/>1 primary + 2 replicas"]
    CS["Cassandra 5<br/>6 nodes, RF=3"]
    S3["S3 / MinIO<br/>Iceberg + Parquet"]
  end

  subgraph SERVE["Serving"]
    API["insight-api<br/>Java 21, virtual threads<br/>HPA 4-12"]
    NOT["notifier<br/>webhooks + email"]
    WEB["web<br/>React 19 + TS + Vite"]
  end

  subgraph INTEL["Intelligence (async)"]
    BR["energy-brain<br/>Python, LightGBM"]
    AG["copilot-agent<br/>Python, LangGraph"]
    BJ["batch-jobs<br/>DuckDB / Trino"]
  end

  subgraph OBS["Platform"]
    OT["OTel Collector"]
    PR["Prometheus + Grafana<br/>+ Tempo + Loki"]
    KC["Keycloak"]
  end

  SIM["simulator<br/>realistic + blast"]

  SIM -->|"HTTPS POST batch / MQTT | 6 ms"| GW
  SIM -->|"MQTT 5 TLS | 4 ms"| MQ
  MQ -->|"MQTT bridge -> Kafka produce | 5 ms"| GW
  GW -->|"Kafka produce acks=all, zstd, Protobuf | 18 ms"| RP
  GW -->|"HTTP GET schema, cached 10 min | 0.2 ms"| SR
  GW -->|"Redis GETEX idempotency probe | 1.1 ms"| RD
  GW -->|"JDBC: oem_field_mapping, cached 60 s | 0.4 ms"| PG

  RP -->|"Kafka consume, 120 partitions | 120 ms"| SP
  RP -->|"Kafka consume telemetry.critical.v1 | 90 ms"| RE
  SP -->|"Redis pipeline HSET vehicle_state | 2 ms"| RD
  SP -->|"CQL batched async writes, LOCAL_QUORUM | 9 ms"| CS
  SP -->|"Kafka produce rollups.1m.v1 | 12 ms"| RP
  SP -->|"Kafka produce alerts.v1 | 10 ms"| RP
  RE -->|"Kafka produce alerts.v1 | 10 ms"| RP
  RP -->|"Kafka Connect S3 sink -> Iceberg | 60 s batch"| S3
  RP -->|"consume rollups.1m.v1, COPY batches of 5k | 90 ms"| API
  API -->|"JDBC COPY into hypertable | 22 ms"| PG
  RP -->|"consume alerts.v1 | 40 ms"| NOT
  NOT -->|"HTTPS POST HMAC-signed, 3 retries | 120 ms"| API

  WEB -->|"HTTPS REST /v1 + wss stream | 25 ms net"| API
  API -->|"Redis GET hot state | 3 ms"| RD
  API -->|"JDBC read replica, keyset | 12-40 ms"| PG
  API -->|"CQL raw drill-down | 55 ms (p99 180)"| CS
  API -->|"OIDC JWKS, cached 1 h | 0.1 ms local verify"| KC

  BJ -->|"Iceberg scan via S3 API | 2-40 s"| S3
  BJ -->|"JDBC upsert daily ledger | 1-6 s"| PG
  BR -->|"Iceberg read features | 30 s"| S3
  BR -->|"gRPC score batch | 8 ms"| API
  AG -->|"HTTPS typed tools only | 90-400 ms/tool"| API
  AG -->|"pgvector HNSW kNN | 14 ms"| PG

  GW -->|"OTLP/gRPC"| OT
  SP -->|"OTLP/gRPC"| OT
  API -->|"OTLP/gRPC"| OT
  AG -->|"OTLP/gRPC"| OT
  OT -->|"remote_write / OTLP"| PR
```

**Boundary rules that keep this maintainable (and are machine-enforced, §7.4):**
1. Only `ingest-gateway` produces to `telemetry.*`. Nothing else may.
2. Only `insight-api` writes to Postgres on the request path; `stream-processor` writes to Postgres **only** via the `rollups.1m.v1` topic, never directly. One writer per store per path.
3. `copilot-agent` has **no database credentials**. It reaches data only through typed HTTP tools on `insight-api`.
4. No service calls another service synchronously except `web → insight-api`, `agent → insight-api`, `brain → insight-api`. Everything else is the log. This is what keeps the failure modes countable.
