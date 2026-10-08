# ReadyLine — Shift-Readiness Intelligence for Mixed ICE/EV Fleets

**Solution & Architecture Design Document** · v1.0 · for a 5-person student team, 10-day timebox

---

## Assumptions (stated, not asked)

| # | Assumption | Why this reading |
|---|---|---|
| A1 | The 100,000 vehicles are **multi-tenant**: ~60 tenants, largest 18,000 vehicles, median 900, a dispatcher owns ~250. | The brief says "one mid-sized fleet customer base". A single 100k fleet has no realistic human user; a 100k *customer base* does, and it forces tenant isolation, which is graded. |
| A2 | Data arrives on **two paths**: OEM-cloud connectors (webhook push + REST poll, device-free, the Motorq model) and MQTT from directly-connected units/our simulator. | Motorq is device-free, but the brief names MQTT. Both are real; their latency profiles differ and that difference is load-bearing (see §4.3). |
| A3 | "Ingest → dashboard < 2 s" means **event timestamp → visible in live vehicle state / alert feed**, not "visible in a 1-minute rollup tile". | A 1-minute window cannot close in under 2 s. We measure the former and label the latter's freshness in the UI. |
| A4 | Team = 5 students, ~80 usable hours each = **400 person-hours**. Reviewer hardware = 16 GB RAM, 8 cores. | The hard constraint says a reviewer runs `docker compose up` on a laptop. 400 h is the budget every scope decision in §18–19 is measured against. |
| A5 | All data synthetic. The simulator is a first-class, tested component, not a script. | Stated constraint; also the source of the honesty problem in §15.4. |
| A6 | Currency USD; costs are us-east-1 on-demand list prices, Oct 2026. | Needed for a checkable cost estimate. |

---

## Design lineage: the ICU central-monitoring analogy

Every hard part of this system was solved thirty years ago in a different building: the **hospital intensive care unit**. An ICU runs 30 continuous vital-sign streams per bed across hundreds of beds, 99.9% of the data is boring, a handful of events need a human at the bedside in under a minute, and the whole thing is reviewed retrospectively for quality improvement. Fleet telemetry has the same shape.

What we port, and what it buys us:

| ICU solution | What it is | ReadyLine port |
|---|---|---|
| **Alarm fatigue is the actual failure mode** (AAMI/Joint Commission: >85% of ICU alarms are non-actionable; nurses silence monitors) | The design target is not detection recall, it is *actionable alerts per shift* | We cap the morning board at **12 vehicles per dispatcher** and treat "alerts raised that the dispatcher dismissed" as a tracked SLI (§14) |
| **NEWS2 / MEWS early-warning score** | A transparent additive score: each vital gets 0–3 points from a published band table; total ≥5 escalates. Validated, auditable, explainable to a tired human at 3 a.m. | **VEWS** — Vehicle Early Warning Score (§8.1). Rules-first, ML only as a gated re-ranker (§15) |
| **ESI 5-level triage** | Acuity drives response time, not arrival order | 4 severity bands with contractual response targets (§2.3) |
| **"Similar patients" case retrieval** | Clinicians reason by precedent far better than by probability | Incident-signature kNN over pgvector (§8.9, §15.2) |
| **SBAR handoff** (Situation, Background, Assessment, Recommendation) | A fixed 4-slot format so handoffs don't lose information under time pressure | The alert card has exactly 4 slots: what, since when, why we think so, what to do. No free-form LLM prose (§15.5) |
| **Epic's sepsis model failure** (deployed widely, external validation found AUC 0.63 vs the vendor's 0.76–0.83; it fired on 18% of patients and missed 67% of sepsis cases) | A model validated only on its author's data is a claim about the author's data | §15.4 — we refuse to quote offline AUC as a product claim and ship rules as the decision path |

**Where the analogy breaks, and what we changed** (this matters more than where it holds):

