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
