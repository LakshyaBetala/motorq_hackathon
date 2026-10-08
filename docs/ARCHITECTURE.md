# ReadyLine — Shift-Readiness & Cost-Leak Intelligence for Mixed ICE/EV Fleets

**Locked architecture and methodology.** Decided 2026-10-08. This is the source of truth; build from it.

Synthesised from two surviving arena designs (a004 "ReadyLine", a001 "FleetLeak") that independently
converged on the same user, the same rejection of predictive maintenance, and the same core insight:
**ranking is the product.** a004 contributes the alarm-fatigue discipline and the rules-first score;
a001 contributes dollar attribution and a falsifiable ranking metric.

---

## 1. Problem space (locked)

**Shift readiness: tell the dispatcher at 06:10 which vehicles will not finish today's shift, rank
them by attributed dollars, and act on each before the drivers arrive.**

> Priya Raman, Fleet Operations Manager, needs a way to **know which of her 250 vehicles are a risk
> to today's plan and what to do about each**, because today she finds out when a driver calls from
> the yard at 06:52 — her existing portal has 9 dashboards and sends 40+ threshold emails a day, so
> she has muted it. The information was *present and unusable.*

Primary user: fleet operations manager (dispatcher). Secondary: depot supervisors, finance, drivers, sustainability, platform engineers.

**The pain we are actually solving is alarm fatigue, not detection.** >85% of ICU alarms are
non-actionable and nurses silence the monitors; fleet telematics has the identical failure mode.
So the design target is *actionable alerts per shift*, not recall.

### Why this space wins

| Criterion | Why this specifically |
| --- | --- |
| Scale | All 100k vehicles must be scored every minute on a rolling multi-signal state — it cannot be sampled, so the 100k ev/s path is load-bearing, not theatre. |
| Data engineering | Needs real-time scoring (<5 s) *and* 90-day batch cohort baselines to calibrate bands per tenant. Both halves of the mandate are intrinsic. |
| System design | A hard 06:10 deadline forces real CAP choices with user-visible consequences, a priority lane for criticals, and a degradation ladder. |
| Algorithms | Every algorithm the brief names falls out non-decoratively (§5). Idle time *is* trip segmentation; cost ranking *is* the leaderboard. |
| Testing (heavy) | Core is pure algorithmic logic with **ground-truth labels from our own simulator** → we write correctness assertions, not smoke tests. |
| Honesty | The dominant term in our impact number is the most fragile; we ship a sensitivity panel instead of hiding it. |

