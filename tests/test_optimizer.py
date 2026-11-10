"""Phase 5 tests: front annotation, strategy selection, and algorithm sizing."""

import numpy as np
import pandas as pd
import pytest
import yaml

from leyp_optimizer import annotate_front, select_strategy


@pytest.fixture
def front():
    """A small synthetic front: investment up, risk down."""
    df = pd.DataFrame(
        {
            "Investment_Cost": [0.0, 40.0, 80.0, 120.0, 200.0],
            "Risk_Cost": [600.0, 380.0, 190.0, 120.0, 60.0],
        }
    )
    df["Total_Cost"] = df["Investment_Cost"] + df["Risk_Cost"]
    return annotate_front(df)


def test_annotation_normalises_to_unit_range(front):
    for col in ("Norm_Investment", "Norm_Risk"):
        assert front[col].min() == pytest.approx(0.0)
        assert front[col].max() == pytest.approx(1.0)


def test_ideal_distance_matches_normalised_objectives(front):
    expected = np.sqrt(front["Norm_Investment"] ** 2 + front["Norm_Risk"] ** 2)
    assert front["Ideal_Distance"].values == pytest.approx(expected.values)


def test_annotation_handles_a_degenerate_front():
    """A front where every solution is identical must not divide by zero."""
    df = pd.DataFrame({"Investment_Cost": [10.0, 10.0], "Risk_Cost": [5.0, 5.0]})
    df["Total_Cost"] = df["Investment_Cost"] + df["Risk_Cost"]
    out = annotate_front(df)
    assert out["Norm_Investment"].tolist() == [0.0, 0.0]
    assert out["Ideal_Distance"].notna().all()


def test_min_total_cost_selects_the_cheapest(front):
    assert select_strategy(front, "min_total_cost") == front["Total_Cost"].idxmin()


def test_knee_selects_the_closest_to_ideal(front):
    assert select_strategy(front, "knee") == front["Ideal_Distance"].idxmin()


def test_the_two_rules_can_disagree(front):
    """If they always agreed the choice would not be worth exposing."""
    assert select_strategy(front, "min_total_cost") != select_strategy(front, "knee")


def test_unknown_selection_method_is_rejected(front):
    with pytest.raises(ValueError):
        select_strategy(front, "whatever")


def test_annotation_leaves_objectives_untouched(front):
    assert front["Total_Cost"].tolist() == [
        i + r for i, r in zip(front["Investment_Cost"], front["Risk_Cost"])
    ]


# --- Configuration sanity ------------------------------------------------------

@pytest.fixture(scope="module")
def config():
    with open("optimizer_config.yaml") as f:
        return yaml.safe_load(f)


def test_search_is_large_enough_to_resolve_a_front(config):
    """50 evaluations could not separate signal from simulation noise."""
    alg = config["algorithm"]
    evaluations = alg["pop_size"] + alg["n_gen"] * alg["n_offsprings"]
    assert evaluations >= 400


def test_budget_ceiling_can_express_a_solvent_policy(config):
    """The ceiling must allow renewing the network within the horizon.

    At $120/inch-ft this network costs about $84.4M to renew once; a ceiling
    of $500k/yr over 100 years could only fund $50M of that.
    """
    from leyp_config import SIMULATION_YEARS

    assert config["genes"]["budget"]["max"] * SIMULATION_YEARS >= 84_400_000


def test_replicates_are_averaged(config):
    assert config["simulation"]["n_replicates"] >= 3


def test_selection_rule_is_recognised(config):
    assert config.get("selection", "min_total_cost") in {"min_total_cost", "knee"}


def test_emergency_carries_a_premium_over_planned_work():
    """Proactive renewal must be cheaper per foot than reactive replacement."""
    from leyp_config import (
        CIP_REPLACEMENT_COST_PER_INCH_FT,
        EMERGENCY_REPLACEMENT_COST_PER_FT,
    )

    # Compared at the network's dominant 6-inch diameter.
    cip_per_ft = CIP_REPLACEMENT_COST_PER_INCH_FT * 6
    assert EMERGENCY_REPLACEMENT_COST_PER_FT > cip_per_ft * 2


# --- The symptom: an interior budget optimum ------------------------------------

# Wide enough that a genuine interior optimum has room on both sides, and run
# at the configured replicate count: the cost surface is flat above the
# optimum (plan 2.2), so at fewer replicates the argmin is decided by noise.
_SWEEP = (10_000, 200_000, 500_000, 1_000_000, 1_500_000, 2_000_000, 3_000_000, 5_000_000)


def _configured_replicates():
    import yaml

    return int(yaml.safe_load(open("optimizer_config.yaml"))["simulation"]["n_replicates"])


