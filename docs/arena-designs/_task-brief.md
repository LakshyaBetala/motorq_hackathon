# Task: Design the winning solution architecture for the Connected Vehicle Intelligence Hackathon

You are designing (NOT yet implementing) the complete solution for a university-level enterprise
engineering hackathon. Your output is an **architecture and solution design document**. Another team
will build from it. You are competing against other designers; the strongest design wins.

## What you must produce

A single self-contained design document in Markdown. It must be concrete enough that an engineering
team could start building on Monday. Vague gestures at technology names score zero. Every choice
needs a stated reason AND a stated rejected alternative.

Your document MUST cover, in this order:

1. **Problem space chosen + why it wins.** You pick it (see the list below, or define your own).
   Justify it against the grading criteria, not just on interest. Name the primary user, the job to
   be done, the pain, and a defensible measurable impact number.
2. **Solution overview and user journey** — vehicle event -> detection -> insight -> action -> outcome.
3. **Feature list** with MoSCoW priority, each traceable to a service/code path.
4. **High-level architecture**: C4 L1 context + C4 L2 container view (describe them precisely in text
   and/or Mermaid). Every arrow must carry its protocol. Give expected latency per hop.
5. **Technology stack table**: layer | choice | why this, and what you rejected.
6. **Data architecture**:
   - Relational core in 3NF (Fleet, Vehicle, Driver, Trip, Alert, Subscription, etc.) — give the actual
     tables, keys and relationships. Document any deliberate denormalisation.
   - Polyglot map: which data lives in which store, and the CAP choice (CP or AP) for each, with
     PACELC latency reasoning.
   - Partitioning / sharding keys for telemetry, and why they avoid hot spots.
   - Capacity estimate: events/sec, bytes/event, daily + yearly volume, hot/warm/cold retention with
     a cost estimate.
   - The three slowest queries you anticipate, and the specific optimisation for each (composite /
     partial indexes, materialised views, keyset pagination, N+1 removal).
7. **Low-level design**: architecture style (layered / hexagonal / clean — justify), layer
   responsibilities and the allowed dependency direction, repository folder tree two levels deep,
   SOLID + 12-factor application points, and a design-pattern table (pattern | problem it solves here
   | where it lives).
8. **Algorithms and data structures** — this is heavily weighted. For each: the problem, the algorithm,
   pseudocode, time AND space complexity, and the scale you would test at. The brief explicitly
   suggests (you are not limited to these):
   - Graph: nearest reachable charger under remaining range (Dijkstra / A*); clusters of vehicles at
     unapproved depots (connected components)
   - Dynamic programming: optimal EV charging schedule under time-of-use tariffs; segmenting noisy
     GPS into stops and trips
   - Regex / parsing: VIN validation (17 chars, no I/O/Q, check digit); OBD-II DTC codes like P0301
   - Streaming: sliding-window aggregates, Bloom-filter de-duplication, Count-Min Sketch top-K,
     geohash indexing
9. **Event schemas and API contracts**: topics, keys, partition count, serialization format, schema
   evolution strategy, API versioning, pagination, error format, rate limits.
10. **Two sequence diagrams**, one of which MUST be a failure path (broker down / duplicate event /
    OEM changes payload format mid-flight).
11. **NFR plan** against the targets below: how each is achieved AND how it is measured.
12. **Security and compliance**: STRIDE threat model for the ingestion path and the public API with
    the top five threats and their controls; OAuth2/OIDC + RBAC + tenant isolation design; mTLS for
    devices; secrets; data minimisation, masking, retention and right-to-erasure flow.
13. **Test strategy**: what is tested at each level, with the specific tools, and how 80%+ coverage is
    reached on core logic. Name the edge cases (duplicate events, out-of-order data, unknown OEM
    format, broker outage).
14. **Observability**: metrics, logs, traces; what the dashboard shows; a worked troubleshooting
    walk-through for a latency spike.
15. **AI/ML and agentic component**: the decision it supports and why rules alone are insufficient;
    features; leakage checks; the baseline it is evaluated against and the metric; the agent's tool
    permissions, guardrails, prompt-injection defences, human-in-the-loop and audit trail.
    BE HONEST about the weakness of training an ML model on data from your own simulator, and say how
    you mitigate the circularity.
16. **3-5 ADRs** in Context -> Options Considered -> Decision -> Consequences form. They must cover
    databases, messaging, and at least one explicit CAP trade-off.
17. **Deployment**: containers, orchestration, IaC, CI/CD, and specifically how the SAME deployment
    runs on a second cloud with no code changes.
