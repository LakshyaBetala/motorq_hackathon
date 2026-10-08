"""Producer: simulator -> Redpanda.

Keyed by VIN, which is ADR-0002: VINs hash uniformly so load spreads evenly, and per-vehicle
ordering is preserved, which trip segmentation and sequence de-duplication both depend on.
Keying by fleet_id would put the 18,000-vehicle tenant on one partition and cap the platform.

Each vehicle emits in its OEM's native dialect. The consumer normalises. That split is the whole
point: adding an OEM must not require touching the producer or the consumer, only the registry.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass

from confluent_kafka import Producer

from .faults import schedule_faults
from .fleet import generate_depots, generate_vehicles, plan_tenants
from .telemetry import encode, generate_events, seed_positions


@dataclass(frozen=True, slots=True)
class StreamConfig:
    brokers: str = os.environ.get("KAFKA_BROKERS", "localhost:9092")
    topic: str = os.environ.get("TELEMETRY_TOPIC", "telemetry.raw")
    vehicles: int = int(os.environ.get("SIM_LIVE_VEHICLES", "2000"))
    total_fleet: int = int(os.environ.get("SIM_VEHICLES", "100000"))
    tenants: int = int(os.environ.get("SIM_TENANTS", "60"))
    hz: float = float(os.environ.get("SIM_EVENTS_PER_VEHICLE_SEC", "1"))
    duration_s: float = float(os.environ.get("SIM_DURATION_S", "60"))
    seed: int = int(os.environ.get("SIM_SEED", "20261008"))
    # Unthrottled is the throughput benchmark; throttled mirrors wall-clock for the demo.
    realtime: bool = os.environ.get("SIM_REALTIME", "false").lower() == "true"


def build_producer(cfg: StreamConfig) -> Producer:
    return Producer({
        "bootstrap.servers": cfg.brokers,
        # Batch aggressively. Per-message round trips cap throughput around 5k/s regardless of
        # how fast the generator runs; with batching the broker is no longer the bottleneck.
        "linger.ms": 20,
        "batch.size": 1 << 20,
        "compression.type": "lz4",
        "queue.buffering.max.messages": 1_000_000,
        "queue.buffering.max.kbytes": 1_048_576,
        # acks=1 not acks=all: telemetry is the AP side of the design (ADR-0001). Losing a
        # handful of 1 Hz samples on a broker failure is acceptable; blocking ingest is not.
        "acks": "1",
        "enable.idempotence": False,
    })


def run(cfg: StreamConfig | None = None) -> dict[str, float]:
    cfg = cfg or StreamConfig()
    start_ts = time.time()

    tenants = plan_tenants(cfg.total_fleet, cfg.tenants, seed=cfg.seed)
    depots = generate_depots(tenants, seed=cfg.seed)
    all_vehicles = generate_vehicles(tenants, depots, seed=cfg.seed)

    live = []
    for v in all_vehicles:
        live.append(v)
        if len(live) >= cfg.vehicles:
            break

    _, ledger_by_vin = schedule_faults(
        iter(live), horizon_days=30.0, start_ts=start_ts, seed=cfg.seed
    )
    seed_positions(live, {d.depot_id: (d.lat, d.lon) for d in depots})
    dialect_of = {v.vin: v.model.dialect for v in live}

    producer = build_producer(cfg)
    stopping = False

    def _stop(*_a) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass  # not available on all platforms

    sent = 0
    errors = 0
    t0 = time.perf_counter()

    def _on_delivery(err, _msg) -> None:
        nonlocal errors
        if err is not None:
            errors += 1

    for ev in generate_events(
        live, ledger_by_vin, start_ts=start_ts, duration_s=cfg.duration_s,
        hz=cfg.hz, seed=cfg.seed,
    ):
        if stopping:
            break
        payload = encode(ev, dialect_of[ev.vin])
        # Headers carry PLATFORM metadata; the body stays the OEM's bytes, untouched, so it can
        # be stored raw in Mongo and replayed through a corrected mapping later.
        #
        # tenant_id/fleet_id are deliberately not in the body: an OEM has no idea which of our
        # customers owns a vehicle. In production the ingest gateway resolves ownership from the
        # device's mTLS certificate (or the OEM connector's credentials) against the vehicle
        # registry, then attaches it here. This producer stands in for that gateway.
        # Without this, every event landed in fleet_id 1 and tenant isolation in ClickHouse was
        # silently void.
        while True:
            try:
                producer.produce(
                    cfg.topic,
                    key=ev.vin.encode(),
                    value=json.dumps(payload, separators=(",", ":")).encode(),
                    headers=[
                        ("dialect", dialect_of[ev.vin].encode()),
                        ("tenant_id", str(ev.tenant_id).encode()),
                        ("fleet_id", str(ev.fleet_id).encode()),
                    ],
                    on_delivery=_on_delivery,
                )
                break
            except BufferError:
                # Back-pressure: the local queue is full. Serve delivery callbacks and retry
                # rather than dropping. This is the producer honouring back-pressure instead of
                # pretending it does not exist.
                producer.poll(0.1)

        sent += 1
        if sent % 10_000 == 0:
            producer.poll(0)
        if cfg.realtime and sent % max(1, int(cfg.vehicles * cfg.hz)) == 0:
            time.sleep(1.0 / max(cfg.hz, 0.001))

    producer.flush(30)
    elapsed = time.perf_counter() - t0

    stats = {
        "sent": sent,
        "errors": errors,
        "elapsed_s": round(elapsed, 3),
        "events_per_sec": round(sent / elapsed, 1) if elapsed > 0 else 0.0,
    }
    print(json.dumps(stats), flush=True)
    return stats


if __name__ == "__main__":
    result = run()
    sys.exit(1 if result["errors"] else 0)
