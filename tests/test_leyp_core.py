"""Physics engine tests: condition initialization, hazard, degradation, breaks."""

import math

import numpy as np
import pytest

from leyp_config import (
    ALPHA,
    COEFF_DIAMETER,
    LEYP_BREAK_FEEDBACK_CAP,
    MATERIAL_PROPS,
    SEGMENT_BREAK_THRESHOLD,
    STANDARD_LIFE,
)
from leyp_core import Pipe, VirtualSegment


# --- Condition initialization -------------------------------------------------

def test_initial_condition_within_scale(pipe_attrs):
    """Condition must land on the documented 1-6 rating scale."""
    for age in (0, 25, 50, 75, 100, 150):
        p = Pipe({**pipe_attrs, "Age": age})
        assert 1.0 <= p.current_condition <= 6.0


def test_initial_condition_decreases_with_age(pipe_attrs):
    """An older pipe of the same material must not start in better condition."""
    conditions = [
        Pipe({**pipe_attrs, "Age": age}).current_condition for age in (10, 40, 70, 100)
    ]
    assert conditions == sorted(conditions, reverse=True)


def test_condition_never_reaches_failure_floor_from_age_alone(pipe_attrs):
    """The exponential init curve is asymptotic: age alone cannot condemn a pipe.

    Documents why leyp_runner's `initially_dead` set is always empty.
    """
    p = Pipe({**pipe_attrs, "Age": 400, "Condition": 1})
    assert p.current_condition > 1.001


def test_csv_condition_modifier_raises_condition(pipe_attrs):
    """The CSV Condition column blends the age estimate upward."""
    low = Pipe({**pipe_attrs, "Condition": 1}).current_condition
    high = Pipe({**pipe_attrs, "Condition": 2}).current_condition
    assert high > low


@pytest.mark.parametrize("material", sorted(STANDARD_LIFE))
def test_all_materials_initialize(pipe_attrs, material):
    """Every configured material must produce a usable pipe."""
    p = Pipe({**pipe_attrs, "Material": material})
    assert 1.0 <= p.current_condition <= 6.0
    assert p.total_ttf_years >= 10
    assert p.degradation_rate > 0


# --- Hazard -------------------------------------------------------------------

def test_hazard_is_positive(pipe):
    assert pipe.calculate_hazard(0) > 0


def test_hazard_rises_with_age(pipe):
    """Weibull beta > 1 for DI, so hazard must increase over the horizon."""
    assert pipe.calculate_hazard(50) > pipe.calculate_hazard(0)


def test_leyp_feedback_scales_hazard(pipe):
    """Each prior break multiplies hazard by (1 + ALPHA * n_breaks)."""
    pipe.n_breaks = 0
    base = pipe.calculate_hazard(10)
    pipe.n_breaks = 4
    assert pipe.calculate_hazard(10) == pytest.approx(base * (1 + ALPHA * 4))


def test_larger_diameter_lowers_hazard(pipe_attrs):
    """COEFF_DIAMETER is negative: bigger mains break less often."""
    small = Pipe({**pipe_attrs, "Diameter": 6.0})
    large = Pipe({**pipe_attrs, "Diameter": 12.0})
    small.n_breaks = large.n_breaks = 0
    assert large.calculate_hazard(10) < small.calculate_hazard(10)


def test_material_multiplier_applied(pipe_attrs):
    """AC has a higher base multiplier than PVC, so it must be more hazardous."""
    ac = Pipe({**pipe_attrs, "Material": "AC"})
    pvc = Pipe({**pipe_attrs, "Material": "PVC"})
    ac.n_breaks = pvc.n_breaks = 0
    assert MATERIAL_PROPS["AC"]["base_mult"] > MATERIAL_PROPS["PVC"]["base_mult"]
    assert ac.calculate_hazard(10) > pvc.calculate_hazard(10)


# --- Degradation --------------------------------------------------------------

def test_degrade_reduces_condition(pipe):
    before = pipe.current_condition
    pipe.degrade()
    assert pipe.current_condition < before


def test_degrade_respects_scale_floor(pipe):
    for _ in range(500):
        pipe.degrade()
    assert pipe.current_condition >= 1.0


