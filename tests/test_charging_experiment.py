"""End-to-end test of the charging experiment.

This is the one test that exercises the platform's central claim the whole way through:
generate two arms over many nights, estimate the effect, and check that the statistics say what
they should. It runs entirely in-process, no infrastructure required.

The negative test matters as much as the positive one. If the pre-period is generated with both
arms already diverging, difference-in-differences has no valid baseline and will happily report
an effect that is really a pre-existing group difference.
"""

from __future__ import annotations

import pytest

from readyline.experiment import Observation, assign_arm, difference_in_differences
from readyline_sim.charging import TARIFF_30MIN, simulate


@pytest.fixture(scope="module")
def sessions():
    # Small but realistic: 2 depots x 20 EVs x 20 nights.
    return simulate(nights=20, evs_per_depot=20, depots=2, cap_kw=120.0)


def _observations(rows, depot_of):
    """Collapse sessions into one pre and one post observation per vehicle."""
    agg: dict[tuple[str, str], list[float]] = {}
    half = max(r.started_at for r in rows) - (
        max(r.started_at for r in rows) - min(r.started_at for r in rows)
    ) / 2
    for r in rows:
        period = "post" if r.started_at >= half else "pre"
        key = (r.vin, period)
        cost, energy = agg.get(key, (0.0, 0.0)) if key in agg else (0.0, 0.0)
        agg[key] = (cost + r.cost_inr, energy + max(r.soc_end - r.soc_start, 0.0))

    out = []
    for (vin, period), (cost, energy) in agg.items():
        out.append(
            Observation(
                vin=vin,
                arm=assign_arm(vin, depot_of[vin]),
                period=period,
                cost=cost,
                route_km=energy,
            )
        )
    return out


def test_simulation_produces_the_expected_number_of_sessions(sessions):
    assert len(sessions) == 20 * 2 * 20, len(sessions)


def test_both_arms_are_present(sessions):
    depot_of = {r.vin: r.fleet_id for r in sessions}
    arms = {assign_arm(vin, depot) for vin, depot in depot_of.items()}
    assert arms == {"control", "treatment"}


def test_pre_period_has_no_optimised_sessions(sessions):
    """DiD needs a baseline in which both arms behave identically. Without it the estimate
    confounds the treatment effect with a pre-existing difference between the groups."""
    mid = min(r.started_at for r in sessions) + (
        max(r.started_at for r in sessions) - min(r.started_at for r in sessions)
    ) / 2
    pre = [r for r in sessions if r.started_at < mid]
    assert pre, "no pre-period sessions generated"
    assert all(r.was_optimised == 0 for r in pre)


def test_post_period_optimises_only_the_treatment_arm(sessions):
    mid = min(r.started_at for r in sessions) + (
        max(r.started_at for r in sessions) - min(r.started_at for r in sessions)
    ) / 2
    post = [r for r in sessions if r.started_at >= mid]
    depot_of = {r.vin: r.fleet_id for r in sessions}
    for r in post:
        if assign_arm(r.vin, depot_of[r.vin]) == "control":
            assert r.was_optimised == 0, f"{r.vin} is control but was optimised"


def test_experiment_detects_a_real_and_significant_saving(sessions):
    """The platform's headline claim, end to end."""
    depot_of = {r.vin: r.fleet_id for r in sessions}
    result = difference_in_differences(_observations(sessions, depot_of))

    assert result.n_control > 0, "no control arm; nothing to compare against"
    assert result.n_treatment > result.n_control, "arms look inverted"
    assert result.saving_per_100km > 0, "treatment must be cheaper, not dearer"
    assert result.significant_at_05, f"not significant, p={result.p_value}"
    assert result.ci_high < 0, "95% CI should exclude zero for a real effect"


def test_control_arm_barely_changes_between_periods(sessions):
    """The validation of the whole design: control is unmanaged in BOTH periods, so its change
    must be ~0. A control arm that drifts means something other than the treatment moved, and
    the estimate is not attributable."""
    depot_of = {r.vin: r.fleet_id for r in sessions}
    result = difference_in_differences(_observations(sessions, depot_of))
    assert abs(result.control_change) < abs(result.treatment_change) * 0.1


def test_tariff_has_a_genuine_cheap_window():
    """If the tariff were flat there would be nothing to arbitrage and the saving would be
    an artefact of something else."""
    assert max(TARIFF_30MIN) / min(TARIFF_30MIN) > 2.5


def test_simulation_is_deterministic():
    a = simulate(nights=4, evs_per_depot=5, depots=1, cap_kw=80.0)
    b = simulate(nights=4, evs_per_depot=5, depots=1, cap_kw=80.0)
    assert [r.cost_inr for r in a] == [r.cost_inr for r in b]


def test_no_session_exceeds_one_hundred_percent_soc(sessions):
    for r in sessions:
        assert r.soc_end <= 100.0 + 1e-6, f"{r.vin} charged to {r.soc_end}%"
        assert r.soc_end >= r.soc_start - 1e-6


def test_energy_conservation_holds_on_every_session(sessions):
    """kWh bought = kWh stored + losses, checked against the pack size as an INDEPENDENT third
    quantity.

    An earlier version of this test recovered the pack size from the same kwh and SoC delta it
    was checking, which made it reproduce the efficiency constant by construction -- it passed
    unconditionally and proved nothing. Carrying battery_kwh on the row makes it a real
    conservation check: if the ledger drifts, every dollar figure downstream is wrong and this
    fails immediately.
    """
    checked = 0
    for r in sessions:
        stored_frac = (r.soc_end - r.soc_start) / 100.0
        if stored_frac <= 1e-6 or r.kwh <= 1e-6:
            continue
        stored_kwh = stored_frac * r.battery_kwh
        implied_efficiency = stored_kwh / r.kwh
        assert 0.90 < implied_efficiency < 0.94, (
            f"{r.vin}: {stored_kwh:.3f} kWh stored from {r.kwh:.3f} kWh bought "
            f"implies {implied_efficiency:.4f} efficiency, which is unphysical"
        )
        checked += 1
    assert checked > 0, "no session carried enough energy to check"


def test_cost_equals_energy_times_tariff(sessions):
    """A second conservation identity, on money rather than energy."""
    for r in sessions:
        if r.kwh <= 1e-6:
            continue
        assert r.cost_inr == pytest.approx(r.kwh * r.tariff_inr, rel=1e-3), r.vin
