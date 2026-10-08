"""Tests for the randomised holdout and the difference-in-differences estimator.

The statistics here are the platform's central claim, so they are tested against cases with a
known answer: a null effect must not be declared significant, a planted effect must be recovered,
and the t-distribution implementation is checked against published critical values.
"""

from __future__ import annotations

import random

import pytest

from readyline.experiment import (
    DEFAULT_CONTROL_FRACTION,
    Observation,
    _two_sided_p,
    arm_balance,
    assign_arm,
    difference_in_differences,
)


# --------------------------------------------------------------------- assignment


def test_assignment_is_deterministic():
    """Re-randomising a vehicle mid-experiment is undetectable in the output and biases it."""
    for i in range(200):
        vin = f"VIN{i:05d}"
        assert assign_arm(vin, 7) == assign_arm(vin, 7)


def test_assignment_hits_the_requested_fraction():
    vins = [f"VIN{i:06d}" for i in range(20_000)]
    control = sum(1 for v in vins if assign_arm(v, 3) == "control")
    observed = control / len(vins)
    assert abs(observed - DEFAULT_CONTROL_FRACTION) < 0.01, observed


def test_assignment_is_stratified_so_each_depot_gets_its_own_draw():
    """An unstratified global draw can concentrate the control arm in the cheapest depot and
    report a saving that is pure composition."""
    for depot in range(1, 9):
        vins = [f"D{depot}V{i:05d}" for i in range(4_000)]
        frac = sum(1 for v in vins if assign_arm(v, depot) == "control") / len(vins)
        assert 0.07 < frac < 0.13, f"depot {depot} drew {frac:.3f}"


def test_same_vin_can_differ_across_depots():
    """Depot is part of the hash, which is what makes the draw per-stratum."""
    differing = sum(1 for d in range(200) if assign_arm("SAMEVIN", d) == "control")
    assert 0 < differing < 200


def test_invalid_fraction_is_rejected():
    with pytest.raises(ValueError):
        assign_arm("VIN", 1, control_fraction=1.5)


def test_arm_balance_exposes_an_empty_control_arm():
    assignments = {f"V{i}": (1, "treatment") for i in range(10)}
    assignments["V99"] = (2, "control")
    balance = arm_balance(assignments)
    assert balance[1]["control"] == 0
    assert balance[2]["control"] == 1


# --------------------------------------------------------------------- the estimator


def _obs(vin, arm, period, cost, km=100.0):
    return Observation(vin=vin, arm=arm, period=period, cost=cost, route_km=km)


def _null_trial(seed: int):
    """One experiment in which the treatment does nothing."""
    rng = random.Random(seed)
    obs = []
    for i in range(400):
        arm = "control" if i % 10 == 0 else "treatment"
        base = rng.gauss(20.0, 3.0)
        obs.append(_obs(f"V{i}", arm, "pre", base))
        # Both arms change identically: no treatment effect.
        obs.append(_obs(f"V{i}", arm, "post", base + rng.gauss(0.0, 1.0)))
    return difference_in_differences(obs)


def test_estimator_is_calibrated_under_the_null():
    """The most important test in the suite, and it must be run over many trials.

    Asserting that a *single* null experiment comes out non-significant is a coin flip: by
    construction it fails 5% of the time, and an earlier version of this test did exactly that
    on seed 42. What actually needs to hold is the calibration: across many null experiments the
    false-positive rate is ~5%, the estimate is unbiased, and p-values are uniform.
    """
    trials = [_null_trial(s) for s in range(400)]

    false_positives = sum(1 for r in trials if r.significant_at_05) / len(trials)
    assert 0.02 < false_positives < 0.09, f"miscalibrated: {false_positives:.3f}"

    mean_estimate = sum(r.did_estimate for r in trials) / len(trials)
    assert abs(mean_estimate) < 0.15, f"biased estimator: {mean_estimate:+.4f}"

    # Under the null, p-values are uniform on [0, 1], so their mean is ~0.5.
    mean_p = sum(r.p_value for r in trials) / len(trials)
    assert 0.42 < mean_p < 0.58, f"p-values not uniform: {mean_p:.3f}"


