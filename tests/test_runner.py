"""Runner tests: the Phase 0 reproducibility gate, replicates, and diagnostics."""

import os

import numpy as np
import pandas as pd
import pytest

from leyp_runner import EMERGENCY_CAUSES, run_simulation

BUDGET = 250000.0
TRIGGER = 2.5


# --- Reproducibility (Phase 0 gate) -------------------------------------------

def test_same_seed_reproduces_to_the_dollar(inventory_csv):
    """The Phase 0 acceptance gate: identical genes and seed, identical costs."""
    kwargs = dict(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        seed=99,
    )
    first = run_simulation(**kwargs)
    second = run_simulation(**kwargs)
    assert first == second


def test_seed_survives_intervening_random_draws(inventory_csv):
    """Seeding must not depend on the caller's global stream position."""
    kwargs = dict(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        seed=7,
    )
    first = run_simulation(**kwargs)
    np.random.random(1000)  # disturb the global stream
    assert run_simulation(**kwargs) == first


def test_different_seeds_give_different_draws(inventory_csv):
    kwargs = dict(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
    )
    assert run_simulation(**kwargs, seed=1) != run_simulation(**kwargs, seed=2)


def test_detailed_return_is_reproducible(inventory_csv):
    kwargs = dict(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        generate_report=True,
        seed=5,
    )
    assert run_simulation(**kwargs) == run_simulation(**kwargs)


# --- Replicates ---------------------------------------------------------------

def test_replicates_average_is_reproducible(inventory_csv):
    kwargs = dict(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        seed=11,
        n_replicates=4,
    )
    assert run_simulation(**kwargs) == run_simulation(**kwargs)


def test_single_replicate_matches_first_of_many(inventory_csv):
    """Replicate i uses seed+i, so a 1-replicate run equals the first draw."""
    common = dict(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        seed=3,
    )
    single = run_simulation(**common, n_replicates=1)
    manual = [
        run_simulation(
            override_input_path=inventory_csv,
            annual_budget=BUDGET,
            rehab_trigger=TRIGGER,
            seed=3 + i,
        )
        for i in range(3)
    ]
    assert single == manual[0]

    averaged = run_simulation(**common, n_replicates=3)
    assert averaged[0] == pytest.approx(float(np.mean([m[0] for m in manual])))
    assert averaged[1] == pytest.approx(float(np.mean([m[1] for m in manual])))


def test_replicates_reduce_spread(inventory_csv):
    """Averaging must move estimates closer together than single draws do."""
    def spread(n_replicates):
        totals = [
            sum(
                run_simulation(
                    override_input_path=inventory_csv,
                    annual_budget=BUDGET,
                    rehab_trigger=TRIGGER,
                    seed=1000 + k * 50,
                    n_replicates=n_replicates,
                )
            )
            for k in range(6)
        ]
        return float(np.std(totals))

    assert spread(8) < spread(1)


def test_invalid_replicate_count_rejected(inventory_csv):
    with pytest.raises(ValueError):
        run_simulation(override_input_path=inventory_csv, n_replicates=0)


# --- Reports and diagnostics --------------------------------------------------

def test_reports_are_written(inventory_csv, tmp_path):
    out = tmp_path / "results"
    run_simulation(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        output_dir=str(out),
        generate_report=True,
        seed=21,
    )
    for name in ("Optimal_Action_Plan.csv", "cost_summary.csv", "simulation_diagnostics.csv"):
        assert (out / name).exists(), f"missing {name}"


def test_diagnostics_cover_every_year(inventory_csv, tmp_path):
    from leyp_config import SIMULATION_YEARS

    out = tmp_path / "results"
    run_simulation(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        output_dir=str(out),
        generate_report=True,
        seed=22,
    )
    diag = pd.read_csv(out / "simulation_diagnostics.csv")
    assert len(diag) == SIMULATION_YEARS
    assert list(diag["Year"]) == list(range(1, SIMULATION_YEARS + 1))
    for cause in EMERGENCY_CAUSES:
        assert f"Emergency_{cause}" in diag.columns


def test_diagnostics_reconcile_with_cost_summary(inventory_csv, tmp_path):
    """Per-year diagnostics must sum to the reported totals."""
    out = tmp_path / "results"
    run_simulation(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        output_dir=str(out),
        generate_report=True,
        seed=23,
    )
    diag = pd.read_csv(out / "simulation_diagnostics.csv")
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]

    assert diag["CIP_Spend"].sum() == pytest.approx(summary["CIP_Cost"])
    assert diag["Emergency_Cost"].sum() == pytest.approx(summary["Emergency_Cost"])
    assert diag["Repair_Cost"].sum() == pytest.approx(summary["Repair_Cost"])
    assert diag["Breaks"].sum() == summary["Total_Breaks"]
    assert diag["Emergency_Count"].sum() == summary["Emergency_Count"]

    cause_total = sum(diag[f"Emergency_{c}"].sum() for c in EMERGENCY_CAUSES)
    assert cause_total == summary["Emergency_Count"]


def test_budget_utilization_never_exceeds_one(inventory_csv, tmp_path):
    out = tmp_path / "results"
    run_simulation(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        output_dir=str(out),
        generate_report=True,
        seed=24,
    )
    diag = pd.read_csv(out / "simulation_diagnostics.csv")
    assert (diag["Budget_Utilization"] <= 1.0 + 1e-9).all()
    assert (diag["CIP_Spend"] <= BUDGET + 1e-6).all()


def test_every_emergency_action_carries_a_cause(inventory_csv, tmp_path):
    out = tmp_path / "results"
    run_simulation(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        output_dir=str(out),
        generate_report=True,
        seed=25,
    )
    plan = pd.read_csv(out / "Optimal_Action_Plan.csv")
    emergencies = plan[plan["Action"] == "Emergency_Replacement"]
    assert not emergencies.empty
    assert emergencies["Cause"].isin(EMERGENCY_CAUSES).all()


