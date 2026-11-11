"""Per-strategy action plan export and the discount-rate sensitivity sweep.

Entry points run a seconds-long search on the three-pipe inventory
(conftest's tiny_config / tiny_optimizer); formatting and validation are
checked on synthetic inputs.
"""

import pandas as pd
import pytest

import leyp_optimizer
from leyp_optimizer import (
    PLAN_FILES,
    SELECTION_METHODS,
    _parse_args,
    export_strategy_plans,
    replot,
    run_optimization,
    run_sensitivity_only,
    select_strategy,
    sensitivity_settings,
    sensitivity_table,
)
from leyp_runner import representative_replicate, simulate


def _count_searches(monkeypatch):
    """Record the discount rate and checkpoint file of every NSGA-II search."""
    calls = []
    real = leyp_optimizer.run_search

    def spy(config, input_path, discount_rate=None, checkpoint_path=None, **kw):
        rate = discount_rate if discount_rate is not None else config.get("discount_rate")
        calls.append((rate, checkpoint_path))
        if checkpoint_path is None:
            return real(config, input_path, discount_rate)
        return real(config, input_path, discount_rate, checkpoint_path)

    monkeypatch.setattr(leyp_optimizer, "run_search", spy)
    return calls


# --- Strategy plan export -----------------------------------------------------------


def test_full_run_exports_one_plan_per_strategy(tiny_optimizer):
    df = run_optimization()
    for name in PLAN_FILES.values():
        assert (tiny_optimizer / name).exists(), name
    # The selected strategy's export is the action plan itself.
    selected = (tiny_optimizer / PLAN_FILES["knee"]).read_bytes()
    assert selected == (tiny_optimizer / "Optimal_Action_Plan.csv").read_bytes()
    summary = pd.read_csv(tiny_optimizer / "selected_strategies.csv").set_index("strategy")
    for rule in SELECTION_METHODS:
        assert summary.loc[rule, "plan_file"] == PLAN_FILES[rule]
    assert len(df) > 0


def test_each_exported_plan_is_its_strategys_simulation(tiny_optimizer, tiny_config):
    df = run_optimization()
    sim = tiny_config["simulation"]
    for rule in SELECTION_METHODS:
        row = df.loc[select_strategy(df, rule)]
        r = simulate("temp_optimization_input.csv", annual_budget=row["Budget"],
                     rehab_trigger=row["Rehab_Trigger"], seed=sim["seed"],
                     n_replicates=sim["n_replicates"], discount_rate=0.03)
        rep = representative_replicate(r["replicates"])
        plan = pd.read_csv(tiny_optimizer / PLAN_FILES[rule])
        cip = plan.loc[plan["Action"] == "CIP_Replacement", "Cost"].sum() if len(plan) else 0.0
        assert cip == pytest.approx(rep["cip_cost"])


def test_export_can_be_switched_off(tiny_optimizer, tiny_config):
    tiny_config["export_strategy_plans"] = False
    run_optimization()
    written = {p.name for p in tiny_optimizer.iterdir()}
    assert not written & set(PLAN_FILES.values())
    assert "plan_file" not in pd.read_csv(tiny_optimizer / "selected_strategies.csv").columns


def test_plot_only_exports_without_touching_the_run(tiny_optimizer):
    run_optimization()
    before = {name: (tiny_optimizer / name).read_bytes() for name in PLAN_FILES.values()}
    plan = (tiny_optimizer / "Optimal_Action_Plan.csv").read_bytes()
    results = (tiny_optimizer / "nsga2_results.csv").read_bytes()
    for name in PLAN_FILES.values():
        (tiny_optimizer / name).unlink()

    replot()
    for name, content in before.items():
        assert (tiny_optimizer / name).read_bytes() == content
    assert (tiny_optimizer / "Optimal_Action_Plan.csv").read_bytes() == plan
    assert (tiny_optimizer / "nsga2_results.csv").read_bytes() == results


