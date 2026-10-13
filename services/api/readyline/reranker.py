"""The ML re-ranker, and the honest way to evaluate it.

**It cannot surface an alert.** It receives the set the VEWS rules already opened and reorders it
(ADR-0003). That is not a limitation we are apologising for -- it is the structural reason a model
trained on our own simulator cannot manufacture a false alert.

What it is allowed to improve is *ordering within the cap*. The Morning Board shows 12 cards; if
the three vehicles that will actually fail tonight sit at positions 14, 19 and 30 in rule order,
the dispatcher never sees them. Ranking is where the remaining value is, and it is measurable
without making any claim the data cannot support.

The evaluation is deliberately narrow:

* The baseline is **the rules themselves** -- VEWS score then dollars at risk -- not a random
  shuffle. Beating a random baseline would be meaningless.
* The metric is **precision@12**, the thing the product actually constrains, not AUC. A model with
  a beautiful AUC that puts the right vehicles at rank 13 is worthless here.
* Training and evaluation use **disjoint vehicles**, not a random row split. A random split leaks:
  the same vehicle appears in both halves and the model memorises its degradation curve rather
  than learning the pattern.
* We report the delta against the baseline, and if it is negative we say so and ship the rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

FEATURES = (
    "vews_total",
    "aux_battery_v",
    "coolant_temp_c",
    "soh_pct",
    "pack_temp_c",
    "soc_vs_shift_need_pct",
    "tyre_pressure_drift_pct",
    "dtc_max_severity",
    "oil_life_pct",
    "brake_wear_pct",
    "dollars_at_risk",
    "days_since_last_service",
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One alert the rules have already opened."""

    vin: str
    features: dict[str, float]
    vews_total: int
    dollars_at_risk: float
    # Ground truth from the simulator ledger. Only ever used for training and back-testing;
    # production never sees it: a ledger read from a product path makes every metric circular.
    failed_within_72h: bool | None = None

    def vector(self) -> list[float]:
        return [float(self.features.get(f, 0.0)) for f in FEATURES]


def baseline_order(candidates: Sequence[Candidate]) -> list[Candidate]:
    """The rules' own ordering: VEWS first, then money. This is what must be beaten."""
    return sorted(candidates, key=lambda c: (-c.vews_total, -c.dollars_at_risk, c.vin))


def precision_at_k(ordered: Sequence[Candidate], k: int = 12) -> float:
    """Fraction of the top k that really did fail. The product's actual constraint."""
    top = ordered[:k]
    labelled = [c for c in top if c.failed_within_72h is not None]
    if not labelled:
        return 0.0
    return sum(1 for c in labelled if c.failed_within_72h) / len(labelled)


class Reranker:
    """Gradient-boosted re-ranker over rule-surfaced candidates.

    scikit-learn rather than PyTorch: the feature set is twelve engineered signals and a few
    thousand rows. A neural network here would be slower to train, harder to explain to a
    dispatcher, and no more accurate.
    """

    def __init__(self) -> None:
        self._model = None

    @property
    def fitted(self) -> bool:
        return self._model is not None

    def fit(self, candidates: Sequence[Candidate], *, seed: int = 0) -> "Reranker":
        from sklearn.ensemble import GradientBoostingClassifier

        labelled = [c for c in candidates if c.failed_within_72h is not None]
        if len(labelled) < 40:
            raise ValueError(f"not enough labelled candidates to fit: {len(labelled)}")
        y = [int(c.failed_within_72h) for c in labelled]
        if len(set(y)) < 2:
            raise ValueError("labels are all one class; nothing to learn")

        self._model = GradientBoostingClassifier(
            n_estimators=120, max_depth=3, learning_rate=0.08, random_state=seed
        )
        self._model.fit([c.vector() for c in labelled], y)
        return self

    def score(self, candidate: Candidate) -> float:
        if self._model is None:
            raise RuntimeError("re-ranker is not fitted")
        return float(self._model.predict_proba([candidate.vector()])[0][1])

    def rerank(self, candidates: Sequence[Candidate]) -> list[Candidate]:
        """Reorder, never filter.

        The returned list is a permutation of the input -- same length, same members. A test
        asserts that, because silently dropping a candidate would turn the re-ranker into a
        gate, which ADR-0003 forbids.
        """
        if self._model is None:
            return baseline_order(candidates)
        return sorted(
            candidates,
            key=lambda c: (-self.score(c), -c.vews_total, c.vin),
        )


