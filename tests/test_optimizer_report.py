"""Optimizer outputs: selection rules, plateau, report figure, strategy summary,
and the --cost-curve-only and --plot-only entry points.

The rules, plateau, figure and summary are checked on small synthetic fronts
with known totals and slopes, so no optimisation runs. The entry points are
checked on a seconds-long run against a three-pipe inventory.
"""

import os

import numpy as np
import pandas as pd
import pytest

import leyp_optimizer
from leyp_optimizer import (
    PICK_STYLES,
    SELECTION_METHODS,
    _parse_args,
    add_nominal_costs,
    marginal_return,
    plateau_points,
    plot_optimization_curve,
    plot_pareto_cost_curve,
    replot,
    run_optimization,
    select_strategy,
    selection_settings,
    write_strategy_summary,
)

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAVED_FRONT = os.path.join(REPO_DIR, "Optimization_Results_NSGA2", "nsga2_results.csv")


def _front():
    """Five points; totals 10.0, 6.0, 4.5, 4.6, 5.2 ($M)."""
    return pd.DataFrame({
        "Investment_Cost": [1e6, 2e6, 3e6, 4e6, 5e6],
        "Risk_Cost": [9e6, 4e6, 1.5e6, 0.6e6, 0.2e6],
        "Budget": [1e5, 2e5, 3e5, 4e5, 5e5],
        "Rehab_Trigger": [1.2, 1.4, 1.6, 1.8, 2.0],
    }).assign(Total_Cost=lambda d: d.Investment_Cost + d.Risk_Cost)


def _report_front(rate=0.03, nominal_risk=None):
    """A seven-point discounted front with the columns add_nominal_costs adds."""
    df = pd.DataFrame({
        "Investment_Cost": [0.5e6, 1e6, 2e6, 3e6, 4e6, 5e6, 6e6],
        "Risk_Cost": [20e6, 12e6, 5e6, 2.2e6, 1.2e6, 0.8e6, 0.6e6],
        "Budget": [2e4, 4e4, 8e4, 1.2e5, 1.6e5, 2e5, 2.4e5],
        "Rehab_Trigger": [1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8],
    }).assign(Total_Cost=lambda d: d.Investment_Cost + d.Risk_Cost)
    df["Discount_Rate"] = rate
    df["Nominal_Investment"] = df["Investment_Cost"] * 2.5
    df["Nominal_Risk"] = (df["Risk_Cost"] * 3.0 if nominal_risk is None
                          else np.asarray(nominal_risk, dtype=float))
    df["Nominal_Total"] = df["Nominal_Investment"] + df["Nominal_Risk"]
    df["Avg_Annual_Spend"] = df["Nominal_Investment"] / 100
    df["Failures"] = [300.0, 200.0, 120.0, 60.0, 40.0, 30.0, 25.0]
    df["Emergency_Replaced_Pipes"] = [40.0, 25.0, 12.0, 5.0, 3.0, 2.0, 1.0]
    df["Backlog_Pipes"] = 128.0
    df["Backlog_Length"] = 31_300.0
    return df


# --- Selection rules and plateau -------------------------------------------------


def test_three_rules_on_a_known_front():
    df = _front()
    assert select_strategy(df, "min_total_cost") == 2              # 4.5M
    assert select_strategy(df, "knee") == 1                        # normalised distances 1.0, 0.50, 0.52, 0.75, 1.0
    assert select_strategy(df, "max_marginal_return", window=3) == 1
    with pytest.raises(ValueError):
        select_strategy(df, "other")


def test_rules_survive_a_degenerate_front():
    """Every strategy identical: each rule still returns a row, no division by zero."""
    df = pd.DataFrame({"Investment_Cost": [5e6, 5e6], "Risk_Cost": [1e6, 1e6]})
    df["Total_Cost"] = df["Investment_Cost"] + df["Risk_Cost"]
    for rule in SELECTION_METHODS:
        assert select_strategy(df, rule) in df.index