def _min_cost_budget(n_replicates=None):
    n_replicates = n_replicates or _configured_replicates()
    from leyp_runner import simulate

    totals = {}
    for b in _SWEEP:
        r = simulate("Louisa_wConduits_Input_CSV.csv", annual_budget=b, rehab_trigger=1.5,
                     seed=20260830, n_replicates=n_replicates)
        totals[b] = r["investment_cost"] + r["risk_cost"]
    return min(totals, key=totals.get)


def test_budget_optimum_is_interior():
    """The optimizer no longer drives the budget to a bound."""
    best = _min_cost_budget()
    assert best not in (_SWEEP[0], _SWEEP[-1]), f"optimum at a bound: ${best:,}"


def test_wearout_is_load_bearing_for_interior_optimum(monkeypatch):
    """Without wear-out the optimum sits at a bound again.

    Pins the finding that the age-related term is what gives the capital
    programme a target worth funding: it concentrates risk in old pipes that
    CIP can reach by risk-per-dollar. Remove it and risk is spread evenly, so
    either buying everything always pays or nothing does.
    """
    import leyp_core

    monkeypatch.setattr(leyp_core, "WEAROUT_MAX_MULTIPLIER", 1.0)
    best = _min_cost_budget()
    assert best in (_SWEEP[0], _SWEEP[-1]), f"unexpected interior optimum without wear-out: ${best:,}"


# --- Pareto cost curve (nominal) and the --cost-curve-only flag -----------------

from leyp_optimizer import (  # noqa: E402
    _parse_args,
    _within_tolerance_runs,
    add_nominal_costs,
    plot_pareto_cost_curve,
    run_optimization,
)


def _nominal_front(nominal_risk):
    """Synthetic front: PV objectives, plus nominal columns with a chosen risk."""
    df = pd.DataFrame(
        {
            "Investment_Cost": [0.0, 40.0, 80.0, 120.0, 200.0],
            "Risk_Cost": [600.0, 380.0, 190.0, 120.0, 60.0],
            "Budget": [1e4, 1e5, 2e5, 4e5, 8e5],
            "Rehab_Trigger": [1.5] * 5,
        }
    )
    df["Total_Cost"] = df["Investment_Cost"] + df["Risk_Cost"]
    df = annotate_front(df)
    df["Nominal_Investment"] = df["Investment_Cost"] * 3e6
    df["Nominal_Risk"] = np.asarray(nominal_risk, dtype=float) * 1e6
    df["Nominal_Total"] = df["Nominal_Investment"] + df["Nominal_Risk"]
    df["Avg_Annual_Spend"] = df["Nominal_Investment"] / 100
    return df


def test_within_tolerance_runs_finds_contiguous_spans():
    x = [1, 2, 3, 4, 5, 6]
    total = [10.0, 9.0, 12.0, 9.2, 9.3, 20.0]   # min 9.0; 5% -> 9.45
    assert _within_tolerance_runs(x, total, 0.05) == [(2, 2), (4, 5)]


def test_within_tolerance_runs_always_includes_the_minimum():
    assert _within_tolerance_runs([1, 2, 3], [5.0, 1.0, 5.0], 0.0) == [(2, 2)]


def test_cost_curve_marks_the_knee_selected(tmp_path):
    df = _nominal_front([1800, 1000, 500, 250, 400])   # knee 2, nominal min 3
    assert select_strategy(df, "knee") != df["Nominal_Total"].idxmin()
    info = plot_pareto_cost_curve(df, str(tmp_path), selection="knee")
    assert (tmp_path / "pareto_cost_curve.png").stat().st_size > 0
    labels = info["legend_labels"]
    assert labels[:4] == ["Total within 5% of minimum", "Investment", "Risk",
                          "Total (investment + risk)"]
    assert "Knee (balanced) — selected" in labels
    assert sum("selected" in label for label in labels) == 1


def test_both_markers_selected_when_knee_is_the_minimum(tmp_path):
    """One strategy can be both; each marker then truthfully says selected."""
    df = _nominal_front([1800, 1000, 300, 350, 100])   # knee = nominal min = 2
    assert select_strategy(df, "knee") == df["Nominal_Total"].idxmin()
    labels = plot_pareto_cost_curve(df, str(tmp_path), selection="knee")["legend_labels"]
    assert "Minimum total cost — selected" in labels
    assert "Knee (balanced) — selected" in labels
    assert len(labels) == 6


def test_star_is_the_minimum_of_the_plotted_nominal_total(tmp_path):
    df = _nominal_front([1800, 1000, 300, 350, 100])   # nominal totals min at index 2
    info = plot_pareto_cost_curve(df, str(tmp_path), selection="knee")
    assert info["min_index"] == df["Nominal_Total"].idxmin() == 2


def test_min_total_cost_selected_on_the_star_when_they_coincide(tmp_path):
    df = _nominal_front([1800, 1000, 500, 250, 400])   # nominal min = PV min = index 3
    assert select_strategy(df, "min_total_cost") == df["Nominal_Total"].idxmin()
    info = plot_pareto_cost_curve(df, str(tmp_path), selection="min_total_cost")
    assert "Minimum total cost — selected" in info["legend_labels"]
    assert len(info["legend_labels"]) == 6