def test_confidence_interval_covers_the_truth_at_the_nominal_rate():
    """Coverage, not just width. Using the normal quantile instead of the t quantile produced
    93.2% coverage against a nominal 95% -- narrow intervals that overstate confidence."""
    trials = [_null_trial(s) for s in range(400)]
    covered = sum(1 for r in trials if r.ci_low < 0 < r.ci_high) / len(trials)
    assert covered > 0.93, f"CI under-covers: {covered:.3f}"


def test_planted_effect_is_recovered_with_the_right_sign_and_magnitude():
    rng = random.Random(7)
    true_effect = -4.0  # treatment becomes $4/100km cheaper
    obs = []
    for i in range(600):
        arm = "control" if i % 10 == 0 else "treatment"
        base = rng.gauss(20.0, 2.0)
        obs.append(_obs(f"V{i}", arm, "pre", base))
        delta = rng.gauss(0.0, 1.0) + (true_effect if arm == "treatment" else 0.0)
        obs.append(_obs(f"V{i}", arm, "post", base + delta))

    result = difference_in_differences(obs)
    assert result.significant_at_05
    assert result.did_estimate == pytest.approx(true_effect, abs=0.6)
    assert result.saving_per_100km > 0, "a cost reduction must report as a positive saving"
    assert result.ci_low < true_effect < result.ci_high


def test_did_removes_a_fixed_level_difference_between_arms():
    """Arms differ in baseline cost (different depots). A naive post-only comparison would
    attribute that gap to the treatment; DiD must not."""
    obs = []
    for i in range(200):
        arm = "control" if i % 10 == 0 else "treatment"
        # Treatment vehicles are inherently more expensive, in BOTH periods.
        base = 30.0 if arm == "treatment" else 10.0
        obs.append(_obs(f"V{i}", arm, "pre", base))
        obs.append(_obs(f"V{i}", arm, "post", base))  # nothing changed

    result = difference_in_differences(obs)
    assert result.did_estimate == pytest.approx(0.0, abs=1e-6), "level difference leaked in"


def test_normalises_by_distance_not_per_vehicle():
    """A control vehicle that drove less must not look cheaper for that reason alone."""
    obs = [
        _obs("T1", "treatment", "pre", 100.0, km=100.0),   # 100/100km
        _obs("T1", "treatment", "post", 100.0, km=100.0),
        _obs("C1", "control", "pre", 50.0, km=50.0),       # also 100/100km
        _obs("C1", "control", "post", 50.0, km=50.0),
    ]
    result = difference_in_differences(obs)
    assert result.treatment_pre == pytest.approx(result.control_pre)
    assert result.did_estimate == pytest.approx(0.0, abs=1e-9)


def test_vehicles_seen_in_only_one_period_are_excluded():
    """Comparing different populations across periods silently biases the estimate."""
    obs = [
        _obs("BOTH", "treatment", "pre", 20.0),
        _obs("BOTH", "treatment", "post", 18.0),
        _obs("ONLYPRE", "treatment", "pre", 99.0),
        _obs("ONLYPOST", "control", "post", 1.0),
    ]
    result = difference_in_differences(obs)
    assert result.n_treatment == 1
    assert result.n_control == 0


def test_zero_distance_does_not_divide_by_zero():
    obs = [
        _obs("V", "treatment", "pre", 10.0, km=0.0),
        _obs("V", "treatment", "post", 10.0, km=0.0),
    ]
    result = difference_in_differences(obs)
    assert result.treatment_pre == 0.0


def test_empty_input_is_handled():
    result = difference_in_differences([])
    assert result.n_treatment == 0 and result.n_control == 0
    assert result.p_value == 1.0


# --------------------------------------------------------------------- the t-distribution


@pytest.mark.parametrize(
    "t,dof,expected",
    [
        (2.228, 10, 0.05),    # published two-sided critical value, 10 dof
        (2.086, 20, 0.05),
        (1.960, 100_000, 0.05),  # converges to the normal
        (0.0, 10, 1.0),
    ],
)
def test_p_value_matches_published_critical_values(t, dof, expected):
    """Our own incomplete-beta implementation, checked against a statistics table."""
    assert _two_sided_p(t, dof) == pytest.approx(expected, abs=0.002)


def test_p_value_is_symmetric_in_sign():
    assert _two_sided_p(2.5, 30) == pytest.approx(_two_sided_p(-2.5, 30))


def test_larger_t_gives_smaller_p():
    assert _two_sided_p(3.0, 30) < _two_sided_p(1.0, 30)
