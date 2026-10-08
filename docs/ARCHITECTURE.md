# ReadyLine — Shift-Readiness & Cost-Leak Intelligence for Mixed ICE/EV Fleets

**Locked architecture and methodology.** Decided 2026-10-08. This is the source of truth; build from it.

Synthesised from two surviving arena designs (a004 "ReadyLine", a001 "FleetLeak") that independently
converged on the same user, the same rejection of predictive maintenance, and the same core insight:
**ranking is the product.** a004 contributes the alarm-fatigue discipline and the rules-first score;
a001 contributes dollar attribution and a falsifiable ranking metric.

---

## 1. Problem space (locked — revised 2026-10-09)

**The nightly depot commitment: by 16:00, decide which vehicle charges on which charger, starting
when, to what SoC — so every route tomorrow completes, under the depot's electrical cap, at
minimum cost.**

> A depot charging operations manager running 400–1,200 mixed vehicles across 3–9 depots needs a
> way to **commit a nightly plan that guarantees tomorrow's routes**, because today drivers plug in
> at shift end (17:00–21:00, the on-peak window) and every charger draws full power at once — so
> energy is bought at the most expensive hour, the site demand peak sets the monthly demand charge,
> packs sit at 100% SoC for nine hours calendar-ageing, and 2–4 vehicles a week still get swapped
> because nobody knew their SoC would not cover tomorrow's route.

Primary user: depot charging operations manager. Secondary: dispatcher (morning exceptions),
finance, sustainability, OEM platform team.

### Why this replaced "shift readiness" (the previous framing)

The earlier framing scored itself honestly at 3/10 and had three structural faults, not merely
missing code:

1. **It was predictive maintenance renamed.** We rejected PM for synthetic-data circularity, then
   built a system predicting which vehicles fail. The rename does not survive a sharp question.
2. **The algorithms were decoration.** A1 (reachable charger) and A2 (charging DP) survived the
   merge but the product never used them.
3. **It traded ambition for honesty and scored neither.** Rules-as-decision-path is clean, but it
   zeroes the two rows the brief explicitly grades: "ML and vector layer" and "Agentic AI".

The depot commitment fixes all three simultaneously:

- **The depot power cap makes this a resource-constrained scheduling problem** over a shared,
  contended supply — not N independent single-vehicle DPs. That is genuine algorithmic depth
  rather than a textbook exercise.
- **A1 and A2 become the product.** Reachability and the tariff DP are what the plan is made of.
- **VEWS survives and becomes coherent**: mechanical readiness and energy readiness combine into
  one verdict, "will this vehicle complete tomorrow's assigned route".
- **It is a commitment the system executes**, not a warning it emits — which gives the agentic
  layer real work, real consequences, and therefore real guardrails worth building.
- It is an **optimisation under constraint**, which no amount of simulator circularity can fake.

### What carries over

The alarm-fatigue discipline is kept, repositioned: the Morning Board is no longer a list of
predictions, it is **the exceptions to a plan we committed last night**. That is a stronger object
— every card has a prior commitment to compare against. >85% of ICU alarms are non-actionable and
nurses silence the monitors; the hard cap of 12 stays.

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

### The randomised holdout arm (the core credibility mechanism)

Two of the four competing designs independently converged on this, which is why it is now central
rather than an afterthought.

**10% of EVs are randomised into an unmanaged control arm at onboarding**, stratified by depot and
by daily km. Control vehicles are measured identically and generate recommendations internally —
but the recommendations are never delivered, and the depot plan excludes them. They charge the way
the fleet charges today: plug in on arrival, draw full power.

Reported saving is a **difference-in-differences** on `$ per 100 route-km`, treatment versus
control, against the pre-period — with a two-sided t-test and a 95% confidence interval, served
from `GET /api/v1/experiments/charging-arbitrage`.

This is the difference between "our model says we saved $927/EV/year" and a measured number with
an error bar. It is also the one thing in the whole design that cannot be faked by a simulator
that we wrote, because **both arms run against the same simulator**. Any bias in the generator
applies equally to both and cancels in the difference.

| Claim | Target | Asserted by |
| --- | --- | --- |
| **Energy arbitrage saving** | **measured, with 95% CI** | Randomised holdout, DiD, t-test |
| **Depot peak reduction** | site peak cut ≥ 25% | Measured against the cap, both arms |
| Plan feasibility | **100%** of committed routes complete | Simulator replay; a missed route is a hard failure |
| Schedule optimality | DP cost within **1%** of brute force on small instances | Property test vs exhaustive search |
| Energy conservation | ledger balances to **< 0.1%** | Conservation identity: kWh bought = kWh stored + losses |
| Attention budget | **≤ 12 exceptions/dispatcher/shift** | Hard cap, enforced in code |
| Alert precision | precision@12 ≥ 0.50 | Simulator ground-truth labels |

