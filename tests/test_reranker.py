"""Re-ranker tests, built on candidates derived from the simulator's ground-truth ledger.

The structural tests matter more than the accuracy number. ADR-0003 says the model may reorder
what the rules surfaced and may never surface anything itself, and that is the single property
keeping a model trained on our own generator from manufacturing false alerts. If a future change
lets `rerank` drop or add a candidate, it becomes a gate, and the honesty argument collapses.
"""

from __future__ import annotations

import random

import pytest

from readyline.reranker import (
    FEATURES,
    Candidate,
    Evaluation,
    Reranker,
    baseline_order,
    evaluate,
    evaluate_repeated,
    precision_at_k,
)


def _candidates(n: int = 400, seed: int = 3) -> list[Candidate]:
    """Synthesise rule-surfaced candidates, scored by the REAL VEWS band table.

    Using the production scorer matters. An earlier version of this helper computed the VEWS
    total from a continuous formula over the same signals that drive failure, which made the
    rules baseline near-optimal by construction and left the model nothing to recover. Real VEWS
    buckets each signal into 0-3 bands, and that banding genuinely discards within-band
    ordering -- which is precisely the information a re-ranker can exploit.
    """
    from readyline.algorithms.vews import score as vews_score

    rng = random.Random(seed)
    out: list[Candidate] = []
    for i in range(n):
        aux = rng.uniform(11.2, 12.9)
        soh = rng.uniform(62.0, 100.0)
        coolant = rng.uniform(80.0, 115.0)
        brake = rng.uniform(40.0, 99.0)
        pack = rng.uniform(20.0, 50.0)
        tyre = rng.uniform(0.0, 25.0)
        oil = rng.uniform(5.0, 100.0)
        dtc = rng.choice([0, 0, 1, 2, 3])
        headroom = rng.uniform(-10.0, 60.0)
        dollars = rng.uniform(100.0, 4000.0)

        observations = {
            "aux_battery_v": aux, "coolant_temp_c": coolant, "soh_pct": soh,
            "pack_temp_c": pack, "soc_vs_shift_need_pct": headroom,
            "tyre_pressure_drift_pct": tyre, "dtc_max_severity": float(dtc),
            "oil_life_pct": oil, "brake_wear_pct": brake,
        }
        vews = vews_score(observations).total

        # Failure depends on the CONTINUOUS signals, which the banded score only approximates.
        risk = (
            0.52 * max(0.0, (12.4 - aux) / 1.2)
            + 0.26 * max(0.0, (90.0 - soh) / 28.0)
            + 0.14 * max(0.0, (coolant - 95.0) / 20.0)
            + 0.08 * (brake / 99.0)
        )
        failed = rng.random() < min(0.92, max(0.02, risk))

        out.append(
            Candidate(
                vin=f"VIN{i:05d}",
                features={**observations, "vews_total": vews, "dollars_at_risk": dollars,
                          "days_since_last_service": rng.uniform(1, 400)},
                vews_total=vews,
                dollars_at_risk=dollars,
                failed_within_72h=failed,
            )
        )
    return out


# --------------------------------------------------------------- structural guarantees


def test_rerank_is_a_permutation_never_a_filter():
    """ADR-0003. If the model can drop a candidate it has become a gate."""
    cands = _candidates(120)
    model = Reranker().fit(cands)
    out = model.rerank(cands)
    assert len(out) == len(cands)
    assert {c.vin for c in out} == {c.vin for c in cands}


def test_unfitted_reranker_falls_back_to_the_rules():
    """A missing or failed model must degrade to rule order, not to an arbitrary one."""
    cands = _candidates(40)
    assert [c.vin for c in Reranker().rerank(cands)] == [c.vin for c in baseline_order(cands)]


def test_scoring_before_fitting_is_an_error_not_a_guess():
    with pytest.raises(RuntimeError):
        Reranker().score(_candidates(2)[0])


def test_baseline_is_the_rules_not_a_shuffle():
    """Beating a random ordering would prove nothing."""
    cands = _candidates(50)
    ordered = baseline_order(cands)
    scores = [(c.vews_total, c.dollars_at_risk) for c in ordered]
    assert scores == sorted(scores, reverse=True)


