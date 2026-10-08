"""A3 segmenting noisy GPS into stops and trips.

This is not a convenience feature. Idle time *is* the output of this algorithm, and idle time is
one of the three cost drivers in the impact model, so a sloppy segmenter corrupts the headline
number. It is also what populates `trip` rows, which every downstream analytic joins against.

Why a DP rather than a threshold rule: a speed threshold mislabels three common real situations.
  1. Stop-and-go traffic looks like dozens of micro-stops. A rule emits 40 trips for one journey.
  2. GPS drift at a standstill shows 2-4 km/h of phantom motion, so a parked vehicle never stops.
  3. A genuine 90-second delivery stop sits below any threshold that also tolerates (1) and (2).
The DP optimises a global cost over the whole sequence instead of deciding point by point, so a
brief excursion cannot split a stop and a brief pause cannot split a trip.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

from .energy import haversine_km

State = Literal["STOP", "MOVE"]


@dataclass(frozen=True, slots=True)
class Fix:
    """One GPS fix."""

    ts: float          # epoch seconds
    lat: float
    lon: float
    speed_kmh: float


@dataclass(frozen=True, slots=True)
class Segment:
    state: State
    start_index: int
    end_index: int     # inclusive
    start_ts: float
    end_ts: float
    distance_km: float

    @property
    def duration_s(self) -> float:
        return self.end_ts - self.start_ts


def _observation_cost(fix: Fix, state: State, speed_threshold_kmh: float) -> float:
    """How poorly this fix fits the proposed state.

    Soft hinge rather than a hard test, so that a single noisy sample cannot flip the label on
    its own — it just contributes a little cost, which the transition penalty then outweighs.
    """
    if state == "STOP":
        return max(0.0, fix.speed_kmh - speed_threshold_kmh)
    return max(0.0, speed_threshold_kmh - fix.speed_kmh)


def segment_trips(
    fixes: Sequence[Fix],
    *,
    speed_threshold_kmh: float = 3.0,
    switch_penalty: float = 25.0,
    min_stop_seconds: float = 120.0,
) -> list[Segment]:
    """Label every fix STOP or MOVE, then collapse runs into segments.

    Viterbi over a 2-state chain. `switch_penalty` is the cost of changing state, which is what
    buys hysteresis: flipping is only worth it when the observations justify it persistently.
    Raising it merges more aggressively; lowering it fragments.

    After labelling, stops shorter than `min_stop_seconds` are absorbed into the surrounding
    motion — a 20-second traffic-light pause is not a delivery stop.

    Time O(n * 2 * 2) = O(n). Space O(n) for backpointers.
    """
    if not fixes:
        return []
    if len(fixes) == 1:
        state: State = "STOP" if fixes[0].speed_kmh <= speed_threshold_kmh else "MOVE"
        return [Segment(state, 0, 0, fixes[0].ts, fixes[0].ts, 0.0)]

    states: tuple[State, State] = ("STOP", "MOVE")
    n = len(fixes)

    cost = [[math.inf] * 2 for _ in range(n)]
    back = [[-1] * 2 for _ in range(n)]
    for s in range(2):
        cost[0][s] = _observation_cost(fixes[0], states[s], speed_threshold_kmh)

    for i in range(1, n):
        for s in range(2):
            obs = _observation_cost(fixes[i], states[s], speed_threshold_kmh)
            best_prev, best_cost = -1, math.inf
            for p in range(2):
                c = cost[i - 1][p] + (0.0 if p == s else switch_penalty)
                if c < best_cost:
                    best_prev, best_cost = p, c
            cost[i][s] = best_cost + obs
            back[i][s] = best_prev

    # Backtrack.
    labels: list[State] = [states[0]] * n
    s = 0 if cost[n - 1][0] <= cost[n - 1][1] else 1
    for i in range(n - 1, -1, -1):
        labels[i] = states[s]
        s = back[i][s] if back[i][s] >= 0 else s

    segments = _collapse(fixes, labels)
    return _absorb_short_stops(fixes, segments, min_stop_seconds)


def _collapse(fixes: Sequence[Fix], labels: Sequence[State]) -> list[Segment]:
    segments: list[Segment] = []
    start = 0
    for i in range(1, len(fixes) + 1):
        if i == len(fixes) or labels[i] != labels[start]:
            segments.append(
                Segment(
                    state=labels[start],
                    start_index=start,
                    end_index=i - 1,
                    start_ts=fixes[start].ts,
                    end_ts=fixes[i - 1].ts,
                    distance_km=_path_distance(fixes, start, i - 1),
                )
            )
            start = i
    return segments


def _path_distance(fixes: Sequence[Fix], lo: int, hi: int) -> float:
    return sum(
        haversine_km(fixes[i].lat, fixes[i].lon, fixes[i + 1].lat, fixes[i + 1].lon)
        for i in range(lo, hi)
    )


def _absorb_short_stops(
    fixes: Sequence[Fix], segments: list[Segment], min_stop_seconds: float
) -> list[Segment]:
    """Merge sub-threshold stops into neighbouring motion, then merge adjacent like states."""
    if not segments:
        return []
    kept: list[Segment] = []
    for seg in segments:
        is_interior = seg is not segments[0] and seg is not segments[-1]
        if seg.state == "STOP" and is_interior and seg.duration_s < min_stop_seconds:
            continue  # dropped; the surrounding MOVEs will merge over it
        kept.append(seg)

    merged: list[Segment] = []
    for seg in kept:
        if merged and merged[-1].state == seg.state:
            prev = merged[-1]
            merged[-1] = Segment(
                state=prev.state,
                start_index=prev.start_index,
                end_index=seg.end_index,
                start_ts=prev.start_ts,
                end_ts=seg.end_ts,
                distance_km=_path_distance(fixes, prev.start_index, seg.end_index),
            )
        else:
            merged.append(seg)
    return merged


def idle_seconds(segments: Sequence[Segment], *, engine_on_only: bool = True) -> float:
    """Total stopped time. This feeds the idling cost driver directly.

    `engine_on_only` is a placeholder for the ignition signal: a parked vehicle overnight is not
    'idling' in the cost sense, and counting it would inflate the savings claim enormously. The
    caller passes only within-shift segments. The flag exists to make that requirement explicit
    rather than implicit.
    """
    return sum(s.duration_s for s in segments if s.state == "STOP")