def test_rules_that_coincide_still_get_three_files(inventory_csv, tmp_path):
    """A one-point front: every rule picks it, every file is written."""
    df = pd.DataFrame({"Investment_Cost": [1e5], "Risk_Cost": [2e5], "Budget": [50_000.0],
                       "Rehab_Trigger": [2.0]}).assign(Total_Cost=3e5)
    config = {"simulation": {"seed": 3, "n_replicates": 1}, "discount_rate": 0.03}
    written = export_strategy_plans(df, str(tmp_path), config, inventory_csv)
    assert set(written) == set(SELECTION_METHODS)
    contents = {open(path).read() for path in written.values()}
    assert len(contents) == 1


# --- Sensitivity settings and table -----------------------------------------------------


def test_sensitivity_defaults():
    s = sensitivity_settings({})
    assert s == {"enabled": False, "discount_rates": [0.0, 0.03, 0.05], "strategy": "knee"}


def test_sensitivity_strategy_is_independent_of_selection():
    s = sensitivity_settings({"selection": "min_total_cost"})
    assert s["strategy"] == "knee"


@pytest.mark.parametrize("block, match", [
    ({"strategy": "cheapest"}, "strategy"),
    ({"discount_rates": []}, "non-empty"),
    ({"discount_rates": [-0.01, 0.03]}, r"\[0, 1\)"),
    ({"discount_rates": [0.03, 0.03]}, "duplicates"),
])
def test_bad_sensitivity_blocks_are_refused(block, match):
    with pytest.raises(ValueError, match=match):
        sensitivity_settings({"sensitivity": block})


def _pick(rate, budget, trigger, spend, failures, ceiling=False):
    return {"rate": rate, "annual_budget": budget, "rehab_trigger": trigger,
            "avg_annual_spend": spend, "failures": failures, "at_budget_ceiling": ceiling}


def test_sensitivity_table_layout():
    picks = [_pick(0.0, 1_990_000, 1.80, 296_400, 9.5, ceiling=True),
             _pick(0.03, 178_443, 1.2533, 143_872, 51.625),
             _pick(0.05, 120_000, 1.10, 98_000, 70.2)]
    t = sensitivity_table(picks, "knee")
    assert list(t.columns) == ["Nominal", "3%", "5%"]
    assert list(t.index) == ["Recommended avg spend (knee pick)",
                             "Failures (breaks) over 100 yr at the knee pick",
                             "Strategy type at the knee pick"]
    assert t.loc["Recommended avg spend (knee pick)"].tolist() == ["$296k/yr", "$144k/yr", "$98k/yr"]
    assert t.loc["Failures (breaks) over 100 yr at the knee pick"].tolist() == ["10", "52", "70"]
    strategy = t.loc["Strategy type at the knee pick"]
    assert strategy["3%"] == "$178k/yr budget, replace at condition ≤ 1.25"
    assert strategy["Nominal"] == "$1.99M/yr budget, replace at condition ≤ 1.80, at the budget ceiling"


def test_sensitivity_rows_name_the_strategy():
    t = sensitivity_table([_pick(0.03, 1e5, 1.5, 1e5, 10)], "min_total_cost")
    assert t.index[0] == "Recommended avg spend (min-total pick)"


# --- The sweep --------------------------------------------------------------------------


def test_full_run_with_the_sweep_reuses_its_own_front(tiny_optimizer, tiny_config, monkeypatch):
    tiny_config["sensitivity"] = {"enabled": True}
    calls = _count_searches(monkeypatch)
    df = run_optimization()

    rates = [rate for rate, _ in calls]
    assert rates == [0.03, 0.0, 0.05]                 # main run, then only the other two
    table = pd.read_csv(tiny_optimizer / "discount_sensitivity.csv", index_col="metric")
    assert list(table.columns) == ["Nominal", "3%", "5%"]
    knee = df.loc[select_strategy(df, "knee")]
    detail = pd.read_csv(tiny_optimizer / "discount_sensitivity_detail.csv").set_index("rate_label")
    assert detail.loc["3%", "source"] == "reused"
    assert detail.loc["3%", "annual_budget"] == pytest.approx(knee["Budget"])
    assert detail.loc["3%", "avg_annual_spend"] == pytest.approx(knee["Avg_Annual_Spend"])
    for tag in ("0pct", "3pct", "5pct"):
        assert (tiny_optimizer / "sensitivity" / f"nsga2_results_rate_{tag}.csv").exists()