def test_degradation_rate_reaches_floor_at_ttf(pipe):
    """degradation_rate is defined so condition 6 decays to 1 at the sampled TTF."""
    assert pipe.degradation_rate == pytest.approx(math.log(6.0) / pipe.total_ttf_years)


def test_current_ttf_positive(pipe):
    assert pipe.current_ttf > 0


# --- Virtual segments and breaks ----------------------------------------------

def test_segment_failure_threshold():
    """A segment fails at SEGMENT_BREAK_THRESHOLD breaks, not before."""
    seg = VirtualSegment(100.0)
    seg.n_point_breaks = SEGMENT_BREAK_THRESHOLD - 1
    assert not seg.has_failed(SEGMENT_BREAK_THRESHOLD)
    seg.n_point_breaks = SEGMENT_BREAK_THRESHOLD
    assert seg.has_failed(SEGMENT_BREAK_THRESHOLD)


def test_single_segment_at_threshold_fails_the_pipe(pipe):
    """Confirmed rule: any ONE segment reaching the threshold condemns the pipe.

    This is the intended behaviour, not the '3+ segments' wording carried over
    from a prior project.
    """
    pipe.segments[0].n_point_breaks = SEGMENT_BREAK_THRESHOLD
    result = pipe.simulate_year(1)
    assert result["failed"] is True
    assert pipe.current_condition == 1.0


def test_pipe_survives_breaks_spread_below_threshold(pipe):
    """Breaks spread thinly across segments must not trip the failure rule."""
    for seg in pipe.segments:
        seg.n_point_breaks = SEGMENT_BREAK_THRESHOLD - 1
    pipe.degradation_rate = 1e-9  # isolate the failure rule from decay
    assert any(s.n_point_breaks for s in pipe.segments)
    assert not any(s.has_failed(SEGMENT_BREAK_THRESHOLD) for s in pipe.segments)


def test_update_leyp_state_sums_segment_breaks(pipe):
    for i, seg in enumerate(pipe.segments):
        seg.n_point_breaks = i
    pipe.update_leyp_state()
    assert pipe.n_breaks == sum(range(len(pipe.segments)))


def test_reset_breaks_clears_history(pipe):
    for seg in pipe.segments:
        seg.n_point_breaks = 2
    pipe.reset_breaks()
    assert pipe.n_breaks == 0


def test_simulate_year_repair_cost_matches_break_count(pipe):
    from leyp_config import EMERGENCY_REPAIR_COST_PER_BREAK

    result = pipe.simulate_year(1)
    assert result["repair_cost"] == result["breaks"] * EMERGENCY_REPAIR_COST_PER_BREAK


def test_zero_intensity_produces_no_breaks():
    seg = VirtualSegment(300.0)
    assert seg.simulate_breaks(0.0) == 0
    assert seg.n_point_breaks == 0


# --- Wear-out: the age-related arm of the bathtub curve -------------------------

import leyp_core as _lc
from leyp_config import (
    WEAROUT_MAX_MULTIPLIER,
    WEAROUT_ONSET_LIFE_RATIO,
    WEAROUT_STEEPNESS,
)


def _pipe_at_life_ratio(pipe_attrs, material, ratio):
    life = STANDARD_LIFE.get(material, STANDARD_LIFE["Default"])["base_life"]
    return Pipe({**pipe_attrs, "Material": material, "Age": ratio * life})


@pytest.mark.parametrize("material", sorted(STANDARD_LIFE))
def test_wearout_is_unity_before_onset(pipe_attrs, material):
    for ratio in (0.0, 0.25, 0.5, 0.9, WEAROUT_ONSET_LIFE_RATIO):
        p = _pipe_at_life_ratio(pipe_attrs, material, ratio)
        assert p.wearout_multiplier(0) == 1.0


@pytest.mark.parametrize("material", sorted(STANDARD_LIFE))
def test_wearout_is_monotone_in_age(pipe_attrs, material):
    p = _pipe_at_life_ratio(pipe_attrs, material, 0.0)
    values = [p.wearout_multiplier(y) for y in range(0, 400)]
    assert all(b >= a for a, b in zip(values, values[1:]))


