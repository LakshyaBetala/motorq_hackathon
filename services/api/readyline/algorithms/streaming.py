"""A6 Bloom de-duplication, A7 Count-Min top-K, A8 geohash + union-find depot clustering.

These are the streaming structures. All three trade exactness for bounded memory, which is the
whole point at 100k events/sec: an exact dedup set over a day of traffic is 8.6 billion entries,
while the Bloom filter below is ~1.2 MB per hour per shard.

Every approximation here is one-sided, and the direction matters:
  - Bloom: may claim "seen" for an unseen event (drops a real event), never the reverse.
  - Count-Min: may over-count, never under-count.
Both error directions are stated with each class because choosing them wrongly is how these
structures cause silent data loss.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Iterable, Iterator

# ----------------------------------------------------------------- A6 Bloom filter


class BloomFilter:
    """Fixed-size Bloom filter for (vin, seq) duplicate suppression.

    Sizing: m = -n ln(p) / (ln 2)^2, k = (m/n) ln 2 — the standard optimal-k result.

    False positives drop a genuine event. We therefore run this per (vehicle, hour) shard with a
    1% target, and the ingest service emits `dedup_false_positive_risk` so the rate is visible
    rather than assumed. An exact Redis SET is used for CRITICAL-lane events, where dropping one
    is unacceptable and the volume is small enough to afford it.

    add/contains: O(k) hashes, k ~ 7 at p=0.01. Space: m bits, independent of n.
    """

    __slots__ = ("_m", "_k", "_bits", "_added")

    def __init__(self, expected_items: int, false_positive_rate: float = 0.01) -> None:
        if expected_items <= 0:
            raise ValueError("expected_items must be positive")
        if not 0.0 < false_positive_rate < 1.0:
            raise ValueError("false_positive_rate must be in (0, 1)")
        ln2 = math.log(2)
        self._m = max(8, int(math.ceil(-expected_items * math.log(false_positive_rate) / (ln2 ** 2))))
        self._k = max(1, int(round((self._m / expected_items) * ln2)))
        self._bits = bytearray((self._m + 7) // 8)
        self._added = 0

    @property
    def bits(self) -> int:
        return self._m

    @property
    def hashes(self) -> int:
        return self._k

    @property
    def size_bytes(self) -> int:
        return len(self._bits)

    def _positions(self, key: bytes) -> Iterator[int]:
        # Kirsch-Mitzenmacher: two independent hashes synthesise k, instead of k separate
        # digests. Same asymptotic false-positive rate, k-times less hashing on the hot path.
        digest = hashlib.blake2b(key, digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "little")
        h2 = int.from_bytes(digest[8:], "little") | 1  # odd, so it generates the full cycle
        for i in range(self._k):
            yield ((h1 + i * h2) % self._m)

    def add(self, key: bytes) -> None:
        for pos in self._positions(key):
            self._bits[pos >> 3] |= 1 << (pos & 7)
        self._added += 1

    def __contains__(self, key: bytes) -> bool:
        return all(self._bits[pos >> 3] & (1 << (pos & 7)) for pos in self._positions(key))

    def add_if_absent(self, key: bytes) -> bool:
        """Return True if the key was newly added (i.e. the event is not a duplicate).

        This is the single call the ingest path makes, so the check and the set are one pass.
        """
        if key in self:
            return False
        self.add(key)
        return True

    def estimated_false_positive_rate(self) -> float:
        """Current theoretical FP rate given how many items are actually in the filter."""
        if self._added == 0:
            return 0.0
        return (1 - math.exp(-self._k * self._added / self._m)) ** self._k


def dedup_key(vin: str, seq: int) -> bytes:
    """Stable key for duplicate detection. (vin, seq) is the OEM's own idempotency pair."""
    return f"{vin}:{seq}".encode()


# ----------------------------------------------------------------- A7 Count-Min Sketch


class CountMinSketch:
    """Count-Min sketch for top-K cost leaks / worst degraders.

    Error is one-sided: estimates are never below the true count, and exceed it by more than
    eps*N with probability < delta. Over-counting is the safe direction here — it can promote a
    vehicle onto a review list that did not deserve it, but never hides one that did.

    update: O(d). query: O(d). Space: w*d counters, with w = ceil(e/eps), d = ceil(ln(1/delta)).
    """

    __slots__ = ("_w", "_d", "_table", "_total")

    def __init__(self, epsilon: float = 0.001, delta: float = 0.01) -> None:
        self._w = int(math.ceil(math.e / epsilon))
        self._d = int(math.ceil(math.log(1 / delta)))
        self._table = [[0] * self._w for _ in range(self._d)]
        self._total = 0

    @property
    def shape(self) -> tuple[int, int]:
        return self._d, self._w

    def _columns(self, key: str) -> Iterator[int]:
        digest = hashlib.blake2b(key.encode(), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "little")
        h2 = int.from_bytes(digest[8:], "little") | 1
        for i in range(self._d):
            yield (h1 + i * h2) % self._w

    def update(self, key: str, count: int = 1) -> None:
        self._total += count
        for row, col in enumerate(self._columns(key)):
            self._table[row][col] += count

    def estimate(self, key: str) -> int:
        """Minimum across rows — the estimator that gives the one-sided guarantee."""
        return min(self._table[row][col] for row, col in enumerate(self._columns(key)))

    @property
    def total(self) -> int:
        return self._total


