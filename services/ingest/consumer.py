"""Ingest: Redpanda -> normalise -> dedup -> ClickHouse.

Design points that matter, each of which is a thing that breaks if done naively:

* **Batched inserts.** ClickHouse merges on write; inserting row-by-row creates one part per row
  and the merge scheduler collapses within minutes. We buffer and insert in blocks, which is the
  single largest throughput lever in the whole pipeline.
* **Idempotent on (vin, seq).** A Bloom filter (A6) absorbs the at-least-once duplicates the
  upstream produces, in bounded memory. False positives drop a real event, so the rate is
  reported as a metric rather than assumed.
* **Offsets commit after the insert, never before.** Committing first turns a crash into silent
  data loss; committing after means a crash replays a batch, which the dedup absorbs. That is
  at-least-once delivery plus idempotent application, which is what "effectively once" means in
  practice.
* **Rejects go to the DLQ with the raw bytes.** A payload we cannot map is kept verbatim so it
  can be replayed once the registry is corrected. Dropping it would lose data permanently on an
  OEM format change, which is the exact scenario the architecture is built around.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

import clickhouse_connect  # noqa: E402
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer  # noqa: E402

from readyline.algorithms.streaming import BloomFilter, dedup_key  # noqa: E402
from readyline.normalise import NormalisationError, normalise  # noqa: E402


@dataclass(frozen=True, slots=True)
class IngestConfig:
    brokers: str = os.environ.get("KAFKA_BROKERS", "localhost:9092")
    topic: str = os.environ.get("TELEMETRY_TOPIC", "telemetry.raw")
    dlq_topic: str = os.environ.get("TELEMETRY_DLQ", "telemetry.dlq")
    group: str = os.environ.get("INGEST_GROUP", "ingest-v1")
    ch_host: str = os.environ.get("CLICKHOUSE_HOST", "localhost")
    ch_port: int = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
    ch_user: str = os.environ.get("CLICKHOUSE_USER", "voltroute")
    ch_password: str = os.environ.get("CLICKHOUSE_PASSWORD", "devonly")
    ch_database: str = os.environ.get("CLICKHOUSE_DB", "voltroute")
    batch_rows: int = int(os.environ.get("INGEST_BATCH_ROWS", "20000"))
    batch_seconds: float = float(os.environ.get("INGEST_BATCH_SECONDS", "2.0"))
    dedup_capacity: int = int(os.environ.get("INGEST_DEDUP_CAPACITY", "2000000"))
    idle_exit_s: float = float(os.environ.get("INGEST_IDLE_EXIT_S", "0"))


COLUMNS = [
    "fleet_id", "vin", "ts", "seq", "lat", "lon", "geohash7", "speed_kmh",
    "soc_pct", "soh_pct", "pack_temp_c", "odo_km", "fuel_lvl_pct", "evt", "dtc",
]


@dataclass(slots=True)
class Metrics:
    consumed: int = 0
    inserted: int = 0
    duplicates: int = 0
    rejected: int = 0
    batches: int = 0
    started: float = field(default_factory=time.perf_counter)

    def snapshot(self) -> dict[str, float]:
        elapsed = max(1e-9, time.perf_counter() - self.started)
        return {
            "consumed": self.consumed,
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "batches": self.batches,
            "elapsed_s": round(elapsed, 3),
            "events_per_sec": round(self.consumed / elapsed, 1),
        }


def _geohash7(lat: float, lon: float) -> str:
    from readyline.algorithms.streaming import geohash_encode
    return geohash_encode(lat, lon, 7)


def _to_row(rec: dict, *, tenant_id: int, fleet_id: int) -> list:
    from datetime import datetime, timezone
    return [
        fleet_id,
        rec["vin"],
        datetime.fromtimestamp(float(rec["ts"]), tz=timezone.utc),
        int(rec["seq"]),
        float(rec["lat"]),
        float(rec["lon"]),
        _geohash7(float(rec["lat"]), float(rec["lon"])),
        float(rec.get("speed_kmh") or 0.0),
        rec.get("soc_pct"),
        rec.get("soh_pct"),
        rec.get("pack_temp_c"),
        float(rec.get("odo_km") or 0.0),
        rec.get("fuel_pct"),
        rec.get("evt") or "TICK",
        list(rec.get("dtc") or ()),
    ]


def run(cfg: IngestConfig | None = None) -> dict[str, float]:
    cfg = cfg or IngestConfig()

    client = clickhouse_connect.get_client(
        host=cfg.ch_host, port=cfg.ch_port, username=cfg.ch_user,
        password=cfg.ch_password, database=cfg.ch_database,
    )
    consumer = Consumer({
        "bootstrap.servers": cfg.brokers,
        "group.id": cfg.group,
        "auto.offset.reset": "earliest",
        # Manual commit: offsets advance only after ClickHouse has the rows.
        "enable.auto.commit": False,
        "fetch.min.bytes": 1 << 16,
        "fetch.wait.max.ms": 200,
        "max.partition.fetch.bytes": 1 << 22,
        "session.timeout.ms": 45000,
    })
    consumer.subscribe([cfg.topic])
    dlq = Producer({"bootstrap.servers": cfg.brokers, "linger.ms": 50})

    bloom = BloomFilter(expected_items=cfg.dedup_capacity, false_positive_rate=0.01)
    metrics = Metrics()
    buffer: list[list] = []
    last_flush = time.perf_counter()
    last_message = time.perf_counter()
    stopping = False

    def _stop(*_a) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass

    def flush() -> None:
        nonlocal buffer, last_flush
        if buffer:
            client.insert(table="telemetry", data=buffer, column_names=COLUMNS)
            metrics.inserted += len(buffer)
            metrics.batches += 1
            buffer = []
        # Commit only after the insert has returned. A crash before this replays the batch,
        # which the Bloom filter absorbs; committing first would lose it silently.
        try:
            consumer.commit(asynchronous=False)
        except KafkaException as exc:
            # _NO_OFFSET simply means this consumer has nothing to commit yet (no partitions
            # assigned, or no messages read). Anything else is real and must surface.
            if exc.args[0].code() != KafkaError._NO_OFFSET:
                raise
        last_flush = time.perf_counter()

    try:
        while not stopping:
            msgs = consumer.consume(num_messages=5000, timeout=0.5)
            now = time.perf_counter()

            if not msgs:
                if buffer and now - last_flush >= cfg.batch_seconds:
                    flush()
                if cfg.idle_exit_s and now - last_message > cfg.idle_exit_s:
                    break
                continue

            last_message = now
            for msg in msgs:
                if msg.error():
                    if msg.error().code() != KafkaError._PARTITION_EOF:
                        metrics.rejected += 1
                    continue

                metrics.consumed += 1
                raw = msg.value()
                # Platform metadata rides in headers, attached by the gateway after it resolved
                # ownership from the device certificate. The OEM body never carries tenancy.
                dialect = "tata_v2"
                tenant_id = 0
                fleet_id = 0
                for key, value in (msg.headers() or ()):
                    text = value.decode() if isinstance(value, bytes) else str(value)
                    if key == "dialect":
                        dialect = text
                    elif key == "tenant_id":
                        tenant_id = int(text)
                    elif key == "fleet_id":
                        fleet_id = int(text)

                if not tenant_id or not fleet_id:
                    # Un-attributed telemetry must never be stored: it would be invisible to
                    # every tenant-scoped query and would silently corrupt per-tenant billing.
                    metrics.rejected += 1
                    dlq.produce(cfg.dlq_topic, key=msg.key(), value=raw,
                                headers=[("reason", b"missing_tenancy")])
                    continue

                try:
                    rec = normalise(json.loads(raw), dialect)
                except (NormalisationError, json.JSONDecodeError, KeyError) as exc:
                    metrics.rejected += 1
                    reason = getattr(exc, "reason", exc.__class__.__name__)
                    # Raw bytes, verbatim, so a corrected mapping can replay them.
                    dlq.produce(cfg.dlq_topic, key=msg.key(), value=raw,
                                headers=[("reason", str(reason).encode()),
                                         ("dialect", dialect.encode())])
                    continue

                if not bloom.add_if_absent(dedup_key(rec["vin"], int(rec["seq"]))):
                    metrics.duplicates += 1
                    continue

                try:
                    buffer.append(_to_row(rec, tenant_id=tenant_id, fleet_id=fleet_id))
                except (TypeError, ValueError, KeyError):
                    metrics.rejected += 1
                    dlq.produce(cfg.dlq_topic, key=msg.key(), value=raw,
                                headers=[("reason", b"row_build_failed")])

            if len(buffer) >= cfg.batch_rows or (now - last_flush) >= cfg.batch_seconds:
                flush()
    finally:
        try:
            flush()
        except Exception as exc:  # noqa: BLE001 - last-ditch; we still want the metrics out
            print(f"final flush failed: {exc}", file=sys.stderr)
        dlq.flush(10)
        consumer.close()

    stats = metrics.snapshot()
    stats["dedup_fp_rate"] = round(bloom.estimated_false_positive_rate(), 6)
    print(json.dumps(stats), flush=True)
    return stats


if __name__ == "__main__":
    run()