18. **Risks, technical debt, and what you would cut** if time ran short. Be honest about what is
    realistically achievable by a student team versus what is aspirational.
19. **A build order**: a phased plan so that at every checkpoint there is a working demoable system.

## The grading context (this is what you are optimising for)

Judges score: scale, data engineering, system design, security, testing and DevOps "at production
quality". The brief states explicitly: **"it is open-ended in what you build, but strict in how well
you build it"** and **"Depth beats breadth."** Testing is **"heavily weighted."**

### Minimum bar every solution must meet
- Works on simulated data from **at least 100,000 vehicles**, generated by your own data simulator
- Processes events in **real time** AND supports **batch analytics** over historical data
- Uses a combination of **relational, NoSQL and (where useful) vector** storage, each justified
- Exposes insights through **secure APIs and a usable web interface**
- Is **containerised, tested, and deployable on at least one cloud with no code changes needed for
  another**

### Scale parameters given in the brief
| Parameter | Value | Notes |
| --- | --- | --- |
| Connected vehicles | 100,000 | One mid-sized fleet customer base |
| Events per vehicle | 1 per second | Location, speed, battery, diagnostics |
| Ingest rate | ~100,000 events/sec | Bursts of 3x at shift start or network recovery |
| Event size | ~1 KB | JSON before compression |
| Raw volume | ~8.6 TB/day, ~3 PB/year | Before compression and down-sampling |

### Non-functional targets
| NFR | Target |
| --- | --- |
| Throughput | Sustain 100,000+ events/sec; survive a 3x burst for 5 minutes without data loss |
| Latency | Ingest to dashboard < 2 s; critical alert < 5 s; API p95 < 200 ms, p99 < 500 ms |
| Scalability | Stateless services scale horizontally; adding brokers or nodes needs no code change |
| Availability | No single point of failure; 99.9% target; recovers after a broker or pod is killed |
| Security | OAuth2/OIDC + JWT, RBAC with tenant isolation, mTLS for devices, TLS 1.3 in transit, AES-256 at rest, secrets in a vault, OWASP Top 10 and API Top 10 |
| Compliance | Audit logs for every data access and AI-agent action; masking of location data; retention and right-to-erasure (GDPR, India DPDP) |

### Testing expectations
| Test type | Minimum evidence |
| --- | --- |
| Unit | 80%+ coverage on core services and algorithms (pytest, JUnit 5 + Mockito) |
| Integration and contract | Real broker, DBs and cache via Testcontainers; contract tests (Pact) |
| Acceptance | BDD scenarios for key user stories (Cucumber, behave) |
| Performance and load | k6, Locust or Gatling at 100,000 events/sec; throughput, p95/p99, consumer lag; one soak test |
| Security | SAST (Semgrep, SonarQube), DAST (OWASP ZAP), dependency and image scans (Trivy) |
| Compliance and chaos | Verify audit trail and erasure; kill a pod or broker and prove recovery |

### Example tooling the brief names (you may pick ANY tool, but must justify)
- IoT ingestion: MQTT (EMQX, Mosquitto), Avro / Protobuf
- Messaging: Kafka (brief says "recommended") or RabbitMQ
- Big data: Kafka Streams, Flink, Spark on Hadoop/HDFS or S3, Parquet, Iceberg
- Persistence: PostgreSQL / TimescaleDB, Cassandra / MongoDB, Redis, Elasticsearch, Snowflake / ClickHouse
- Full stack: React; FastAPI (SQLModel), Spring Boot (Hibernate/JPA), ASP.NET Core, Go
- ML / vector: scikit-learn, PyTorch, pgvector, Qdrant
- Agentic AI: LangGraph, MCP tools, any LLM
- Log analytics: Splunk or ELK, OpenTelemetry, Prometheus + Grafana
- DevOps: Docker, Kubernetes / Helm, Terraform, GitHub Actions; AWS / GCP / Azure

### System design expectations named in the brief
CAP theorem per data type with PACELC latency trade-offs; partitioning and sharding; replication;
idempotency; exactly-once vs at-least-once delivery; back-pressure; circuit breakers; CQRS and event
sourcing; cache invalidation; horizontal scaling; graceful degradation; hot/warm/cold data lifecycle
with retention and cost estimate; microservices with clear boundaries and hexagonal or layered
internals.

