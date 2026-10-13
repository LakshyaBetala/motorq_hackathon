# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**ReadyLine** — a submission for the Talenciaglobal / Motorq "Connected Vehicle Intelligence"
hackathon. By 16:00 it commits tonight's depot charging plan — which vehicle, which charger,
starting when, to what SoC — so every route tomorrow completes, under the site's electrical cap,
at minimum cost. The Morning Board then shows only the exceptions to that commitment.

Two source documents in the repo root define the grading. They are **gitignored on purpose** —
never commit or push them:
- `Motorq_Hackathon_Problem_Statement.pdf`
- `Motorq_Hackathon_Solution_Document_Template.docx`

**Read `docs/ARCHITECTURE.md` before changing anything.** The problem space and stack are locked.
`docs/arena-designs/` holds four competing designs from planning — reference material only,
superseded.

## Commands

```bash
make up          # docker compose: redpanda, clickhouse, postgres, mongo, redis, emqx, keycloak, prom, grafana
make seed        # 100,000 vehicles across 60 tenants into PostgreSQL (~5 s, uses COPY)
make experiment  # 30 nights of both holdout arms into ClickHouse (~90 s)
make stream      # produce telemetry into Redpanda
make ingest      # consume Redpanda -> normalise -> ClickHouse
make api         # FastAPI on :8000
make web         # Vite UI on :5173, proxies /api to :8000
make test        # all 200 tests
make test-cov    # coverage report (currently 88%)
make bench       # 4 producers + 4 consumers, throughput measurement
```

Single test:
```bash
python -m pytest tests/test_depot.py::test_matches_brute_force_optimum_on_a_small_instance -x
python -m pytest tests/test_agent.py -q          # no API key needed, LLM is stubbed
```

Integration tests skip automatically when the stack is down. **CI fails if the tenant-isolation
tests skip** — a silent skip is a green build with no isolation coverage.

## Environment gotchas on this machine

- `docker exec ... -f /tmp/x.sql` fails: Git Bash rewrites the container path. Prefix with
  `MSYS_NO_PATHCONV=1`.
- `PYTHONPATH` needs `;` not `:` on Windows: `PYTHONPATH="services/api;services/simulator"`.
- Several services bind IPv4 only while `localhost` resolves to `::1`. Use `127.0.0.1` in health
  checks. This bug has now appeared twice (ClickHouse healthcheck, Vite dev server).
- `pkill -f uvicorn` does not kill the Windows process. Use PowerShell `Get-CimInstance
  Win32_Process | Where CommandLine -like '*uvicorn*'`. A stale server running old code cost an
  hour of debugging once already.
- Python prints to a cp1252 console and dies on `—` or `≈`. Use `python -X utf8`.

## Five things that are easy to get wrong

**1. The 100,000 vehicles are multi-tenant** — ~60 tenants, largest ~16,000, one dispatcher owns
~250. Every query, index and API route carries tenant scope.

**2. Rules decide; ML only re-ranks.** The re-ranker reorders what VEWS already surfaced and may
never surface an alert itself (ADR-0003). It is also **currently disabled**, because it lost to
the rules: precision@12 0.656 vs 0.625 over 8 splits. Do not enable it without re-running
`evaluate_repeated` and beating the rules by more than 1/12.

**3. The Morning Board is hard-capped at 12.** Raising the cap to improve recall defeats the
product thesis — the pain is alarm fatigue, not detection.

**4. A0's per-vehicle subproblem must stay greedy, not DP.** The DP discretises SoC into buckets,
so its optimum is costlier than the continuous one, which makes the summed Lagrangian "lower
bound" exceed the achievable cost — i.e. not a bound. There is a test for this.

**5. The duality gap is `None` when load was shed.** Reporting a small gap on a plan that failed
to serve the depot would read as near-optimal at exactly the moment we failed.

## Architecture in one pass

```
vehicles ─┬─ MQTT 5 + mTLS ────────┐
          └─ OEM cloud webhook/poll┘→ Redpanda[telemetry.raw, key=VIN, 64 parts]
                   ├→ ingest (Python) → normalise → ClickHouse (AP) + MongoDB (raw envelopes)
                   └→ detector → EWMA → VEWS → alerts → PostgreSQL (CP) + Redis pub/sub
                                                       ↓
                             FastAPI ← Redis (live state, rate limit, Bloom dedup)
                                ↓
                             React + MapLibre
```

**ClickHouse is AP and holds telemetry; PostgreSQL is CP and holds fleet/billing/RBAC/audit.**
Never put billing or audit rows in ClickHouse, never put raw telemetry in Postgres.

**MongoDB holds un-normalised OEM envelopes** so a mid-flight format change is fixed in the
registry and *replayed*. A new dialect is a versioned entry promoted out of shadow mode, not a
deploy.

**Tenancy travels in Kafka headers**, never in the OEM payload — an OEM does not know our
customer ids. An event without resolved tenancy goes to the DLQ rather than being stored
unattributed.

**Everything is Python.** The architecture doc originally specified Go for ingest and detector;
Go is not installed on this machine and a single language removed an integration risk that was
not worth carrying. The hot path is I/O-bound batching, so Go's advantage was smaller than it
looked. Measured result in `docs/BENCHMARKS.md`.

### Two profiles, one codebase

`local` and `scale` differ **only by environment variables**. A code branch on profile breaks
12-factor and the cloud-agnostic claim.

### Tenant scope

Set per transaction with `set_config('app.tenant_id', ..., true)` so it unwinds on commit. A
session-level setting lets a pooled connection carry the previous caller's tenant into the next
request. RLS is `FORCE`d and the API connects as `readyline_app`, which owns nothing.

## The graded core

Ten algorithms in `docs/ARCHITECTURE.md` §5, each with a complexity bound and a test. A0 (depot
scheduling under a shared power cap) is the centrepiece and is cross-checked against brute force.

**The simulator emits a ground-truth fault and cost ledger.** Keep it out of every product read
path — if production code reads it, every metric becomes circular.

Current state: **200 tests, 88% coverage**. Claims without a measurement lose marks; every number
in the docs traces to a `make` target.

## Conventions

- All data is synthetic. Never commit real VINs, owner data or location traces.
- Secrets in `.env` (gitignored). `.env.example` stays current. **Never commit an API key** —
  `GROQ_API_KEY` lives only in `.env`.
- Honesty is a graded feature here. `docs/BENCHMARKS.md`, `docs/THREAT-MODEL.md` and
  §12 of `docs/SOLUTION-DOCUMENT.md` state what is *not* done. Do not quietly close those gaps
  by softening the wording — close them with code or leave them stated.
- Final submission is tagged `v1.0-submission`.