| Break | Consequence | Design change |
|---|---|---|
| ICU nurse:patient is 1:2. Dispatcher:vehicle is 1:250. | There is no human to absorb the residual. | Automation must *act*, not just notify → the agentic layer with real (bounded, reversible) write tools (§15.5). |
| The patient is in the room. The vehicle is 40 km away and moving. | "Go look at it" is not an available action. | Every recommendation must be remotely executable: reschedule, reserve a charger, reroute, book a bay, message a driver. |
| Bedside monitors are on a hospital LAN with fixed latency. OEM clouds poll on their own cadence. | A single latency SLO is a lie. | Per-source **freshness SLI** and a UI staleness badge (§4.3, §14.1). |
| One patient, one record. A vehicle is shared by 3 drivers and can be sold mid-day. | Person-linked and asset-linked data diverge. | `driver_assignment` as a temporal bridge table; crypto-shredding for person-linked fields (§12.5). |
| Clinicians are a single trusted group. Lenders, fleets and drivers have *opposed* interests. | A dashboard that helps one party harms another. | Location masking by scope, not by role name; tenant isolation enforced at the database (§12.4). |
| ICU monitors don't get OTA firmware updates that rename fields. | Software-Defined Vehicles change payloads without notice. | The normalisation registry + shadow-mode promotion (§9.4) — the single most important piece of this design. |

---

## 1. Problem space chosen, and why it wins

### 1.1 The pick

**Shift readiness: preventing unplanned downtime and cost leakage in mixed ICE/EV commercial fleets.**

One sentence: *at 06:10 every morning, tell the dispatcher which of her 250 vehicles will not finish today's shift, and do something about each one before the drivers arrive.*

This is deliberately **not** "predictive maintenance" (an ML claim we cannot honestly validate on simulated data) and not "a fleet dashboard" (breadth, not depth). It is a decision with a deadline, which is what makes the latency, scale and testing requirements real rather than decorative.

### 1.2 Primary user

**Priya Raman, Fleet Operations Manager**, regional last-mile carrier, 250 vehicles (170 diesel LCVs, 80 BEVs), 3 depots.

- **Her situation at 06:10:** 230 drivers arrive between 06:30 and 07:15. She has ~45 minutes, one laptop, and a phone she is holding. 6 spare vehicles. 2 maintenance bays with 11 free hours today.
- **Her skill level:** expert in fleet operations, not in data. She will not build a filter. She will not read a scatter plot. She has used a telematics portal for 4 years and uses exactly two of its screens.
- **What she will actually do next:** scan a list top-to-bottom, make 8–12 binary decisions, and stop when the list runs out or the drivers arrive — whichever is first.

**Job to be done:** "When I start my shift, I want to know which vehicles are a risk to today's plan and what to do about each, so I can reassign work before drivers are standing in the yard."

**The pain, concretely:** today she finds out a vehicle is dead when a driver calls from the yard at 06:52. Her existing portal has 9 dashboards and sends 40+ threshold emails a day, so she has muted it. The information was *present* and *unusable*. This is alarm fatigue, and it is the pain we are actually solving.

### 1.3 The impact number, and its sensitivity

Built from stated inputs so a judge can check the arithmetic. All inputs are assumptions to be validated with a design partner; we publish them rather than hide them.

| Input | Value | Source / basis |
|---|---|---|
| Unplanned roadside events per vehicle-year (LCV, urban duty) | 1.8 | Assumption; fleet-maintenance surveys cluster 1.4–2.6 |
| Fleet size (reference tenant) | 2,500 | A2 |
| Events / year | 4,500 | 2,500 × 1.8 |
| Cost of one unplanned event | **$1,250** | tow $320 + 1.7 lost revenue-days × $285/day ($485) + failed-delivery penalty $120 + overtime/recovery admin $325 |
| Cost of the same event converted to planned | **$340** | spare-vehicle swap, scheduled bay slot, no tow, no penalty |
| Avoided cost per conversion | **$910** | |
| Fraction of events visible in telemetry | 0.45 | 12 V battery/starter decay, coolant temperature trend, DTC precursors, tyre-pressure drift, EV pack thermal + SoH, DEF/AdBlue level. *Not* visible: tyre puncture, accident, fuel theft, random electronic failure |
| Detection rate within those, at our operating precision | 0.65 | Set by the ≥0.50 precision@12 constraint — we deliberately give up recall to protect the dispatcher's attention |
| **Conversion rate** | **0.29** | 0.45 × 0.65 |

