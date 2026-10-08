"""A9 VEWS — Vehicle Early Warning Score.

Ported from NEWS2, the clinical early-warning score used at hospital bedsides: each vital gets
0-3 points from a published band table, the points are summed, and the total drives escalation.

Three properties matter more here than predictive power, and they are why this is the decision
path rather than a model:

  1. **It is explainable to a specific sentence.** Priya can look at a card and disagree with
     "aux battery resting voltage 11.4 V scored 3". She cannot disagree with "0.87".
  2. **It is auditable.** The band table is versioned data; an alert records which version scored
     it, so a decision made six months ago can be reconstructed exactly.
  3. **It cannot learn our simulator.** A model trained on synthetic faults reproduces the
     generator. A published band table cannot, because no fitting ever happens.

The ML re-ranker (separate module) may reorder what this surfaces. It may never surface anything
on its own. That asymmetry is the whole honesty argument, so it is enforced here by having this
module own the OPEN/SUPPRESS decision outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]

BAND_TABLE_VERSION = "vews-1.0.0"


@dataclass(frozen=True, slots=True)
class Band:
    """One scoring band: points awarded when `low <= value < high`."""

    low: float
    high: float
    points: int


@dataclass(frozen=True, slots=True)
class SignalSpec:
    name: str
    bands: tuple[Band, ...]
    # A signal that on its own means "stop the vehicle now", regardless of total score.
    critical_at_points: int | None = 3
    unit: str = ""


def _score_signal(spec: SignalSpec, value: float) -> int:
    for band in spec.bands:
        if band.low <= value < band.high:
            return band.points
    return 0


# The published table. Nine signals, 0-3 points each, so the total runs 0-27.
# Thresholds are stated here once and referenced by the UI, so the card and the score can never
# disagree about why something fired.
SIGNALS: tuple[SignalSpec, ...] = (
    SignalSpec(
        "aux_battery_v",
        (Band(-1e9, 11.6, 3), Band(11.6, 12.0, 2), Band(12.0, 12.4, 1), Band(12.4, 1e9, 0)),
        unit="V",
    ),
    SignalSpec(
        "coolant_temp_c",
        (Band(-1e9, 70.0, 1), Band(70.0, 100.0, 0), Band(100.0, 110.0, 2), Band(110.0, 1e9, 3)),
        unit="C",
    ),
    SignalSpec(
        "soh_pct",
        (Band(-1e9, 70.0, 3), Band(70.0, 80.0, 2), Band(80.0, 90.0, 1), Band(90.0, 1e9, 0)),
        critical_at_points=None,  # degradation is never a "stop now" on its own
        unit="%",
    ),
    SignalSpec(
        "pack_temp_c",
        (Band(-1e9, 0.0, 1), Band(0.0, 45.0, 0), Band(45.0, 55.0, 2), Band(55.0, 1e9, 3)),
        unit="C",
    ),
    SignalSpec(
        "soc_vs_shift_need_pct",  # SoC headroom against today's planned route
        (Band(-1e9, 0.0, 3), Band(0.0, 10.0, 2), Band(10.0, 25.0, 1), Band(25.0, 1e9, 0)),
        unit="%",
    ),
    SignalSpec(
        "tyre_pressure_drift_pct",
        (Band(-1e9, 5.0, 0), Band(5.0, 10.0, 1), Band(10.0, 20.0, 2), Band(20.0, 1e9, 3)),
        unit="%",
    ),
    SignalSpec(
        "dtc_max_severity",  # 0 none, 1 medium, 2 high, 3 critical
        (Band(-1e9, 1.0, 0), Band(1.0, 2.0, 1), Band(2.0, 3.0, 2), Band(3.0, 1e9, 3)),
    ),
    SignalSpec(
        "oil_life_pct",
        (Band(-1e9, 5.0, 3), Band(5.0, 15.0, 2), Band(15.0, 30.0, 1), Band(30.0, 1e9, 0)),
        critical_at_points=None,
        unit="%",
    ),
    SignalSpec(
        "brake_wear_pct",
        (Band(-1e9, 60.0, 0), Band(60.0, 80.0, 1), Band(80.0, 92.0, 2), Band(92.0, 1e9, 3)),
        unit="%",
    ),
)

_SIGNAL_BY_NAME: Mapping[str, SignalSpec] = {s.name: s for s in SIGNALS}


@dataclass(frozen=True, slots=True)
class SignalScore:
    name: str
    value: float
    points: int
    unit: str


@dataclass(frozen=True, slots=True)
class VewsResult:
    total: int
    severity: Severity
    scores: tuple[SignalScore, ...]
    triggered_by: tuple[str, ...]      # signals that scored >= 2; the WHY slot on the card
    band_table_version: str = BAND_TABLE_VERSION

    @property
    def should_raise(self) -> bool:
        """LOW never reaches the Morning Board — it is analytics-only."""
        return self.severity in ("CRITICAL", "HIGH", "MEDIUM")


def score(observations: Mapping[str, float]) -> VewsResult:
    """Score one vehicle's current signal state.

    Missing signals score 0 rather than raising: an ICE vehicle has no `soh_pct`, and an OEM that
    does not expose tyre pressure must not make the whole vehicle unscoreable. The consequence —
    that a vehicle with sparse telemetry scores artificially low — is real, and is why the UI
    shows a per-vehicle signal-coverage badge rather than pretending the score is comparable
    across OEMs.

    Time O(number of signals) = O(1). Space O(1).
    """
    scores: list[SignalScore] = []
    total = 0
    any_critical = False

    for name, value in observations.items():
        spec = _SIGNAL_BY_NAME.get(name)
        if spec is None:
            continue
        points = _score_signal(spec, value)
        total += points
        scores.append(SignalScore(name, value, points, spec.unit))
        if spec.critical_at_points is not None and points >= spec.critical_at_points:
            any_critical = True

    severity = _severity_for(total, any_critical)
    triggered = tuple(s.name for s in sorted(scores, key=lambda x: -x.points) if s.points >= 2)
    return VewsResult(
        total=total,
        severity=severity,
        scores=tuple(sorted(scores, key=lambda s: (-s.points, s.name))),
        triggered_by=triggered,
    )


def _severity_for(total: int, any_single_critical: bool) -> Severity:
    """Band thresholds.

    `any_single_critical` exists for the NEWS2 reason: a single catastrophic vital outranks a
    mediocre total. A vehicle with a 3-point pack temperature is a fire risk even if every other
    signal is clean and the total is only 3.
    """
    if any_single_critical or total >= 9:
        return "CRITICAL"
    if total >= 5:
        return "HIGH"
    if total >= 2:
        return "MEDIUM"
    return "LOW"


# ----------------------------------------------------------------- the attention budget

MAX_BOARD_ITEMS = 12

_SEVERITY_RANK: Mapping[str, int] = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


@dataclass(frozen=True, slots=True)
class BoardItem:
    vin: str
    result: VewsResult
    dollars_at_risk: float


@dataclass(frozen=True, slots=True)
class Board:
    items: tuple[BoardItem, ...]
    suppressed_count: int


def build_board(
    candidates: Sequence[BoardItem], *, limit: int = MAX_BOARD_ITEMS
) -> Board:
    """Rank and hard-cap the Morning Board.

    The cap is the product. Returning a 13th item would recreate the 40-emails-a-day portal the
    user already muted, so the overflow is reported as a count the dispatcher can expand, never
    as more cards.

    Ranking is severity first, then dollars at risk, then score. Dollars break ties within a
    severity band rather than across it: a $2,000 MEDIUM must not outrank a CRITICAL, because
    CRITICAL means the vehicle does not move today.

    Time O(n log n). Space O(n).
    """
    eligible = [c for c in candidates if c.result.should_raise]
    ordered = sorted(
        eligible,
        key=lambda c: (
            _SEVERITY_RANK[c.result.severity],
            -c.dollars_at_risk,
            -c.result.total,
            c.vin,
        ),
    )
    return Board(items=tuple(ordered[:limit]), suppressed_count=max(0, len(ordered) - limit))