def test_missing_input_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_simulation(override_input_path=str(tmp_path / "nope.csv"))


# --- Phase 2: CIP precedes the emergency stream --------------------------------

def _aged_out_pipe(pipe_attrs):
    """A pipe one degradation step away from the failure floor."""
    from leyp_core import Pipe

    p = Pipe({**pipe_attrs, "Age": 96})
    p.current_condition = 1.0005
    return p


def test_cip_captures_age_out_before_emergency(pipe_attrs, tmp_path, monkeypatch):
    """A funded age-out pipe must be renewed by CIP, not emergency-replaced.

    Review finding B3: the annual loop used to emergency-replace pipes the
    moment degradation crossed the floor, before CIP ever saw them.
    """
    import leyp_runner
    from water_replacement import ReplacementManager

    seen = {"cip": 0, "emergency": 0}
    real_run_year = ReplacementManager.run_year

    def counting_run_year(self, network, year):
        report = real_run_year(self, network, year)
        seen["cip"] += report["Count"]
        return report

    monkeypatch.setattr(ReplacementManager, "run_year", counting_run_year)

    out = tmp_path / "r"
    run_simulation(
        override_input_path=str(_write_aging_inventory(tmp_path)),
        annual_budget=1e9,          # never the binding constraint
        rehab_trigger=3.0,
        output_dir=str(out),
        generate_report=True,
        seed=41,
    )
    plan = pd.read_csv(out / "Optimal_Action_Plan.csv")
    emergencies = plan[plan["Action"] == "Emergency_Replacement"]
    # "Cause" only exists once an emergency has been logged at all.
    aged_out = (
        emergencies[emergencies["Cause"] == "degradation"]
        if "Cause" in plan.columns
        else emergencies.iloc[0:0]
    )
    assert seen["cip"] > 0
    assert aged_out.empty, (
        "with unlimited budget no pipe should age out into the emergency stream"
    )


def _write_aging_inventory(tmp_path):
    df = pd.DataFrame(
        [
            {"PipeID": f"P{i}", "Material": "AC", "Age": 90, "Length": 100.0,
             "Diameter": 6, "Condition": 1, "CoF_Value": 2}
            for i in range(15)
        ]
    )
    path = tmp_path / "aging.csv"
    df.to_csv(path, index=False)
    return path


def test_budget_is_deployed_when_work_is_available(tmp_path):
    """The Phase 2 gate: a constrained budget must actually get spent.

    Uses the real inventory, where renewal demand always exceeds the budget.
    A budget larger than the available work would idle legitimately, which is
    not what this gate is about.
    """
    out = tmp_path / "r"
    run_simulation(
        override_input_path="Louisa_wConduits_Input_CSV.csv",
        annual_budget=399046.84,
        rehab_trigger=2.8,
        output_dir=str(out),
        generate_report=True,
        seed=42,
    )
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    assert summary["Mean_Budget_Utilization"] > 0.90
    assert summary["Zero_Spend_Years"] == 0


def test_idle_budget_is_not_forced_to_spend(tmp_path):
    """A budget exceeding available work should idle, not manufacture work."""
    out = tmp_path / "r"
    run_simulation(
        override_input_path=str(_write_aging_inventory(tmp_path)),
        annual_budget=1e7,
        rehab_trigger=3.0,
        output_dir=str(out),
        generate_report=True,
        seed=46,
    )
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    assert summary["Mean_Budget_Utilization"] < 0.5


def test_cip_outnumbers_emergency_when_funded(tmp_path):
    """Acceptance criterion: planned work should dominate reactive work."""
    out = tmp_path / "r"
    run_simulation(
        override_input_path="Louisa_wConduits_Input_CSV.csv",
        annual_budget=399046.84,
        rehab_trigger=2.8,
        output_dir=str(out),
        generate_report=True,
        seed=43,
    )
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    assert summary["CIP_Count"] > summary["Emergency_Count"]


def test_unfundable_pipes_surface_in_diagnostics(tmp_path):
    """Pipes that no single year can afford must be visible, not silent."""
    df = pd.DataFrame(
        [{"PipeID": "BIG", "Material": "AC", "Age": 90, "Length": 20000.0,
          "Diameter": 12, "Condition": 1, "CoF_Value": 3}]
    )
    path = tmp_path / "big.csv"
    df.to_csv(path, index=False)

    out = tmp_path / "r"
    run_simulation(
        override_input_path=str(path),
        annual_budget=50000.0,
        rehab_trigger=3.0,
        output_dir=str(out),
        generate_report=True,
        seed=44,
    )
    diag = pd.read_csv(out / "simulation_diagnostics.csv")
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    assert diag["Unfundable"].max() >= 1
    assert summary["Mean_Unfundable_Per_Year"] > 0
    assert summary["CIP_Count"] == 0


def test_absolute_emergency_counts_are_reported(inventory_csv, tmp_path):
    """The year 1-2 ratio must be readable alongside its absolute counts."""
    out = tmp_path / "r"
    run_simulation(
        override_input_path=inventory_csv,
        annual_budget=BUDGET,
        rehab_trigger=TRIGGER,
        output_dir=str(out),
        generate_report=True,
        seed=45,
    )
    diag = pd.read_csv(out / "simulation_diagnostics.csv")
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    assert summary["Yr1_2_Emergency_Count"] == diag.head(2)["Emergency_Count"].sum()
    assert summary["Max_Annual_Emergency_Count"] == diag["Emergency_Count"].max()