def test_each_rate_checkpoints_to_its_own_file(tiny_optimizer, tiny_config, monkeypatch):
    tiny_config["sensitivity"] = {"enabled": True}
    calls = _count_searches(monkeypatch)
    run_optimization()
    paths = [path for _, path in calls[1:]]
    assert paths == ["nsga2_checkpoint_rate_0pct.pkl", "nsga2_checkpoint_rate_5pct.pkl"]
    assert not list(tiny_optimizer.parent.glob("nsga2_checkpoint*.pkl"))   # cleaned up


def test_sweep_strategy_follows_its_own_setting(tiny_optimizer, tiny_config):
    tiny_config["sensitivity"] = {"enabled": True, "strategy": "min_total_cost"}
    df = run_optimization()
    detail = pd.read_csv(tiny_optimizer / "discount_sensitivity_detail.csv").set_index("rate_label")
    best = df.loc[select_strategy(df, "min_total_cost")]
    assert detail.loc["3%", "annual_budget"] == pytest.approx(best["Budget"])
    table = pd.read_csv(tiny_optimizer / "discount_sensitivity.csv", index_col="metric")
    assert table.index[0] == "Recommended avg spend (min-total pick)"


def test_bad_sweep_settings_fail_before_the_search(tiny_optimizer, tiny_config):
    tiny_config["sensitivity"] = {"enabled": False, "strategy": "cheapest"}
    with pytest.raises(ValueError, match="sensitivity.strategy"):
        run_optimization()
    assert not tiny_optimizer.exists()


def test_sweep_is_off_by_default(tiny_optimizer):
    run_optimization()
    assert not (tiny_optimizer / "discount_sensitivity.csv").exists()


def test_sensitivity_only_reuses_a_reproducible_saved_front(tiny_optimizer, monkeypatch):
    run_optimization()
    plan = (tiny_optimizer / "Optimal_Action_Plan.csv").read_bytes()
    chart = (tiny_optimizer / "optimization_curve.png").read_bytes()
    calls = _count_searches(monkeypatch)

    result = run_sensitivity_only()
    assert [rate for rate, _ in calls] == [0.0, 0.05]
    assert list(result["table"].columns) == ["Nominal", "3%", "5%"]
    assert result["detail"].set_index("rate_label").loc["3%", "source"] == "reused"
    # No action plan and no charts.
    assert (tiny_optimizer / "Optimal_Action_Plan.csv").read_bytes() == plan
    assert (tiny_optimizer / "optimization_curve.png").read_bytes() == chart


def test_sensitivity_only_searches_every_rate_for_a_stale_front(tiny_optimizer, monkeypatch):
    run_optimization()
    path = tiny_optimizer / "nsga2_results.csv"
    stale = pd.read_csv(path)
    stale["Investment_Cost"] *= 1.1
    stale.to_csv(path, index=False)
    calls = _count_searches(monkeypatch)
    run_sensitivity_only()
    assert [rate for rate, _ in calls] == [0.0, 0.03, 0.05]


def test_sensitivity_only_without_a_saved_front(tiny_optimizer, monkeypatch):
    calls = _count_searches(monkeypatch)
    run_sensitivity_only()
    assert [rate for rate, _ in calls] == [0.0, 0.03, 0.05]
    assert (tiny_optimizer / "discount_sensitivity.csv").exists()
    assert not (tiny_optimizer / "Optimal_Action_Plan.csv").exists()


def test_cli_sensitivity_flag():
    assert _parse_args(["--sensitivity-only"]).sensitivity_only is True
    with pytest.raises(SystemExit):
        _parse_args(["--sensitivity-only", "--plot-only"])
