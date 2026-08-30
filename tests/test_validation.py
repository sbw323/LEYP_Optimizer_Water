"""Phase 6 tests: the validation curve must be measured, not inferred."""

import numpy as np
import pandas as pd
import pytest

import water_validation
from water_validation import (
    generate_validation_curve,
    plot_validation_curve,
    save_validation_data,
    summarize_validation,
)

REAL_INVENTORY = "Louisa_wConduits_Input_CSV.csv"


@pytest.fixture(scope="module")
def curve():
    return generate_validation_curve(
        REAL_INVENTORY, budget_min=0.0, budget_max=2_000_000.0,
        n_points=8, n_replicates=2,
    )


# --- Item 20: counts come from the action log ----------------------------------

def test_no_fictional_average_pipe_estimators():
    """The old module inferred counts from an assumed 8in x 300ft pipe."""
    source = open("water_validation.py").read()
    for gone in (
        "_estimate_replaced_count_from_cip_cost",
        "_estimate_replaced_length_from_cip_cost",
        "_estimate_break_count_from_emergency_cost",
    ):
        assert gone not in source


def test_replacement_percentages_are_bounded(curve):
    for col in ("pct_replaced_by_number", "pct_replaced_by_length"):
        assert curve[col].between(0, 100).all()


def test_zero_budget_replaces_nothing(curve):
    first = curve.iloc[0]
    assert first["budget"] == 0.0
    assert first["pct_replaced_by_number"] == 0.0
    assert first["pct_replaced_by_length"] == 0.0


def test_counts_track_the_simulation(curve):
    """The reported coverage must match what the simulation actually did."""
    from leyp_runner import simulate

    row = curve.iloc[-1]
    run = simulate(
        REAL_INVENTORY,
        annual_budget=row["budget"],
        rehab_trigger=water_validation.VALIDATION_TRIGGER,
        seed=water_validation.VALIDATION_SEED,
        n_replicates=2,
    )
    expected = 100.0 * run["cip_pipes"] / run["n_pipes"]
    assert row["pct_replaced_by_number"] == pytest.approx(expected, rel=1e-6)


# --- Item 21: the length panel is genuinely length-based -----------------------

def test_length_axis_is_not_a_copy_of_the_count_axis(curve):
    """Previously pct_avoided_by_length was assigned from the count series."""
    assert not curve["pct_avoided_by_length"].equals(curve["pct_avoided_by_number"])
    assert not curve["pct_replaced_by_length"].equals(curve["pct_replaced_by_number"])


# --- Item 22: spread is reported, monotonicity is not imposed ------------------

def test_monotonic_filter_is_gone():
    assert "_make_monotonic_increasing" not in open("water_validation.py").read()


def test_replicate_spread_is_reported(curve):
    for col in ("sd_avoided_by_number", "sd_avoided_by_length"):
        assert col in curve.columns
        assert (curve[col] >= 0).all()


def test_values_are_reported_exactly_as_measured(curve):
    """No post-processing between simulation and plot.

    The old filter overwrote every decrease with the previous value, so a
    reported point need not correspond to any run. Each y here must equal an
    independent recomputation at the same seed. Asserting the curve is
    non-monotonic would instead be testing the RNG: a measured curve may come
    out monotonic by chance.
    """
    from leyp_runner import simulate

    row = curve.iloc[len(curve) // 2]
    baseline = simulate(
        REAL_INVENTORY, annual_budget=0.0,
        rehab_trigger=water_validation.VALIDATION_TRIGGER,
        seed=water_validation.VALIDATION_SEED, n_replicates=2,
    )
    run = simulate(
        REAL_INVENTORY, annual_budget=row["budget"],
        rehab_trigger=water_validation.VALIDATION_TRIGGER,
        seed=water_validation.VALIDATION_SEED, n_replicates=2,
    )
    expected = 100.0 * (
        baseline["total_breaks"] - run["total_breaks"]
    ) / baseline["total_breaks"]
    assert row["pct_avoided_by_number"] == pytest.approx(max(0.0, expected), rel=1e-6)


# --- Item 23: the sweep spans the network -------------------------------------

def test_sweep_reaches_full_coverage(curve):
    """The old sweep topped out at 11.6% of pipes, so the curve was unreadable."""
    assert curve["pct_replaced_by_number"].max() > 90.0
    assert curve["pct_replaced_by_length"].max() > 80.0


def test_low_coverage_region_is_sampled(curve):
    """Coverage is steeply non-linear in budget; linear spacing missed this half."""
    assert (curve["pct_replaced_by_number"] <= 50.0).sum() >= 3


# --- Item 24: the baseline is a real counterfactual ----------------------------

def test_baselines_are_reported(curve):
    """Both references are recorded so the y axis can be interpreted."""
    assert curve["no_intervention_breaks"].iloc[0] > curve["reactive_only_breaks"].iloc[0]


def test_avoidance_is_measured_against_reactive_only(curve):
    """y must be attributable to CIP, so a zero budget must avoid nothing.

    Measuring against a no-intervention run instead credits CIP with
    everything emergency response already achieves.
    """
    assert curve["pct_avoided_by_number"].iloc[0] == 0.0
    assert curve["pct_avoided_by_length"].iloc[0] == 0.0


def test_avoidance_percentages_are_bounded(curve):
    for col in ("pct_avoided_by_number", "pct_avoided_by_length"):
        assert curve[col].between(0, 100).all()


# --- Outputs -------------------------------------------------------------------

def test_plot_and_data_are_written(curve, tmp_path):
    png, csv = tmp_path / "v.png", tmp_path / "v.csv"
    plot_validation_curve(curve, str(png))
    save_validation_data(curve, str(csv))
    assert png.stat().st_size > 0
    assert len(pd.read_csv(csv)) == len(curve)


def test_summary_reports_reach_and_diagonal(curve):
    s = summarize_validation(curve)
    for key in (
        "max_pct_replaced_by_number",
        "max_pct_replaced_by_length",
        "above_diagonal_first_50_by_number",
        "above_diagonal_first_50_by_length",
    ):
        assert key in s
    assert isinstance(s["above_diagonal_first_50_by_number"], bool)