def test_marginal_return_is_the_windowed_slope_at_the_window_centre():
    df = _front()
    mr = marginal_return(df, window=3)
    # Risk falls 9 -> 1.5 over the first three points: 3.75 avoided per $.
    assert mr.idxmax() == 1 and mr[1] == pytest.approx((9e6 - 1.5e6) / (3e6 - 1e6))
    assert mr[[0, 4]].isna().all()                                 # no window centred on the ends
    # A window wider than the front shrinks to the front; row order is irrelevant.
    assert select_strategy(df, "max_marginal_return", window=50) == 2
    shuffled = df.sample(frac=1, random_state=0)
    assert select_strategy(shuffled, "max_marginal_return", window=3) == 1
    assert marginal_return(df.iloc[:1]).isna().all()


def test_marginal_return_is_about_one_at_the_minimum_total():
    """On a smooth front the minimum total is where a dollar avoids a dollar."""
    inv = np.linspace(1e6, 9e6, 41)
    df = pd.DataFrame({"Investment_Cost": inv, "Risk_Cost": 9e12 / inv})
    df["Total_Cost"] = df["Investment_Cost"] + df["Risk_Cost"]
    best = select_strategy(df, "min_total_cost")
    assert df.loc[best, "Investment_Cost"] == pytest.approx(3e6)
    assert marginal_return(df)[best] == pytest.approx(1.0, rel=0.05)


def test_plateau_points():
    df = _front()
    assert set(plateau_points(df, 0.05).index) == {2, 3}           # <= 4.725M
    assert set(plateau_points(df, 0.20).index) == {2, 3, 4}        # <= 5.4M
    assert len(plateau_points(df, 0.0)) == 1


def test_selection_settings_validate_the_config():
    assert selection_settings({}) == ("min_total_cost", 0.05)
    assert selection_settings({"selection": "max_marginal_return", "plateau_tolerance": 0.1}) == (
        "max_marginal_return", 0.1)
    with pytest.raises(ValueError, match="selection"):
        selection_settings({"selection": "cheapest"})
    with pytest.raises(ValueError, match="plateau_tolerance"):
        selection_settings({"plateau_tolerance": -0.01})


# --- Report figure ------------------------------------------------------------------


def test_figure_states_the_runs_rate_and_its_vocabulary(tmp_path):
    info = plot_optimization_curve(_report_front(), str(tmp_path / "c.png"), selection="knee")
    assert (tmp_path / "c.png").stat().st_size > 10_000
    assert info["title"] == ("Optimisation output: 100-year cost against investment "
                             "along the Pareto front")
    assert info["xlabel"] == "100-year investment, present value at 3.0% ($ million)"
    assert info["ylabel"] == "100-year cost, present value at 3.0% ($ million)"
    assert info["legend_labels"] == [
        "Total within 5% of minimum", "Investment", "Risk", "Total (investment + risk)",
        "Minimum total cost", "Knee (balanced) — selected", "Max marginal return"]
    assert info["callout_conflicts"] == 0


def test_figure_reads_the_rate_from_the_front(tmp_path):
    info = plot_optimization_curve(_report_front(rate=0.05), str(tmp_path / "c.png"))
    assert "present value at 5.0%" in info["xlabel"]
    undiscounted = _report_front(rate=0.0).drop(columns=["Nominal_Investment", "Nominal_Risk"])
    info = plot_optimization_curve(undiscounted, str(tmp_path / "n.png"))
    assert info["xlabel"] == "100-year investment, nominal ($ million)"


@pytest.mark.parametrize("rule", SELECTION_METHODS)
def test_only_the_configured_rule_is_flagged(tmp_path, rule):
    info = plot_optimization_curve(_report_front(), str(tmp_path / "c.png"), selection=rule)
    flagged = [label for label in info["legend_labels"] if label.endswith("— selected")]
    assert flagged == [PICK_STYLES[rule][3] + " — selected"]
    assert info["selected_index"] == select_strategy(_report_front(), rule)


def test_markers_are_where_the_definitions_put_them(tmp_path):
    df = _report_front()
    info = plot_optimization_curve(df, str(tmp_path / "c.png"))
    assert info["picks"]["min_total_cost"] == df["Total_Cost"].idxmin()
    assert info["picks"]["max_marginal_return"] == marginal_return(df).idxmax()
    lo, hi = info["plateau_range"]
    inside = df[(df.Investment_Cost >= lo) & (df.Investment_Cost <= hi)]
    assert (inside["Total_Cost"] <= 1.05 * df["Total_Cost"].min()).all()