def test_wearout_is_bounded(pipe_attrs):
    p = _pipe_at_life_ratio(pipe_attrs, "GI", 0.0)
    for y in range(0, 2000, 25):
        assert p.wearout_multiplier(y) <= WEAROUT_MAX_MULTIPLIER + 1e-12
    far = _pipe_at_life_ratio(pipe_attrs, "GI", 5.0)
    assert far.wearout_multiplier(0) >= 0.99 * WEAROUT_MAX_MULTIPLIER


def test_wearout_onset_is_at_average_life(pipe_attrs):
    at = _pipe_at_life_ratio(pipe_attrs, "DI", WEAROUT_ONSET_LIFE_RATIO)
    just_past = _pipe_at_life_ratio(pipe_attrs, "DI", WEAROUT_ONSET_LIFE_RATIO + 0.01)
    assert at.wearout_multiplier(0) == 1.0
    assert just_past.wearout_multiplier(0) > 1.0


def test_wearout_curve_matches_specification(pipe_attrs):
    """1 + (M-1)(1 - exp(-k * excess)) at a few named points."""
    for ratio in (1.1, 1.25, 1.5, 2.0, 3.0):
        p = _pipe_at_life_ratio(pipe_attrs, "AC", ratio)
        excess = ratio - WEAROUT_ONSET_LIFE_RATIO
        expected = 1 + (WEAROUT_MAX_MULTIPLIER - 1) * (1 - math.exp(-WEAROUT_STEEPNESS * excess))
        assert p.wearout_multiplier(0) == pytest.approx(expected, rel=1e-9)


def test_wearout_resets_on_replacement(pipe_attrs):
    from water_replacement import ReplacementManager

    p = _pipe_at_life_ratio(pipe_attrs, "AC", 1.5)
    assert p.wearout_multiplier(10) > 1.0
    p.current_condition = 1.5
    ReplacementManager(budget=1e9, rehab_trigger=2.0).run_year([p], 10)
    hdpe_life = STANDARD_LIFE["HDPE"]["base_life"]
    for years_after in (0, 10, int(hdpe_life) - 1):
        assert p.wearout_multiplier(10 + years_after) == 1.0


def test_max_multiplier_of_one_disables_wearout(pipe_attrs, monkeypatch):
    """With the ceiling at 1.0 the hazard is the pre-change formula exactly."""
    monkeypatch.setattr(_lc, "WEAROUT_MAX_MULTIPLIER", 1.0)
    for material in sorted(STANDARD_LIFE):
        p = Pipe({**pipe_attrs, "Material": material, "Age": 0})
        p.n_breaks = 3
        for y in (0, 50, 100, 200):
            t = max(p.initial_age + y, 0.1)
            h0 = (p.beta / p.eta) * ((t / p.eta) ** (p.beta - 1))
            cov = p.mat_mult * np.exp(COEFF_DIAMETER * p.diameter)
            leyp = 1.0 + ALPHA * min(p.n_breaks, LEYP_BREAK_FEEDBACK_CAP)
            assert p.wearout_multiplier(y) == 1.0
            assert p.calculate_hazard(y) == pytest.approx(h0 * cov * leyp, rel=1e-12)


def test_inherited_backlog_starts_in_wearout(pipe_attrs):
    """AC/GI/CI at their inventory ages are already past onset; DI at 96 is not."""
    assert Pipe({**pipe_attrs, "Material": "AC", "Age": 76}).wearout_multiplier(0) > 1.0
    assert Pipe({**pipe_attrs, "Material": "GI", "Age": 76}).wearout_multiplier(0) > 1.0
    assert Pipe({**pipe_attrs, "Material": "CI", "Age": 96}).wearout_multiplier(0) > 1.0
    assert Pipe({**pipe_attrs, "Material": "DI", "Age": 96}).wearout_multiplier(0) == 1.0


def test_hazard_still_scales_with_leyp_feedback(pipe_attrs):
    """Wear-out composes multiplicatively with the break-history term."""
    p = _pipe_at_life_ratio(pipe_attrs, "AC", 1.5)
    p.n_breaks = 0
    base = p.calculate_hazard(0)
    p.n_breaks = 4
    assert p.calculate_hazard(0) == pytest.approx(base * (1 + ALPHA * 4))