**Avoided cost = 4,500 × 0.29 × $910 = $1.19 M/yr for 2,500 vehicles = $475 per vehicle per year.** Across 100,000 vehicles, **$47.5 M/yr**.

Sensitivity, because a single-point estimate is not a claim:

| Scenario | Conversion | $/vehicle/yr |
|---|---|---|
| Pessimistic (visibility 0.30, detection 0.45) | 0.135 | $221 |
| Central | 0.29 | $475 |
| Optimistic (visibility 0.55, detection 0.75) | 0.41 | $672 |

Infrastructure cost is **$0.96/vehicle/yr** (§6.4). Even the pessimistic case clears it by 230×, which is the point: the constraint on this business is dispatcher attention, not compute.

**The number we can actually measure in the hackathon demo**, because the simulator has ground truth:

> **precision@12 ≥ 0.50 and recall@72h ≥ 0.30** on telemetry-visible degradation mechanisms, with **≤ 12 alerts/dispatcher/shift**.

We report these three numbers together. Reporting recall without the alert budget is how you build the thing Priya muted.

### 1.4 Why this wins against the grading criteria

| Criterion | Why shift-readiness specifically scores here |
|---|---|
| **Scale** | The decision needs *all* 100k vehicles scored every minute on a 9-signal rolling state — it cannot be sampled, so the 100k evt/s path is load-bearing, not theatre. |
| **Data engineering** | Needs both: real-time scoring (<5 s) *and* 90-day batch retrospectives to calibrate bands per tenant. Both halves of the mandate are intrinsic, not bolted on. |
| **System design** | A hard 06:10 deadline forces explicit CAP choices with user-visible consequences (§6.2, ADR-003), a priority lane for criticals (§11.1), and a degradation ladder. |
| **Security** | Mixed ICE/EV commercial fleets with driver-linked data under GDPR + DPDP, multi-tenant, with lender-style conflicting interests → tenant isolation and location masking are forced, not optional. |
| **Testing** | The core is pure algorithmic logic (scoring, segmentation, DP, graph) with ground-truth labels from our own simulator → 80%+ coverage on core logic is genuinely reachable (§13.1), and we can write *correctness* tests, not just smoke tests. |
| **Depth beats breadth** | One user, one decision, one deadline. We can go 6 levels deep on it instead of 1 level deep on eight dashboards. |

**Rejected problem spaces:**