@dataclass(frozen=True, slots=True)
class Evaluation:
    baseline_precision: float
    model_precision: float
    n_train: int
    n_test: int
    k: int

    @property
    def delta(self) -> float:
        return round(self.model_precision - self.baseline_precision, 4)

    @property
    def verdict(self) -> str:
        if self.delta > 0.02:
            return (
                f"Re-ranker improves precision@{self.k} from {self.baseline_precision:.3f} to "
                f"{self.model_precision:.3f} (+{self.delta:.3f}). Shipped as a re-ranker only."
            )
        if self.delta < -0.02:
            return (
                f"Re-ranker is WORSE than the rules ({self.model_precision:.3f} vs "
                f"{self.baseline_precision:.3f}). Rules ship; the model is disabled."
            )
        return (
            f"No material difference ({self.model_precision:.3f} vs "
            f"{self.baseline_precision:.3f}). Rules ship, because they are explainable."
        )


def evaluate(
    candidates: Sequence[Candidate], *, k: int = 12, train_fraction: float = 0.7, seed: int = 0
) -> Evaluation:
    """Train and evaluate on DISJOINT VEHICLES.

    Splitting rows at random would put the same vehicle in both halves, and the model would score
    well by memorising that vehicle's trajectory rather than learning anything transferable. That
    is the single easiest way to produce an impressive and worthless number here.
    """
    import random

    labelled = [c for c in candidates if c.failed_within_72h is not None]
    vins = sorted({c.vin for c in labelled})
    rng = random.Random(seed)
    rng.shuffle(vins)
    cut = int(len(vins) * train_fraction)
    train_vins = set(vins[:cut])

    train = [c for c in labelled if c.vin in train_vins]
    test = [c for c in labelled if c.vin not in train_vins]
    if not train or not test:
        raise ValueError("split produced an empty side")

    model = Reranker().fit(train, seed=seed)
    return Evaluation(
        baseline_precision=round(precision_at_k(baseline_order(test), k), 4),
        model_precision=round(precision_at_k(model.rerank(test), k), 4),
        n_train=len(train),
        n_test=len(test),
        k=k,
    )


@dataclass(frozen=True, slots=True)
class RepeatedEvaluation:
    """Evaluation across several vehicle splits.

    precision@12 moves in steps of 1/12, so one split swings by +/-0.17 on noise alone. Deciding
    whether to ship a model on a single split is how teams convince themselves a coin flip is an
    improvement. The ship decision is made on the mean across seeds, and the spread is reported
    beside it so a reader can see how noisy the estimate is.
    """

    runs: tuple[Evaluation, ...]

    @property
    def mean_baseline(self) -> float:
        return round(sum(r.baseline_precision for r in self.runs) / len(self.runs), 4)

    @property
    def mean_model(self) -> float:
        return round(sum(r.model_precision for r in self.runs) / len(self.runs), 4)

    @property
    def mean_delta(self) -> float:
        return round(self.mean_model - self.mean_baseline, 4)

    @property
    def worst_delta(self) -> float:
        return round(min(r.delta for r in self.runs), 4)

    @property
    def wins(self) -> int:
        return sum(1 for r in self.runs if r.delta > 0)

    @property
    def ship_model(self) -> bool:
        """Ship only on a mean gain that clears one step of the metric's own granularity."""
        return self.mean_delta > 1.0 / self.runs[0].k

    @property
    def verdict(self) -> str:
        head = (
            f"precision@{self.runs[0].k} over {len(self.runs)} vehicle splits: "
            f"rules {self.mean_baseline:.3f}, re-ranker {self.mean_model:.3f} "
            f"(mean delta {self.mean_delta:+.3f}, worst {self.worst_delta:+.3f}, "
            f"{self.wins}/{len(self.runs)} splits improved)."
        )
        if self.ship_model:
            return head + " Re-ranker enabled, as a re-ranker only."
        return (
            head
            + " The gain does not clear the metric's own granularity, so it is within noise."
            + " The rules ship and the model is DISABLED by default: an unexplainable"
            + " reordering is not worth a difference we cannot demonstrate."
        )


def evaluate_repeated(
    candidates: Sequence[Candidate], *, k: int = 12, seeds: Sequence[int] = (1, 2, 3, 4, 5, 6, 7, 8)
) -> RepeatedEvaluation:
    return RepeatedEvaluation(tuple(evaluate(candidates, k=k, seed=s) for s in seeds))

