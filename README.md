# ReadyLine

**The nightly depot charging commitment, for mixed ICE/EV fleets.**

By 16:00, decide which vehicle charges on which charger, starting when, to what state of charge —
so every route tomorrow completes, under the depot's electrical cap, at minimum cost. The Morning
Board then shows only the exceptions to that commitment, hard-capped at 12.

Submission for the Talenciaglobal / Motorq *Connected Vehicle Intelligence* hackathon.
Motorq is an industry reference only; this is an independent academic exercise with no affiliation.

---

## The problem

Drivers plug in at shift end — 17:00 to 21:00, the on-peak window — and every charger draws full
power at once. Energy is bought at the most expensive hour, the simultaneous draw sets the
monthly demand charge, packs sit at 100% SoC for nine hours calendar-ageing, and vehicles still
get swapped in the morning because nobody knew their charge would not cover the route.

**Charging is not one problem per vehicle. It is one problem per depot**, because they share one
electrical supply. Running an optimiser per vehicle is measurably worse than useless: ten
vehicles at 20 kW all independently choose the single cheapest hour and draw 200 kW against a
60 kW cap.

## Measured results

Every number here comes from a `make` target, not an estimate. Methods in
[docs/BENCHMARKS.md](docs/BENCHMARKS.md).

| Result | Value |
| --- | --- |
| Charging cost vs charging on arrival | **60.8% lower** |
| Distance from proven optimum | **0.00% duality gap** |
| Saving, randomised holdout | **186.35 per 100 units, 95% CI [152.72, 219.98], p < 0.0001** |
| Ingest throughput | **24,224 ev/s** on 4 consumers (6,050 each, linear) |
| Storage compression | **12.89 : 1**, 8.2 bytes/row |
| 100,000 vehicles seeded | **4.88 s** |
| Tests | **200 passing, 88% coverage** |

### The randomised holdout

10% of vehicles are randomised into an unmanaged control arm at onboarding, stratified by depot,
and never receive a recommendation. The saving is a difference-in-differences with a Welch
t-test, served from `/api/v1/experiments/charging-arbitrage`.

Both arms run against the same simulator, so **any bias in our own generator cancels in the
difference**. That is the only reason a number measured on synthetic data is worth anything. The
control arm moved by +0.0012 between periods against −186.35 for treatment; a near-zero control
change is what makes the difference attributable.

The endpoint **refuses to return a number when it cannot estimate one** rather than fabricating.

## What we deliberately do not claim

- **100,000 events/sec is extrapolated, not demonstrated.** We measured 6,050 ev/s per consumer,
  linear to 4, while sharing a laptop with the broker and four databases. The arithmetic to 17
  consumers is shown; a cloud burst test is the honest way to close it.
- **The ML re-ranker lost to the rules** — precision@12 0.656 vs 0.625 over 8 vehicle splits — so
  it ships **disabled**.