| Rejected | Why |
|---|---|
| Predictive maintenance (as framed) | The honest version collapses into "we learned our own simulator's hazard model". Shift readiness keeps the same engineering but makes the *rule layer* the product, so the claim survives the honesty test. |
| Driver safety scoring | Algorithmically shallow (count harsh events, divide). Also adversarial towards the driver, which makes the privacy story worse, not better. |
| Multi-OEM normalisation as *the* product | Superb data engineering, no human user, no impact number. We absorb it as the **enabling subsystem** (§9.4) instead, where it does the most work. |
| Carbon reporting | A quarterly report, not a decision with a deadline. Nothing forces real-time. |
| Asset recovery | Strong impact (Motorq's 68%) but a narrow single-event workflow; most of the 100k-vehicle machinery would be idle. |

---

## 2. Solution overview and user journey

### 2.1 The one-screen product

**The Morning Board.** A ranked list of at most 12 vehicle cards. Each card is SBAR-shaped:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⬤ CRITICAL   MH12-AB-4471  ·  Tata Ace EV  ·  Depot: Hadapsar       │
│                                                       VEWS 11  ↑ +4  │
│ WHAT   Will not complete today's 118 km route.                       │
│ SINCE  12 V auxiliary battery resting voltage 11.4 V, falling        │
│        0.09 V/day for 11 days. 4 short trips yesterday (no recharge).│
│        Ambient tonight 7 °C.                                         │
│ WHY    Rule AUX_BATT_DECAY (3 of 3 conditions). 7 similar past cases │
│        → 6 were no-crank within 48 h. [see cases]                    │
│ DO     ▸ Swap to spare MH12-AB-4502 (idle, 94% SoC)   ← recommended  │
│         ▸ Book bay slot 09:00–09:40, est. $140                       │
│         ▸ Dismiss  (tell us why: ▾)                                  │
│ Data freshness: 1.2 s (MQTT direct)                                  │
└──────────────────────────────────────────────────────────────────────┘
```

Three design rules, all from the alarm-fatigue literature:
1. **Hard cap of 12.** A 13th finding does not appear; it increments a "suppressed" counter she can expand. Ranking is the product.
2. **Every card carries its reason as a rule name and its evidence**, never "our AI detected". Priya must be able to disagree with a specific sentence.
3. **Dismissal is a first-class, *reasoned* action.** Dismiss-with-reason feeds §15.3 and is the only labelled feedback we will ever have from a real user.

Three supporting screens, and no more: **Vehicle** (live state, 24 h signal strip, trips, open alerts), **Charging Plan** (tonight's depot schedule from the DP in §8.4, with the ToU cost delta), **Fleet Analytics** (batch; 90-day idling/cost/utilisation with CSV + API export). Priya uses screen 1 daily and screen 2 when she clicks a card. We built the other two for her manager, and we say so.

### 2.2 The journey: event → detection → insight → action → outcome

```mermaid
sequenceDiagram
    autonumber
    participant V as Vehicle / OEM cloud
    participant G as ingest-gateway
    participant K as Kafka
    participant F as Flink stream-processor
    participant S as Stores (Redis/Cassandra/PG)
    participant A as agent-service
    participant P as Priya (web)

    V->>G: 1 Hz frame: soc 41%, aux_v 11.4, ambient 7C, odo
    G->>G: mTLS cert.VIN == payload.VIN; VIN regex+checkdigit; normalise via OEM plan
    G->>K: telemetry.raw.v1 (key=vin) + telemetry.critical.v1 if safety/DTC
    K->>F: consume, dedup on (vin,seq), event-time watermark
    F->>F: EWMA per signal -> VEWS bands -> score 11 (was 7)
    F->>S: upsert vehicle.last_* ; alert ON CONFLICT (dedup_key) DO NOTHING
    F->>P: alerts.v1 -> Redis pub/sub -> WebSocket push (p95 ~180 ms)
    Note over A: triggered on severity>=HIGH
    A->>S: get_vehicle_state, get_similar_incidents (pgvector kNN), search_bulletins
    A->>S: estimate_cost(swap) = $0 vs estimate_cost(no_action) = $1,250
    A->>P: ranked card with 1 recommended + 2 alternative actions
    P->>A: tap "Swap to MH12-AB-4502"
    A->>S: swap_shift_assignment (HITL-approved) + create_work_order + audit chain
    S->>P: route reassigned; 06:21. Drivers arrive 06:30 to a working vehicle.
```

**Outcome, measured:** the vehicle that would have failed at 06:52 in the yard instead goes into bay 09:00. The avoided-cost ledger gets a $910 row, attributed to `alert_id`, and the dispatcher's board shows 11 items instead of 40 emails.

### 2.3 Severity ladder (ported from ESI triage)

| Band | Definition | Response target | Delivery | Example |
|---|---|---|---|---|
| **CRITICAL** | Safety now, or vehicle will not start/finish today | **< 5 s** to board + push | Priority lane (§11.1), WebSocket + SMS | Airbag DTC; aux battery no-crank predicted; BEV thermal runaway precursor |
| **HIGH** | Will fail within 72 h, or >$300 exposure today | < 30 s | Board, WebSocket | Coolant trend; SoH cliff; P0301 recurring |
| **MEDIUM** | Degrading; schedule this week | < 5 min | Board (below fold), digest | Tyre pressure drift; brake wear estimate |
| **LOW** | Cost/efficiency only | next batch cycle | Analytics screen only — **never** the board | Idling, route inefficiency, unapproved-depot dwell |

The band determines which pipeline the event takes. That is how a latency SLO becomes an architecture instead of a promise.

---

## 3. Feature list (MoSCoW), traceable to code

Every row names the service and the path where it lives. Paths match the tree in §7.3.

### Must have — built and demoed

| ID | Feature | Service / code path |
|---|---|---|
| M1 | 100k-vehicle simulator: fleets, duty cycles, 9 injectable fault mechanisms, 3 OEM payload dialects, replay at configurable rate | `simulator/` → `internal/hazard/`, `internal/dialect/` |
| M2 | Dual-path ingestion: MQTT 5 + mTLS, and OEM webhook/poll connectors | `ingest-gateway/internal/transport/{mqtt,http,poll}.go` |
| M3 | VIN validation (charset + check digit, NA-conditional) and DTC parsing | `shared/domain/vin.py`, `shared/domain/dtc.py` (+ Go port, one golden-vector file) |
| M4 | OEM normalisation registry + hot reload + shadow-mode promotion | `insight-api/domain/normalization/`, `stream-processor/.../NormalizationPlan.java` |
| M5 | Two-tier dedup (Bloom + confirm set) and idempotent sinks | `stream-processor/.../DedupFunction.java`, `adapters/repo/alert_repo.py` |
| M6 | VEWS scoring with per-vehicle-class band tables | `shared/domain/vews/` |
| M7 | Stop/trip segmentation (HMM Viterbi) + RDP polyline | `shared/domain/segmentation/` |
| M8 | Morning Board read model + ranked API + WebSocket alert stream | `insight-api/api/v1/board.py`, `mv_vehicle_risk_board` |
| M9 | OAuth2/OIDC + JWT, RBAC, Postgres RLS tenant isolation | `insight-api/api/deps.py`, `migrations/0007_rls.sql` |
| M10 | Audit log (hash-chained) for every data access and agent action | `adapters/repo/audit_repo.py` |
| M11 | Batch layer: Iceberg tables, DuckDB local / Spark cloud, same SQL | `batch/sql/*.sql`, `batch/jobs/` |
| M12 | `docker compose up` → seeded 100k vehicles, live board in < 6 min | `deploy/compose/`, `Makefile` |
| M13 | Test + evidence pack: unit/integration/BDD/load/security, CI | `tests/`, `.github/workflows/` |
| M14 | Right-to-erasure flow with crypto-shredding, proven by test | `insight-api/domain/privacy/`, `tests/compliance/test_erasure.py` |

### Should have — built, narrower than designed

| ID | Feature | Path | Narrowing |
|---|---|---|---|
| S1 | Charging-schedule DP under ToU tariffs | `shared/domain/charging/dp.py` | Per-vehicle optimal; depot contention by greedy, not MILP (§8.4) |
| S2 | Nearest reachable charger (bidirectional A* + ALT) | `shared/domain/routing/` | One city OSM extract (Bangalore), not global |
| S3 | Agentic recommend + bounded auto-execute | `agent-service/graph/` | 4 read tools, 2 reversible write tools auto; 2 write tools HITL-only |
| S4 | Similar-incident kNN over pgvector | `insight-api/domain/similarity/` | Feature-engineered 64-d encoder, no learned deep encoder |
| S5 | Unapproved-depot clustering (union-find over geohash) | `batch/jobs/depot_clusters.py` | Nightly batch, not streaming |
| S6 | Cassandra hot store adapter | `adapters/hot_store/cassandra.py` | Cloud profile only; local uses the Timescale adapter (§5.2) |

### Could have — designed, stubbed, honestly labelled "stub" in the UI

C1 Cost-leakage ledger with attribution · C2 Customer-facing Kafka egress topic with per-tenant ACLs · C3 OpenSearch alert/note search · C4 Argo Rollouts canary analysis · C5 GCP Terraform (AWS complete, GCP module interfaces only) · C6 Pact contract tests (schema-registry compatibility gate ships; Pact is the stretch).

### Won't have — and why, explicitly

W1 Mobile native app (web is responsive; a PWA is 60 h we don't have) · W2 Real OEM integration (no partnership, and the constraint forbids real data) · W3 Deep-learning fault model (§15.4 — we cannot validate it, so shipping it would be dishonest) · W4 Multi-region active-active (designed in ADR-003, deployed single-region) · W5 Billing/invoicing · W6 Driver-facing app.