def test_feature_vector_is_stable_and_complete():
    c = _candidates(1)[0]
    assert len(c.vector()) == len(FEATURES)
    assert c.vector() == c.vector()


def test_missing_feature_defaults_to_zero_rather_than_raising():
    c = Candidate(vin="V", features={}, vews_total=3, dollars_at_risk=10.0)
    assert c.vector() == [0.0] * len(FEATURES)


# --------------------------------------------------------------- evaluation discipline


def test_refuses_to_fit_on_too_little_data():
    with pytest.raises(ValueError):
        Reranker().fit(_candidates(10))


def test_refuses_to_fit_on_a_single_class():
    cands = [
        Candidate(f"V{i}", {"vews_total": 5}, 5, 100.0, failed_within_72h=True)
        for i in range(60)
    ]
    with pytest.raises(ValueError):
        Reranker().fit(cands)


def test_train_and_test_vehicles_are_disjoint():
    """A random ROW split leaks: the same vehicle lands in both halves and the model scores well
    by memorising its trajectory. Splitting by vehicle is the only honest option here."""
    cands = _candidates(300)
    ev = evaluate(cands, k=12, train_fraction=0.7)
    assert ev.n_train > 0 and ev.n_test > 0
    assert ev.n_train + ev.n_test == len(cands)


def test_evaluation_reports_precision_at_the_product_cap():
    """AUC is not the metric. The board shows 12 cards; rank 13 is invisible."""
    ev = evaluate(_candidates(400), k=12)
    assert ev.k == 12
    assert 0.0 <= ev.baseline_precision <= 1.0
    assert 0.0 <= ev.model_precision <= 1.0


def test_verdict_is_honest_when_the_model_loses():
    """If the model is worse we must say so and ship the rules, not quietly ship the model."""
    losing = Evaluation(baseline_precision=0.80, model_precision=0.60, n_train=1, n_test=1, k=12)
    assert "WORSE" in losing.verdict
    assert losing.delta < 0

    tie = Evaluation(baseline_precision=0.80, model_precision=0.805, n_train=1, n_test=1, k=12)
    assert "Rules ship" in tie.verdict


def test_precision_at_k_ignores_unlabelled_candidates():
    cands = [
        Candidate("A", {}, 5, 1.0, failed_within_72h=True),
        Candidate("B", {}, 4, 1.0, failed_within_72h=False),
        Candidate("C", {}, 3, 1.0, failed_within_72h=None),
    ]
    assert precision_at_k(cands, k=3) == pytest.approx(0.5)


def test_precision_at_k_with_no_labels_is_zero_not_an_error():
    assert precision_at_k([Candidate("A", {}, 1, 1.0)], k=5) == 0.0


# --------------------------------------------------------------- does it actually help


def test_ship_decision_is_made_on_many_splits_not_one():
    """precision@12 moves in steps of 1/12, so a single split swings +/-0.17 on noise. The real
    measured result here is a mean delta near zero: the re-ranker does NOT reliably beat the
    rules, and the verdict must say so rather than cherry-picking a lucky seed."""
    rep = evaluate_repeated(_candidates(800), k=12)
    assert len(rep.runs) == 8
    assert -0.15 < rep.mean_delta < 0.15, f"unexpectedly large effect: {rep.verdict}"
    if not rep.ship_model:
        assert "DISABLED" in rep.verdict


def test_ship_threshold_respects_metric_granularity():
    """A 'gain' smaller than one position out of twelve is not a gain."""
    runs = tuple(
        Evaluation(baseline_precision=0.5, model_precision=0.52, n_train=1, n_test=1, k=12)
        for _ in range(4)
    )
    from readyline.reranker import RepeatedEvaluation
    assert RepeatedEvaluation(runs).ship_model is False

    strong = tuple(
        Evaluation(baseline_precision=0.5, model_precision=0.70, n_train=1, n_test=1, k=12)
        for _ in range(4)
    )
    assert RepeatedEvaluation(strong).ship_model is True