def test_spend_callouts_are_nominal_investment_over_the_horizon(tmp_path):
    df = _report_front()
    info = plot_optimization_curve(df, str(tmp_path / "c.png"))
    idx = info["picks"]["min_total_cost"]
    spend = df.loc[idx, "Nominal_Investment"] / 100            # not Budget, not a PV
    text = next(c["text"] for c in info["callouts"] if c["text"].startswith("Minimum total cost"))
    assert f"{spend / 1e3:,.0f}k/yr average spend" in text


def test_rules_that_pick_one_strategy_share_a_callout(tmp_path):
    df = _front().assign(Discount_Rate=0.0)
    df.loc[1, "Risk_Cost"] = 2.0e6                              # point 1: min total and knee
    df["Total_Cost"] = df.Investment_Cost + df.Risk_Cost
    assert select_strategy(df, "knee") == select_strategy(df, "min_total_cost") == 1
    info = plot_optimization_curve(df, str(tmp_path / "c.png"), selection="knee")
    texts = [c["text"] for c in info["callouts"]]
    assert any(t.startswith("Minimum total cost = Knee (balanced)") for t in texts)
    assert info["callout_conflicts"] == 0


def test_discounted_front_without_nominal_columns_is_refused(tmp_path):
    df = _report_front().drop(columns=["Nominal_Investment"])
    with pytest.raises(ValueError, match="add_nominal_costs"):
        plot_optimization_curve(df, str(tmp_path / "c.png"))


def test_nominal_chart_rings_the_selected_strategy_when_its_nominal_pick_differs(tmp_path):
    """On nominal costs the PV optimum must not be passed off as the nominal minimum."""
    df = _report_front(nominal_risk=[60e6, 40e6, 14e6, 9e6, 3e6, 2.5e6, 2.4e6])
    pv_pick = select_strategy(df, "min_total_cost")
    assert pv_pick != df["Nominal_Total"].idxmin()
    info = plot_pareto_cost_curve(df, str(tmp_path), selection="min_total_cost")
    assert info["xlabel"] == "100-year investment, nominal ($ million)"
    assert info["picks"]["min_total_cost"] == df["Nominal_Total"].idxmin()
    labels = info["legend_labels"]
    assert "Minimum total cost" in labels                       # nominal pick, unflagged
    assert labels[-1] == "Minimum total cost, present value — selected"
    assert info["callout_conflicts"] == 0


def test_nominal_chart_flags_the_marker_when_the_picks_agree(tmp_path):
    df = _report_front()
    assert select_strategy(df, "knee") == select_strategy(
        leyp_optimizer.nominal_view(df), "knee")
    info = plot_pareto_cost_curve(df, str(tmp_path), selection="knee")
    assert "Knee (balanced) — selected" in info["legend_labels"]
    assert len(info["legend_labels"]) == 7


@pytest.mark.skipif(not os.path.exists(SAVED_FRONT), reason="no saved front")
def test_callouts_are_clear_on_the_saved_front(tmp_path):
    df = pd.read_csv(SAVED_FRONT)
    for rule in SELECTION_METHODS:
        assert plot_optimization_curve(df, str(tmp_path / "c.png"), rule)["callout_conflicts"] == 0
        assert plot_pareto_cost_curve(df, str(tmp_path), rule)["callout_conflicts"] == 0


# --- Strategy summary -----------------------------------------------------------------