### Candidate problem spaces offered by the brief (you may also define your own)
| Problem space | Who it helps | Example question |
| --- | --- | --- |
| Predictive maintenance | Fleet managers | Which vehicles are likely to break down in the next 7 days? |
| EV charging and battery health | EV fleets, OEMs | When and where should each vehicle charge at lowest cost? |
| Driver safety scoring | Fleets, insurers | Which drivers show risky patterns, and what should change? |
| Asset recovery | Lenders | Where is this vehicle now, and is it behaving abnormally? |
| Fuel, idling and utilisation cost | Fleet finance teams | Where are we losing money, and how much can we save? |
| Multi-OEM data normalisation | Platform teams | How do we onboard a new OEM format without downtime? |
| Privacy-safe data sharing | OEMs, regulators | How do we share insights without exposing personal data? |
| Fleet carbon reporting | Sustainability teams | What are our emissions per route, and how do we cut them? |

### Industry context you may use
The reference company is Motorq, a US connected-vehicle intelligence firm (founded 2016, Series B,
$50M+ raised, hubs in San Francisco / Seattle / Chennai / Bangalore). It serves 25+ automotive brands,
500K+ connected vehicles daily, 250B+ signals/month. Its platform is **device-free**: it pulls data
directly from OEM clouds via partnerships rather than aftermarket OBD-II dongles, then normalises
everything into one standard schema. Customers consume data via portal, REST APIs and Kafka streams.
"Motorq Fuse" (April 2026) is an AI layer that flags issues such as excessive idling, recommends
actions and estimates the dollar impact. Motorq reports a 68% reduction in vehicle recovery time for
lenders.

Relevant 2026->2030 trends: ~25 GB/hour per connected car; connected cars ~95% of new vehicles sold by
2030; most raw data must be filtered at the edge. Software-Defined Vehicles get OTA updates so data
formats change often. Mixed ICE/EV fleets need charging optimisation, battery state-of-health tracking
and range prediction. AWS IoT FleetWise closed to new customers in April 2026 — a live reminder of
single-vendor lock-in risk. Agentic AI is moving from dashboards to taking actions. Regulation: UNECE
R155/R156 vehicle cybersecurity and software-update management; India's DPDP Rules notified November
2025; GDPR in Europe.

Illustrative telemetry event (design your own schema, do not just copy this):
```json
{"vin":"1HGCM82633A004352","ts":"2026-09-25T10:15:02.120Z","lat":21.1702,"lon":72.8311,
 "speed_kmh":64.2,"soc_pct":41,"odo_km":18234.7,"dtc":["P0301"],"evt":"HARSH_BRAKE","seq":88412}
```

## Hard constraints on your design

- **Student team, hackathon timebox.** The design must be buildable, and you must be explicit about
  what is actually built versus what is designed-and-stubbed. A design that could only be delivered by
  a 30-person team over a year is a losing design, no matter how elegant. Reviewers will run
  `docker compose up` on a laptop.
- **It must genuinely run on a developer laptop** for the demo, while the architecture must credibly
  scale to the stated numbers. Explain how you reconcile those two (e.g. scaled-down local profile
  versus cloud profile) — this tension is one of the hardest parts of the task and designs that ignore
  it will be attacked.
- **100,000 events/sec on a laptop is not physically realistic.** Say honestly how you demonstrate the
  throughput claim (e.g. a measured per-core/per-partition rate plus a documented extrapolation, or a
  short cloud burst test) rather than pretending.
- Data must be synthetic only. No real personal or vehicle-owner data.
- Deliverables that exist alongside your design: completed Solution Document, a git repo with README
  and one-command `docker compose` setup and a seeded 100K-vehicle dataset, architecture + ER diagrams,
  3-5 ADRs, a working end-to-end demo, test evidence (coverage, load, security scans, CI link), a
  DevOps pack (Dockerfiles, Helm/K8s manifests, Terraform for at least one cloud, STRIDE threat
  model), an algorithms + SQL write-up with complexity analysis and before/after query plans, and a
  demo video of at most 5 minutes.

## How you will be judged in this arena

- **Concreteness** beats breadth. Actual table definitions, actual partition keys, actual complexity
  bounds, actual numbers. Buzzword lists lose.
- **Honesty about trade-offs and limits** is rewarded. Every design has a weak point; naming yours and
  mitigating it beats hiding it.
- **Internal consistency.** If you claim a latency budget, the hop-by-hop numbers must add up. If you
  claim a throughput, the partition count and consumer parallelism must support it.
- **Buildability.** A phased plan where something demoable exists at every checkpoint.
- **Depth in the heavily-weighted areas**: testing, data engineering, system design, algorithms.
- Do not pad. A tight, rigorous document beats a long vague one.