@dataclass(slots=True)
class TopK:
    """Top-K by estimated count, backed by a Count-Min sketch.

    The sketch bounds memory over an unbounded key space; the explicit candidate dict is capped
    at `capacity` so the heap cannot grow without limit either. We keep exact counts for the
    current candidates and fall back to the sketch for eviction decisions.

    offer: O(log k) amortised. Space: O(k) on top of the sketch.
    """

    k: int
    capacity: int = 0
    _sketch: CountMinSketch = field(default_factory=CountMinSketch, repr=False)
    _counts: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.k <= 0:
            raise ValueError("k must be positive")
        if self.capacity <= 0:
            self.capacity = self.k * 10

    def offer(self, key: str, count: int = 1) -> None:
        self._sketch.update(key, count)
        if key in self._counts:
            self._counts[key] += count
            return
        if len(self._counts) < self.capacity:
            self._counts[key] = self._sketch.estimate(key)
            return
        # Evict the weakest candidate, but only if the newcomer actually beats it.
        weakest = min(self._counts, key=self._counts.__getitem__)
        if self._sketch.estimate(key) > self._counts[weakest]:
            del self._counts[weakest]
            self._counts[key] = self._sketch.estimate(key)

    def top(self) -> list[tuple[str, int]]:
        return sorted(self._counts.items(), key=lambda kv: (-kv[1], kv[0]))[: self.k]


# ----------------------------------------------------------------- A8 geohash + union-find

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"  # geohash alphabet: no a, i, l, o


def geohash_encode(lat: float, lon: float, precision: int = 7) -> str:
    """Encode a position as a geohash.

    Precision 7 is ~153 m x 153 m, chosen to match the radius within which two parked vehicles
    should be considered "at the same place". Precision 8 (~38 m) splits a single depot across
    cells; precision 6 (~1.2 km) merges a depot with the street outside it.

    Time O(precision * 5). Space O(precision).
    """
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise ValueError(f"position out of range: {lat},{lon}")
    lat_range, lon_range = [-90.0, 90.0], [-180.0, 180.0]
    out: list[str] = []
    bits = 0
    bit_count = 0
    even = True  # longitude first
    while len(out) < precision:
        if even:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon > mid:
                bits = (bits << 1) | 1
                lon_range[0] = mid
            else:
                bits <<= 1
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat > mid:
                bits = (bits << 1) | 1
                lat_range[0] = mid
            else:
                bits <<= 1
                lat_range[1] = mid
        even = not even
        bit_count += 1
        if bit_count == 5:
            out.append(_BASE32[bits])
            bits = 0
            bit_count = 0
    return "".join(out)


def geohash_neighbours(gh: str) -> set[str]:
    """The 8 cells around `gh`, plus `gh` itself.

    Needed because two vehicles 10 m apart can still land in different cells when they straddle a
    cell boundary. Clustering on the cell alone would split such a depot in two.
    """
    lat, lon, lat_err, lon_err = _geohash_decode_with_error(gh)
    p = len(gh)
    cells = {gh}
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            nlat = lat + dlat * lat_err * 2
            nlon = lon + dlon * lon_err * 2
            if -90.0 <= nlat <= 90.0 and -180.0 <= nlon <= 180.0:
                cells.add(geohash_encode(nlat, nlon, p))
    return cells


def _geohash_decode_with_error(gh: str) -> tuple[float, float, float, float]:
    lat_range, lon_range = [-90.0, 90.0], [-180.0, 180.0]
    even = True
    for ch in gh:
        idx = _BASE32.index(ch)
        for mask in (16, 8, 4, 2, 1):
            bit = 1 if idx & mask else 0
            rng = lon_range if even else lat_range
            mid = (rng[0] + rng[1]) / 2
            if bit:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    lat = (lat_range[0] + lat_range[1]) / 2
    lon = (lon_range[0] + lon_range[1]) / 2
    return lat, lon, (lat_range[1] - lat_range[0]) / 2, (lon_range[1] - lon_range[0]) / 2


class UnionFind:
    """Union-find with path compression and union by rank. Near-constant amortised: O(alpha(n))."""

    __slots__ = ("_parent", "_rank")

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def add(self, x: str) -> None:
        if x not in self._parent:
            self._parent[x] = x
            self._rank[x] = 0

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # path compression
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1


@dataclass(frozen=True, slots=True)
class ParkingCluster:
    geohashes: frozenset[str]
    vins: frozenset[str]
    is_approved: bool


def cluster_parked_vehicles(
    parked: Iterable[tuple[str, float, float]],
    approved_geohashes: set[str],
    precision: int = 7,
    min_vehicles: int = 3,
) -> list[ParkingCluster]:
    """Find groups of vehicles parked together, and flag those not at an approved depot.

    Connected components over geohash adjacency: two cells join when they are neighbours and both
    hold a parked vehicle. This finds depots that nobody registered — a real utilisation leak and
    sometimes a theft signal.

    Time O(n * 9 * alpha(n)) ~ O(n). Space O(n).
    """
    by_cell: dict[str, set[str]] = {}
    for vin, lat, lon in parked:
        by_cell.setdefault(geohash_encode(lat, lon, precision), set()).add(vin)

    uf = UnionFind()
    occupied = set(by_cell)
    for cell in occupied:
        uf.add(cell)
        for neighbour in geohash_neighbours(cell):
            if neighbour in occupied:
                uf.union(cell, neighbour)

    grouped: dict[str, tuple[set[str], set[str]]] = {}
    for cell, vins in by_cell.items():
        root = uf.find(cell)
        cells_acc, vins_acc = grouped.setdefault(root, (set(), set()))
        cells_acc.add(cell)
        vins_acc.update(vins)

    clusters: list[ParkingCluster] = []
    for cells_acc, vins_acc in grouped.values():
        if len(vins_acc) < min_vehicles:
            continue
        clusters.append(
            ParkingCluster(
                geohashes=frozenset(cells_acc),
                vins=frozenset(vins_acc),
                is_approved=bool(cells_acc & approved_geohashes),
            )
        )
    return sorted(clusters, key=lambda c: (-len(c.vins), sorted(c.geohashes)[0]))