def test_strategy_summary(tmp_path):
    df = _report_front()
    path = tmp_path / "selected_strategies.csv"
    write_strategy_summary(df, str(path), selection="knee")
    s = pd.read_csv(path)

    assert list(s["strategy"][:3]) == list(SELECTION_METHODS)
    assert s["strategy"].iloc[-1] == "plateau (total within 5% of minimum)"
    assert s["selected"].tolist() == [False, True, False, False]
    assert (s["basis"] == "present value at 3.0% (end-of-year)").all()

    rows = s.set_index("strategy")
    mt = df.loc[df["Total_Cost"].idxmin()]
    assert float(rows.loc["min_total_cost", "total_100yr"]) == mt["Total_Cost"]
    assert float(rows.loc["min_total_cost", "annual_budget"]) == mt["Budget"]
    assert float(rows.loc["min_total_cost", "rehab_trigger"]) == mt["Rehab_Trigger"]
    assert float(rows.loc["min_total_cost", "avg_annual_spend"]) == pytest.approx(
        mt["Nominal_Investment"] / 100)
    assert float(rows.loc["min_total_cost", "failures_100yr"]) == mt["Failures"]
    assert float(rows.loc["knee", "backlog_pipes"]) == 128
    mr = rows["risk_avoided_per_dollar_invested"].astype(float)
    assert mr["max_marginal_return"] == mr.max() and mr["min_total_cost"] < mr["max_marginal_return"]

    plateau = plateau_points(df, 0.05)
    p = rows.iloc[-1]
    assert int(p["n_front_points"]) == len(plateau)
    assert p["annual_budget"] == f"{plateau.Budget.min():.0f}-{plateau.Budget.max():.0f}"


def test_summary_basis_is_nominal_for_an_undiscounted_front(tmp_path):
    df = _report_front(rate=0.0).drop(columns=["Nominal_Investment", "Nominal_Risk"])
    s = write_strategy_summary(df, str(tmp_path / "s.csv"))
    assert (s["basis"] == "nominal").all()
    assert s.loc[0, "investment_100yr_nominal"] == s.loc[0, "investment_100yr"]


# --- Re-simulation of the front ----------------------------------------------------------


def test_add_nominal_costs_reproduces_the_optimizers_objectives(inventory_csv):
    """Same seed and replicates as the evaluation: identical present values."""
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
    assert (out["Discount_Rate"] == 0.03).all()
    assert (out["Nominal_Investment"] >= out["Investment_Cost"]).all()   # discounting only shrinks
    assert out["Nominal_Total"].tolist() == pytest.approx(
        (out["Nominal_Investment"] + out["Nominal_Risk"]).tolist())
    assert out["Avg_Annual_Spend"].tolist() == pytest.approx(
        (out["Nominal_Investment"] / 100).tolist())
    for col in ("Failures", "Emergency_Replaced_Pipes", "Backlog_Pipes", "Backlog_Length"):
        assert (out[col] >= 0).all()


# --- Entry points ------------------------------------------------------------------------


def test_cli_flags_parse():
    assert _parse_args(["--cost-curve-only"]).cost_curve_only is True
    assert _parse_args(["--plot-only"]).plot_only is True
    args = _parse_args([])
    assert not args.cost_curve_only and not args.plot_only
    with pytest.raises(SystemExit):
        _parse_args(["--plot-only", "--cost-curve-only"])


@pytest.fixture
def tiny_config(inventory_csv):
    return {
        "master_input_file": inventory_csv,
        "output_base_dir": "out",
        "skip_segmentation": True,
        "genes": {"budget": {"min": 10_000, "max": 200_000},
                  "rehab_trigger": {"min": 1.0, "max": 3.5}},
        "discount_rate": 0.03,
        "simulation": {"seed": 7, "n_replicates": 1},
        "validation": {"n_points": 3},
        "selection": "knee",
        "plateau_tolerance": 0.05,
        "algorithm": {"pop_size": 6, "n_offsprings": 3, "n_gen": 2, "seed": 1},
    }