- **JWKS token verification is not implemented.** It fails closed, but it is unfinished.
- Nine known gaps are listed in §12 of [docs/SOLUTION-DOCUMENT.md](docs/SOLUTION-DOCUMENT.md).

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
make seed        # 100,000 vehicles across 60 tenants (~5 s)
make experiment  # 30 nights of both holdout arms (~90 s) -- needed for the saving panel
make api         # FastAPI on :8000
make web         # UI on http://localhost:5173
```

Then open the UI and press **Commit plan**. Lower the site cap until it binds and watch the load
redistribute rather than overload.

```bash
make stream      # produce telemetry into Redpanda
make ingest      # consume -> normalise -> ClickHouse
make bench       # 4 producers + 4 consumers, throughput measurement
make test        # 200 tests
```

Requires Docker and Python 3.12+.

The algorithm, simulator, agent and statistics suites run **standalone with no infrastructure** —
integration tests skip automatically when the stack is down, and the agent tests stub the LLM so
no API key is needed:

```bash
pip install -r requirements-dev.txt
pytest                      # 200 tests
```

The copilot is optional. Put a free key from console.groq.com in `GROQ_API_KEY` to enable it;
everything else works without one.

## The graded core

Ten algorithms, each with pseudocode, a proven complexity bound and a measured runtime:

| # | Problem | Algorithm | Complexity |
| --- | --- | --- | --- |
| **A0** | **Depot commitment under a shared power cap** | **Lagrangian relaxation; subproblem solved exactly by price-sorted greedy** | O(K·N·T log T) |
| A1 | Nearest *reachable* charger given SoC | A\* over energy, admissible heuristic | O(E log V) |
| A2 | Cheapest charging schedule under ToU tariffs | DP over (slot × SoC bucket) | O(T·S·R) |
| A3 | Noisy GPS → stops and trips | Viterbi DP segmentation | O(n) |
| A4 | VIN validation | Regex + ISO 3779 check digit | O(1) |
| A5 | DTC parsing | Regex + severity map | O(1) |
| A6 | Duplicate suppression | Bloom filter on (vin, seq) | O(k) |
| A7 | Top-K cost leaks | Count-Min Sketch + heap | O(1) update |
| A8 | Unapproved-depot clustering | Geohash-7 + union-find | ~O(n·α(n)) |
| A9 | VEWS banding | Additive band table over EWMA | O(1)/event |

**A0 is the centrepiece.** Scheduling N vehicles onto a shared site cap with hard deadlines is
NP-hard. Lagrangian relaxation prices depot power as a congestion charge, and crucially yields a
**lower bound**, so the plan reports how close to optimal it provably is instead of asserting it
is good. Cross-checked against exhaustive search on small instances.

Two properties the tests enforce: the duality gap is `None` when load had to be shed (reporting a
small gap on a plan that failed to serve the depot would read as near-optimal at exactly the
moment we failed), and the unmanaged baseline is never crippled (making it artificially bad is
the easiest way to fake the measured saving).

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
docs/            ARCHITECTURE.md (source of truth), BENCHMARKS.md, THREAT-MODEL.md, adr/
services/
  simulator/     100k vehicles, 9 fault mechanisms, 3 OEM dialects, ground-truth ledger
  api/           FastAPI + the algorithm core + migrations
  ingest/        MQTT/OEM → normalise → dedup → ClickHouse + Mongo
  agent/         copilot: tool allowlist, injection screening, HITL, hash-chained audit
web/             React + Vite + MapLibre
infra/           compose/, helm/, terraform/
tests/
```

## Status

| Component | State |
| --- | --- |
| Algorithms A0–A9 | **Done**, tested, A0 cross-checked against brute force |
| Simulator: fleet, faults, telemetry, ground-truth ledger | **Done** |
| Ingest pipeline: normalise, dedup, DLQ, ClickHouse | **Done**, 578k events through it |
| PostgreSQL 3NF + FORCE RLS tenant isolation | **Done**, verified at DB and HTTP layers |
| ClickHouse schema + rollup MVs | **Done** |
| API: OIDC, RBAC, keyset pagination, rate limit | **Done** (JWKS verification unimplemented) |
| Web UI | **Done** |
| Randomised holdout + DiD endpoint | **Done** |
| Copilot with 4 guardrails | **Done** |
| ML re-ranker | **Done, ships disabled** — it lost to the rules |
| Helm, Terraform, STRIDE | **Written, not validated** (neither CLI installed here) |
| Observability | **Configured in Compose, not instrumented in code** |
| Chaos test, EXPLAIN ANALYZE evidence, location masking | **Not done** |

## Declarations

- All data is synthetic. No real personal or vehicle-owner data.
- Built with assistance from Claude (Anthropic) for design exploration, implementation, test
  authoring and documentation. At runtime the copilot calls Groq-hosted `openai/gpt-oss-120b`
  and `meta-llama/llama-prompt-guard-2-86m` for injection screening.
- Four competing architectures were generated and critiqued during planning; the final design
  synthesises two of them. Those drafts are not part of the submission.