**Rejected:** predictive maintenance (the honest version collapses into "we learned our own
simulator's hazard model" — the circularity *is* the deliverable, unwinnable in the timebox);
driver safety scoring (scoring function arbitrary without insurer loss data, and adversarial to the
driver, which worsens the privacy story); asset recovery (thin data path, and the 68% figure is
Motorq's, not ours to claim); carbon reporting (a quarterly report, nothing forces real-time);
multi-OEM normalisation as headline (infrastructure, not a job to be done — **absorbed as the
enabling spine**, where it does the most work).

### Key reading of the brief (this choice drives the whole design)

The 100,000 vehicles are **multi-tenant**: ~60 tenants, largest 18,000, median 900, one dispatcher
owns ~250. A single 100k fleet has no realistic human user; a 100k *customer base* does — and it
forces tenant isolation, which is graded.

---

## 2. What we claim, and what we actually prove

The absolute dollar figure is a modelled extrapolation and is **labelled as one everywhere it
appears**. What the acceptance tests assert is narrower and falsifiable:

| Claim | Target | Asserted by |
| --- | --- | --- |
| Alert precision | **precision@12 ≥ 0.50** | Simulator ground-truth fault labels |
| Alert recall | **recall@72h ≥ 0.30** on telemetry-visible mechanisms | Same |
| Attention budget | **≤ 12 alerts/dispatcher/shift** | Hard cap, enforced in code |
| Cost ranking correctness | **Spearman ρ ≥ 0.85** vs simulator's ground-truth cost ledger | Back-test |
| Dollar attribution accuracy | **within ±15%** of ledger truth per vehicle-month | Back-test |

Reporting recall without the alert budget is how you build the thing Priya muted. We report all three together.

Modelled impact: **~$475/vehicle/yr** central case (pessimistic $221, optimistic $672) against
infrastructure cost of **~$0.96/vehicle/yr**. The ~230× headroom is the business case, not the absolute number.

---

## 3. The product: one screen

**The Morning Board** — at most 12 ranked vehicle cards, each SBAR-shaped (ported from clinical handoff):

```
⬤ CRITICAL   MH12-AB-4471 · Tata Ace EV · Hadapsar        VEWS 11 ↑+4   $910 at risk
WHAT   Will not complete today's 118 km route.
SINCE  12 V aux resting 11.4 V, falling 0.09 V/day for 11 days. 4 short trips
       yesterday (no recharge). Ambient tonight 7 °C.
WHY    Rule AUX_BATT_DECAY (3 of 3). 7 similar past cases → 6 no-crank within 48 h.
DO     ▸ Swap to spare MH12-AB-4502 (idle, 94% SoC)   ← recommended
       ▸ Book bay 09:00–09:40, est. $140      ▸ Dismiss (reason ▾)
Freshness: 1.2 s (MQTT direct)
```

Three rules, all load-bearing:
1. **Hard cap of 12.** A 13th finding increments a "suppressed" counter. Ranking is the product.
2. **Every card names its rule and its evidence**, never "our AI detected". Priya must be able to disagree with a *specific sentence*.
3. **Dismiss-with-reason is first-class** — the only labelled feedback we will ever get from a real user.

Supporting screens, and no more: **Vehicle** (live state, 24 h signal strip, trips, alerts),
**Charging Plan** (tonight's depot DP schedule with ToU cost delta), **Fleet Analytics** (90-day
cost leaderboard, sensitivity panel, CSV/API export).

### Severity ladder (ESI triage port) — the band selects the pipeline

| Band | Definition | Target | Lane |
| --- | --- | --- | --- |
| CRITICAL | Safety now, or won't start/finish today | **< 5 s** | Priority topic, WebSocket + SMS |
| HIGH | Fails within 72 h, or >$300 exposure today | < 30 s | Board, WebSocket |
| MEDIUM | Degrading; schedule this week | < 5 min | Board below fold, digest |
| LOW | Cost/efficiency only | next batch | Analytics only — **never** the board |

That is how a latency SLO becomes an architecture instead of a promise.

---

## 4. Scoring: VEWS, rules-first (this is how we stay honest)

**VEWS** (Vehicle Early Warning Score), ported from the NEWS2 clinical early-warning score: each of
9 signals gets 0–3 points from a published band table; the total drives the severity band. It is
additive, transparent, auditable, and explainable to a tired human at 06:10.

**ML is a gated re-ranker only — never the decision path.** This is the deliberate answer to
synthetic-data circularity: if the model is only re-ordering items the rules already surfaced, a
model that learned our own generator cannot manufacture a false alert. We refuse to quote offline
AUC as a product claim (cf. the Epic sepsis model: vendor claimed AUC 0.76–0.83, external validation
found 0.63, fired on 18% of patients, missed 67% of sepsis).

---

## 5. Algorithms (the graded core)

| # | Problem | Algorithm | Complexity |
| --- | --- | --- | --- |
| A1 | Nearest *reachable* charger given SoC | **A\*** on road graph, heuristic = haversine/max_speed, edges pruned by energy feasibility | O(E log V) |
| A2 | Cheapest charging schedule under ToU tariffs | **DP** over (slot × SoC bucket) with charge-rate + departure-deadline constraints | O(T·S·R) |
| A3 | Noisy GPS → stops and trips (this *is* how idle time is computed) | **DP / PELT segmentation** with dwell-radius cost | O(n·w) windowed |
| A4 | VIN validation | Regex `^[A-HJ-NPR-Z0-9]{17}$` + ISO 3779 check digit | O(1) |
| A5 | DTC parsing | Regex `^[PBCU][0-3][0-9A-F]{3}$` → severity map | O(1) |
| A6 | Duplicate suppression | **Bloom filter** on (vin, seq) in Redis | O(k), ~1.2 MB/h @1% FPR |
| A7 | Top-K cost leaks / worst degraders | **Count-Min Sketch** + heap | O(1) update |
| A8 | Unapproved-depot clustering | **Geohash-7 bucketing + union-find** connected components | ~O(n·α(n)) |
| A9 | VEWS banding + EWMA per signal | Additive band table over exponentially-weighted means | O(1)/event |

Each gets pseudocode, a proven bound, and a **measured** runtime at stated input size.

---

## 6. Stack (locked, with what was rejected)

| Layer | Choice | Why, and what was rejected |
| --- | --- | --- |
| Ingest paths | **Dual: OEM-cloud connectors (webhook + poll) AND MQTT 5 + mTLS** | Motorq is device-free (OEM clouds), but the brief names MQTT. Both are real and their latency profiles differ — that difference is load-bearing (freshness SLI). |
| Log | **Redpanda** | Kafka API-compatible (zero lock-in) but a single binary — the difference between a demo that boots in 40 s and one that doesn't. Rejected: Kafka (heavy locally), RabbitMQ (no replay/partitioned ordering). |
| Wire format | Protobuf + schema registry | Compact; evolution rules enforced in CI. Rejected: Avro (worse polyglot tooling), JSON (3× bytes). |
| Raw OEM envelopes | **MongoDB** | Schema-less landing zone for un-normalised payloads, so a format change can be **replayed** after we fix the mapping. This is the real NoSQL justification, not a checkbox. |
| Telemetry | **ClickHouse** | Columnar, ~10:1 compression, MVs roll up at insert time so dashboards read pre-aggregated rows. **AP.** Rejected: Cassandra (no cheap analytics), Timescale (same Postgres write ceiling we're escaping). |
| Relational core | **PostgreSQL** 3NF | Fleet, Vehicle, Driver, DriverAssignment, Trip, Alert, Subscription, Billing, AuditLog. **CP.** |
| Vector | **pgvector** in that Postgres | Incident-signature kNN ("7 similar past cases") + bulletin RAG. Corpus is thousands of docs — a separate Qdrant would be an unjustified service. |
| Cache / live state | **Redis** | Last-known state, rate limits, Bloom dedup, pub/sub → WebSocket. |
| Stream processing | **Go** consumers, event-time watermarks | Sub-5 s alerts. Rejected: **Flink** — correct at scale, but costs a student team a week. Honest trade, stated in §10. |
| Batch | ClickHouse MVs + scheduled Python | 90-day cohort baselines. |
| API | **FastAPI** (SQLModel) | OpenAPI free; sits next to ML/agent code. |
| Frontend | **React + Vite + TS**, Tailwind, **MapLibre** | MapLibre = no API key, no vendor lock. |
| Auth | **Keycloak** (OAuth2/OIDC + JWT) | Self-hosted, any cloud. Rejected: Auth0/Cognito (lock-in; brief rewards cloud-agnosticism). |
| ML | scikit-learn GBT, **gated re-ranker only** | §4. |
| Agent | LangGraph, tool allowlist, HITL | Bounded reversible write tools; every call audited. |
| Observability | OTel → Prometheus + Grafana; logs → ClickHouse | One fewer store than ELK, and we already operate it. |
| Deploy | Compose (local), Helm (K8s), Terraform (AWS) | Cloud-agnostic = plain K8s + self-hosted deps. No managed service on the critical path. |

### Polyglot map and CAP

| Data | Store | CAP | PACELC reasoning |
| --- | --- | --- | --- |
| Raw telemetry | ClickHouse | **AP** | A replica loss must never block ingest. Else-Latency. |
| Raw OEM envelopes | MongoDB | **AP** | Landing zone; replayable. |
| Live vehicle state | Redis | **AP** | Stale by seconds is fine for a map pin. |
| Fleet/billing/RBAC/assignments | PostgreSQL | **CP** | A double invoice or stale permission is unacceptable. Else-Consistency. |
| Audit log | PostgreSQL append-only | **CP** | Compliance evidence must be totally ordered. |

### Partitioning & capacity

Topic `telemetry.raw`: **key = VIN**, 64 partitions. VIN gives per-vehicle ordering (trip
segmentation needs it) and hashes evenly — keying by `fleet_id` would hot-spot the 18,000-vehicle
tenant onto one partition. ClickHouse: `PARTITION BY toYYYYMM(ts) ORDER BY (fleet_id, vin, ts)`.

100k × 1 ev/s × 1 KB = 8.6 TB/day raw → ZSTD ~10:1 → **~860 GB/day**. Hot 7 d full-resolution;
warm 90 d 1-min rollup (~98% smaller); cold 7 y Parquet on S3/MinIO. Retention = partition DROP.

---

## 7. Two profiles, one codebase

100k ev/s does not run on a laptop; pretending otherwise loses marks.

| Profile | Vehicles | Rate | Purpose |
| --- | --- | --- | --- |
| `local` (`docker compose up`) | 100,000 seeded, 2,000 live | ~2,000 ev/s | Reviewer's laptop. Full feature set. |
| `scale` (k6, partitioned) | 100,000 | 100,000+ ev/s | Measured throughput, ingest path |

**Env vars only — no code branches on profile.** A profile branch breaks 12-factor and the
cloud-agnostic claim. Throughput is published as measured per-partition × partitions **with the
arithmetic shown**, never asserted.

---

## 8. Services (hexagonal)

```
services/
  simulator/  Python — 100k vehicles, duty cycles, 9 injectable fault mechanisms,
                       3 OEM dialects, bursts/dupes/out-of-order, GROUND-TRUTH LEDGER
  ingest/     Go     — MQTT+mTLS / OEM webhook+poll → normalise → validate → dedup → CH + Mongo
  detector/   Go     — EWMA → VEWS bands → alerts (dedup_key upsert) → Postgres + Redis pub/sub
  api/        Python — FastAPI: REST, OIDC, RBAC, tenant isolation, pagination, rate limits
  agent/      Python — LangGraph copilot, tool allowlist, HITL approval, audit chain
web/          React  — Morning Board, Vehicle, Charging Plan, Analytics
```

Ports & adapters: `domain/` (pure) ← `app/` (use cases) ← `adapters/` (Kafka, DB, HTTP).
Inward-only dependencies, enforced by an import-linter rule in CI.

**The simulator emits a ground-truth fault and cost ledger.** That ledger is what turns acceptance
tests from "the page loads" into real assertions, and it is why §2's metrics are measurable at all.

### The normalisation registry (the most important subsystem)

SDVs get OTA updates that rename fields without notice. A new/changed OEM dialect is a **registry
entry, not a deploy**: mappings are versioned data, new mappings run in **shadow mode** against live
traffic, and are promoted only when their output matches expectations. Raw envelopes in Mongo make
the backfill replayable.

---

## 9. Build order (demoable at every checkpoint)

| Phase | Ships | Demoable |
| --- | --- | --- |
| **P0** | Compose: Redpanda, ClickHouse, Postgres, Mongo, Redis, Keycloak, EMQX | `docker compose up` green |
| **P1** | Simulator + Protobuf + ingest → ClickHouse/Mongo | Events landing, rate visible |
| **P2** | Postgres 3NF + migrations + seed 100k, multi-tenant | ER diagram is real |
| **P3** | FastAPI + OIDC + RBAC + tenant isolation | Secure API |
| **P4** | React Morning Board + live map | **First end-to-end demo** |
| **P5** | VEWS detector + severity lanes (A9, A3, A6) | Sub-5 s alert on screen |
| **P6** | A1 + A2 (A*, DP charging) + Charging Plan screen | ₹/$ saved, visible |
| **P7** | Cost attribution + leaderboard + sensitivity panel (A7, A8) | Ranking back-test ρ |
| **P8** | ML gated re-ranker vs rules baseline | Evaluated, honestly |
| **P9** | LangGraph agent, HITL, audit chain | Copilot acts; audit proves it |
| **P10** | Tests to 80%, k6, Trivy/Semgrep, CI, Helm, Terraform, STRIDE, ADRs | Evidence pack |

---

## 10. Known weaknesses (state these before a judge finds them)

1. **Synthetic-data circularity.** Mitigated structurally: rules are the decision path, ML only
   re-ranks what rules surfaced. We do not quote offline AUC as a product claim.
2. **100k ev/s is not demonstrated on a laptop.** We publish measured per-partition throughput ×
   partition count and show the arithmetic.
3. **Detector is Go-with-watermarks, not Flink.** Less powerful on late data beyond the watermark.
   We bound the watermark explicitly and emit late-arrival count as a metric.
4. **The impact number's dominant term is its most fragile.** Hence the sensitivity panel; defaults
   are labelled "modelled assumption", never "measured".
5. **Single-region.** Designed for multi-region, not built in the timebox.