@pytest.fixture
def tiny_optimizer(tiny_config, tmp_path, monkeypatch):
    """A seconds-long optimizer run on the three-pipe inventory, in tmp_path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(leyp_optimizer, "load_config", lambda: tiny_config)
    return tmp_path / "out"


def test_cost_curve_only_writes_only_the_graph_and_its_data(tiny_optimizer):
    df = run_optimization(cost_curve_only=True)
    written = {p.name for p in tiny_optimizer.iterdir()}
    assert written == {"pareto_cost_curve.png", "pareto_cost_curve_data.csv"}
    assert not (tiny_optimizer.parent / "nsga2_checkpoint.pkl").exists()
    data = pd.read_csv(tiny_optimizer / "pareto_cost_curve_data.csv")
    assert len(data) == len(df) and "Nominal_Total" in data.columns


def test_full_run_writes_the_report_outputs(tiny_optimizer):
    df = run_optimization()
    written = {p.name for p in tiny_optimizer.iterdir()}
    assert {"optimization_curve.png", "selected_strategies.csv", "pareto_cost_curve.png",
            "pareto_front.png", "nsga2_results.csv", "Optimal_Action_Plan.csv",
            "cost_summary.csv", "simulation_diagnostics.csv", "validation_curve.png",
            "validation_data.csv"} <= written
    assert "pareto_cost_curve_data.csv" not in written   # its data is in nsga2_results.csv
    results = pd.read_csv(tiny_optimizer / "nsga2_results.csv")
    for col in leyp_optimizer.REEVALUATION_COLUMNS + ("Discount_Rate",):
        assert col in results.columns
    summary = pd.read_csv(tiny_optimizer / "selected_strategies.csv")
    knee = df.loc[select_strategy(df, "knee")]
    row = summary.set_index("strategy").loc["knee"]
    assert row["selected"] and float(row["annual_budget"]) == pytest.approx(knee["Budget"])


@pytest.mark.parametrize("rule", SELECTION_METHODS)
def test_selection_drives_the_action_plan(tiny_optimizer, tiny_config, monkeypatch, rule):
    """Changing `selection` changes which strategy the action plan is written for."""
    tiny_config["selection"] = rule
    planned = []
    real = leyp_optimizer.run_simulation

    def spy(*args, **kwargs):
        if kwargs.get("generate_report"):
            planned.append((kwargs["annual_budget"], kwargs["rehab_trigger"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(leyp_optimizer, "run_simulation", spy)
    df = run_optimization()
    best = df.loc[select_strategy(df, rule)]
    assert planned == [(best["Budget"], best["Rehab_Trigger"])]
    summary = pd.read_csv(tiny_optimizer / "selected_strategies.csv").set_index("strategy")
    assert summary.loc[rule, "selected"] and summary["selected"].sum() == 1


def test_unknown_selection_fails_before_the_search(tiny_optimizer, tiny_config):
    tiny_config["selection"] = "cheapest"
    with pytest.raises(ValueError, match="selection"):
        run_optimization()
    assert not tiny_optimizer.exists()


def test_plot_only_redraws_from_the_saved_front(tiny_optimizer, tiny_config):
    run_optimization()
    plan = (tiny_optimizer / "Optimal_Action_Plan.csv").read_bytes()
    results = (tiny_optimizer / "nsga2_results.csv").read_bytes()
    summary = (tiny_optimizer / "selected_strategies.csv").read_bytes()

    replot()                                             # same config: identical summary
    assert (tiny_optimizer / "selected_strategies.csv").read_bytes() == summary

    tiny_config["selection"] = "max_marginal_return"
    tiny_config["plateau_tolerance"] = 0.5
    replot()
    s = pd.read_csv(tiny_optimizer / "selected_strategies.csv").set_index("strategy")
    assert s.loc["max_marginal_return", "selected"] and not s.loc["knee", "selected"]
    assert s.index[-1] == "plateau (total within 50% of minimum)"
    # No search and no action plan: the front and the plan are untouched.
    assert (tiny_optimizer / "nsga2_results.csv").read_bytes() == results
    assert (tiny_optimizer / "Optimal_Action_Plan.csv").read_bytes() == plan


def test_plot_only_fills_in_columns_missing_from_an_older_front(tiny_optimizer):
    df = run_optimization()
    older = df[["Investment_Cost", "Risk_Cost", "Budget", "Rehab_Trigger", "Total_Cost"]]
    older.to_csv(tiny_optimizer / "nsga2_results.csv", index=False)
    replot()
    filled = pd.read_csv(tiny_optimizer / "nsga2_results.csv")
    assert filled["Nominal_Investment"].tolist() == pytest.approx(df["Nominal_Investment"].tolist())
    assert (filled["Discount_Rate"] == 0.03).all()


def test_plot_only_needs_a_saved_front(tiny_optimizer):
    with pytest.raises(FileNotFoundError, match="full optimisation"):
        replot()