def test_pv_pick_gets_its_own_marker_when_it_is_not_the_star(tmp_path):
    """The PV optimum must never be passed off as the nominal minimum."""
    df = _nominal_front([1800, 1000, 300, 350, 100])
    pv_pick = select_strategy(df, "min_total_cost")
    assert pv_pick != df["Nominal_Total"].idxmin()
    assert pv_pick != select_strategy(df, "knee")
    info = plot_pareto_cost_curve(df, str(tmp_path), selection="min_total_cost")
    labels = info["legend_labels"]
    assert "Lowest present-value cost — selected" in labels
    assert "Minimum total cost" in labels               # unselected, no suffix
    assert sum("selected" in label for label in labels) == 1


def test_cost_curve_requires_nominal_columns(tmp_path):
    df = _nominal_front([1800, 1000, 300, 350, 100]).drop(columns=["Nominal_Total"])
    with pytest.raises(ValueError, match="add_nominal_costs"):
        plot_pareto_cost_curve(df, str(tmp_path))


def test_add_nominal_costs_reproduces_the_optimizers_objectives(inventory_csv):
    """Same seed and replicates as the evaluation: identical present values."""
    from leyp_config import SIMULATION_YEARS
    from leyp_runner import run_simulation

    config = {"simulation": {"seed": 11, "n_replicates": 2}, "discount_rate": 0.03}
    rows = []
    for budget, trigger in ((50_000.0, 2.0), (200_000.0, 3.0)):
        inv, risk = run_simulation(override_input_path=inventory_csv, annual_budget=budget,
                                   rehab_trigger=trigger, seed=11, n_replicates=2,
                                   discount_rate=0.03)
        rows.append({"Budget": budget, "Rehab_Trigger": trigger,
                     "Investment_Cost": inv, "Risk_Cost": risk})
    out = add_nominal_costs(pd.DataFrame(rows), config, inventory_csv)

    assert out.attrs["pv_reproduction_error"] < 1e-6
    assert (out["Nominal_Investment"] >= out["Investment_Cost"]).all()   # discounting only shrinks
    assert out["Nominal_Total"].tolist() == pytest.approx(
        (out["Nominal_Investment"] + out["Nominal_Risk"]).tolist())
    assert out["Avg_Annual_Spend"].tolist() == pytest.approx(
        (out["Nominal_Investment"] / SIMULATION_YEARS).tolist())


def test_cli_flag_parses():
    assert _parse_args(["--cost-curve-only"]).cost_curve_only is True
    assert _parse_args([]).cost_curve_only is False


@pytest.fixture
def tiny_optimizer(inventory_csv, tmp_path, monkeypatch):
    """A seconds-long optimizer run on the three-pipe inventory, in tmp_path."""
    import leyp_optimizer

    config = {
        "master_input_file": inventory_csv,
        "output_base_dir": "out",
        "skip_segmentation": True,
        "genes": {"budget": {"min": 10_000, "max": 200_000},
                  "rehab_trigger": {"min": 1.0, "max": 3.5}},
        "discount_rate": 0.03,
        "simulation": {"seed": 7, "n_replicates": 1},
        "validation": {"n_points": 3},
        "selection": "knee",
        "algorithm": {"pop_size": 6, "n_offsprings": 3, "n_gen": 2, "seed": 1},
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leyp_optimizer, "load_config", lambda: config)
    return tmp_path / "out"


def test_cost_curve_only_writes_only_the_graph_and_its_data(tiny_optimizer):
    df = run_optimization(cost_curve_only=True)
    written = {p.name for p in tiny_optimizer.iterdir()}
    assert written == {"pareto_cost_curve.png", "pareto_cost_curve_data.csv"}
    assert not (tiny_optimizer.parent / "nsga2_checkpoint.pkl").exists()
    data = pd.read_csv(tiny_optimizer / "pareto_cost_curve_data.csv")
    assert len(data) == len(df) and "Nominal_Total" in data.columns


def test_full_run_also_writes_the_cost_curve(tiny_optimizer):
    run_optimization()
    written = {p.name for p in tiny_optimizer.iterdir()}
    assert {"pareto_cost_curve.png", "optimization_curve.png", "nsga2_results.csv",
            "Optimal_Action_Plan.csv", "cost_summary.csv", "simulation_diagnostics.csv",
            "validation_curve.png", "validation_data.csv"} <= written
    assert "pareto_cost_curve_data.csv" not in written   # its data is in nsga2_results.csv
    results = pd.read_csv(tiny_optimizer / "nsga2_results.csv")
    for col in ("Nominal_Investment", "Nominal_Risk", "Nominal_Total", "Avg_Annual_Spend"):
        assert col in results.columns
