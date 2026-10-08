# ReadyLine

**Shift-readiness and cost-leak intelligence for mixed ICE/EV fleets.**

At 06:10, tell the dispatcher which of her 250 vehicles will not finish today's shift, ranked by
attributed dollars, and act on each before the drivers arrive.

Submission for the Talenciaglobal / Motorq *Connected Vehicle Intelligence* hackathon.
Motorq is an industry reference only; this is an independent academic exercise with no affiliation.

---

## The problem

A fleet operations manager's existing portal has 9 dashboards and sends 40+ threshold emails a
day, so she has muted it. She finds out a vehicle is dead when a driver calls from the yard at
06:52. The information was *present and unusable*.

**The pain is alarm fatigue, not detection.** Over 85% of ICU alarms are non-actionable and nurses
silence the monitors; fleet telematics has the identical failure mode. So our design target is
*actionable alerts per shift*, and the Morning Board is hard-capped at **12 cards**. Ranking is the
product.

## What we claim, and what we prove

The absolute dollar figure is a modelled extrapolation and is labelled as one everywhere it
appears. What the acceptance tests actually assert, against the simulator's ground-truth ledger:

| Claim | Target |
| --- | --- |
| precision@12 | ≥ 0.50 |
| recall@72h on telemetry-visible mechanisms | ≥ 0.30 |
| alerts per dispatcher per shift | ≤ 12 |
| cost-leak ranking, Spearman ρ vs ledger | ≥ 0.85 |
| dollar attribution vs ledger, per vehicle-month | within ±15% |

Reporting recall without the alert budget is how you build the thing the user muted, so we always
report them together.

## Architecture

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

**ClickHouse is AP and holds telemetry. PostgreSQL is CP and holds fleet, billing, RBAC and
audit.** MongoDB holds un-normalised OEM envelopes so a mid-flight format change can be fixed and
*replayed*. Full reasoning, CAP/PACELC table, capacity model and ADRs: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

### Two profiles, one codebase

100,000 events/sec does not run on a laptop, and we don't pretend otherwise.

| Profile | Vehicles | Rate | Purpose |
| --- | --- | --- | --- |
| `local` | 100,000 seeded, 2,000 live | ~2,000 ev/s | Reviewer's laptop, full feature set |
| `scale` | 100,000 | 100,000+ ev/s | Measured throughput, ingest path |

They differ by **environment variables only**. Throughput is published as measured per-partition ×
partition count with the arithmetic shown, never asserted.

## Quick start

```bash
cp .env.example .env
make up          # docker compose: Redpanda, ClickHouse, Postgres, Mongo, Redis, Keycloak, EMQX
make seed        # 100,000 vehicles across ~60 tenants
make simulate    # start the telemetry stream
make test        # all suites
```

Requires Docker. The algorithm and simulator suites run standalone with just Python:

```bash
pip install -r requirements-dev.txt
pytest
```

## The graded core

Nine algorithms, each with pseudocode, a proven complexity bound and a measured runtime:

| # | Problem | Algorithm | Complexity |
| --- | --- | --- | --- |
| A1 | Nearest *reachable* charger given SoC | A\* over energy, admissible heuristic | O(E log V) |
| A2 | Cheapest charging schedule under ToU tariffs | DP over (slot × SoC bucket) | O(T·S·R) |
| A3 | Noisy GPS → stops and trips | Viterbi DP segmentation | O(n) |
| A4 | VIN validation | Regex + ISO 3779 check digit | O(1) |
| A5 | DTC parsing | Regex + severity map | O(1) |
| A6 | Duplicate suppression | Bloom filter on (vin, seq) | O(k) |
| A7 | Top-K cost leaks | Count-Min Sketch + heap | O(1) update |
| A8 | Unapproved-depot clustering | Geohash-7 + union-find | ~O(n·α(n)) |
| A9 | VEWS banding | Additive band table over EWMA | O(1)/event |

A1 and A2 are implemented properly rather than gestured at — A2 is an exact DP, and there is a test
asserting it beats a greedy "charge when cheap" strategy in the case where greedy provably fails.

## How we stay honest about ML

**Rules decide; ML only re-ranks.** VEWS is a transparent additive 0–3-per-signal score ported from
the NEWS2 clinical early-warning score. The ML model may reorder what the rules already surfaced;
it may never surface an alert on its own.

This is the structural answer to synthetic-data circularity: a model that learned our own generator
cannot manufacture a false alert if it only reorders. We do not quote offline AUC as a product
claim — see the Epic sepsis model, where a vendor-claimed AUC of 0.76–0.83 externally validated at
0.63, firing on 18% of patients while missing 67% of sepsis cases.

The simulator reinforces this: **four of its nine fault mechanisms are deliberately invisible in
telemetry** (puncture, collision, fuel theft, random electronic failure), so achievable recall is
bounded by physics rather than by our cleverness.

## Repository layout

```
docs/            ARCHITECTURE.md (source of truth), adr/, arena-designs/ (superseded references)
services/
  simulator/     100k vehicles, 9 fault mechanisms, 3 OEM dialects, ground-truth ledger
  api/           FastAPI + the algorithm core + migrations
  ingest/        Go: MQTT/OEM → normalise → ClickHouse + Mongo
  detector/      Go: EWMA → VEWS → alerts
  agent/         LangGraph copilot, tool allowlist, HITL, audit
web/             React + Vite + MapLibre
infra/           compose/, helm/, terraform/
tests/
```

## Status

| Phase | State |
| --- | --- |
| Algorithm core (A1–A9) + tests | **Done** — 77 tests passing |
| Simulator: fleet, faults, ground-truth ledger | **Done** |
| Postgres 3NF schema, RLS tenant isolation | **Done** |
| ClickHouse schema + rollup MVs | **Done** |
| Compose stack | **Done**, not yet booted (Docker daemon was down in dev) |
| Telemetry generator, ingest, detector, API, web, agent, ML | **Next** |

## Declarations

- All data is synthetic. No real personal or vehicle-owner data.
- Built with assistance from Claude (Anthropic) for design exploration and implementation.
- `docs/arena-designs/` contains four competing designs generated during planning; the final
  architecture synthesises two of them and supersedes all four.