The last two are retained from the previous framing but demoted: they are secondary to the
measured arbitrage number, because they are scored against labels we invented.

**Pack-life extension and avoided-range-failure savings are modelled, not measured, and are
labelled as such in the UI.** We commit only to the arbitrage row.

### Why the conservation identity matters

`kWh bought = kWh stored + charging losses` is an invariant that must hold on every session. It is
a test that *proves correctness* rather than merely executing lines — if the energy ledger drifts,
every dollar figure downstream is wrong and the test says so immediately.

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
| **A0** | **Depot commitment: N vehicles, M chargers, one site power cap, hard departure deadlines** | **Lagrangian relaxation with an exactly-solved subproblem.** Dualise the shared power cap into a time-varying price λ(t). Each vehicle's subproblem is then a continuous knapsack — buy E kWh, rate-capped per slot, none after the deadline — which **price-sorted greedy solves exactly** by an exchange argument. A subgradient step raises λ(t) where the cap is violated and lowers it where slack remains; residual violation is repaired by proportional shedding plus re-placement into slots with headroom. | O(K · N · T log T), K ≈ 24 |
| A1 | Nearest *reachable* charger given SoC | **A\*** on road graph, heuristic = haversine/max_speed, edges pruned by energy feasibility | O(E log V) |
| A2 | Cheapest single-vehicle schedule under ToU tariffs | **DP** over (slot × SoC bucket) with charge-rate + departure-deadline constraints | O(T·S·R) |
| A3 | Noisy GPS → stops and trips (this *is* how idle time is computed) | **DP / PELT segmentation** with dwell-radius cost | O(n·w) windowed |
| A4 | VIN validation | Regex `^[A-HJ-NPR-Z0-9]{17}$` + ISO 3779 check digit | O(1) |
| A5 | DTC parsing | Regex `^[PBCU][0-3][0-9A-F]{3}$` → severity map | O(1) |
| A6 | Duplicate suppression | **Bloom filter** on (vin, seq) in Redis | O(k), ~1.2 MB/h @1% FPR |
| A7 | Top-K cost leaks / worst degraders | **Count-Min Sketch** + heap | O(1) update |
| A8 | Unapproved-depot clustering | **Geohash-7 bucketing + union-find** connected components | ~O(n·α(n)) |
| A9 | VEWS banding + EWMA per signal | Additive band table over exponentially-weighted means | O(1)/event |

Each gets pseudocode, a proven bound, and a **measured** runtime at stated input size.

### Why A0 is the centrepiece

Scheduling N vehicles onto M chargers under a site power cap with hard deadlines is
**NP-hard** (it contains bin-packing). The naive approaches both fail in ways we can demonstrate:

- **Greedy by deadline** fills the cheap window with whoever leaves first, then discovers the last
  vehicles must buy their entire requirement at peak — or cannot finish at all.
- **N independent DPs** (what A2 alone gives you) is *optimal per vehicle and infeasible in
  aggregate*: every vehicle independently picks the same cheapest hour, so the depot draws N ×
  max_kW at 02:00 and trips the main breaker.

Lagrangian relaxation is the right tool because the coupling is a *single shared resource*. The
dual price λ(t) is literally a congestion charge on depot power: vehicles that can wait are priced
out of the contended hour and move themselves. It also yields a **lower bound** on the optimum at
every iteration, so we can report the duality gap and state how close to optimal the committed
plan provably is — rather than asserting it is good.

That bound is what makes this defensible under questioning, and it is why a brute-force
cross-check on small instances is in the test matrix (§2).

**Solving the subproblem exactly is not an optimisation detail — it is what makes the bound real.**
The first implementation used the A2 bucket DP for each vehicle. The DP discretises SoC into 51
buckets, so each per-vehicle "optimum" is the best point *on that grid* and therefore strictly
costlier than the continuous optimum. Summing those inflated costs produced a λ-bound that the
continuous repair phase could legitimately undercut — a "lower bound" above the achievable cost,
which is no bound at all. A test comparing committed cost against the bound caught it. Greedy
removes the discretisation error at its source and is asymptotically faster.

Two further properties the implementation is required to preserve, each covered by a test:

- **The duality gap is `None` when the plan sheds load.** The bound assumes every vehicle reaches
  target; if we shed, the committed plan solved a strictly easier problem. Reporting a small gap
  there would read as near-optimal at precisely the moment we failed to serve the depot.
- **The unmanaged baseline must not be crippled.** The measured saving is a difference against
  it, so making the control arm artificially bad is the single most tempting way to cheat in this
  system. A test asserts the baseline still reaches target when time and power allow.

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
