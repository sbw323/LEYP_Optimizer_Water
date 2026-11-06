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


def test_every_emergency_action_carries_a_cause(tmp_path):
    """Uses the real inventory: with no forced floor replacement a small
    synthetic network at a generous budget produces no emergencies at all."""
    out = tmp_path / "results"
    run_simulation(
        override_input_path="Louisa_wConduits_Input_CSV.csv",
        annual_budget=500_000.0,
        rehab_trigger=1.5,
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


def test_no_emergency_replacement_without_a_segment_failure(tmp_path):
    """Every emergency row must coincide with a break on that pipe that year.

    Seeded history is capped below the threshold, so a segment can only reach
    it through a new break in the year it fails. An emergency row with no
    same-year Break_Event would mean something other than the segment rule
    triggered it.
    """
    out = tmp_path / "r"
    run_simulation(
        override_input_path="Louisa_wConduits_Input_CSV.csv",
        annual_budget=500_000.0,
        rehab_trigger=1.5,
        output_dir=str(out),
        generate_report=True,
        seed=41,
    )
    plan = pd.read_csv(out / "Optimal_Action_Plan.csv")
    em = plan[plan["Action"] == "Emergency_Replacement"]
    br = plan[plan["Action"] == "Break_Event"][["PipeID", "Year"]]
    assert not em.empty
    assert (em["Cause"] == "break_failure").all()
    merged = em.merge(br, on=["PipeID", "Year"], how="left", indicator=True)
    assert (merged["_merge"] == "both").all(), "emergency row without a same-year break"


def test_floor_pipe_is_not_replaced_without_a_break(tmp_path, monkeypatch):
    """A worn-out pipe that has not broken stays in service (plan 3A).

    Hazard is silenced so no break can occur; the pipe decays to the floor
    and must sit there, unreplaced, for the whole horizon.
    """
    import leyp_core

    monkeypatch.setattr(leyp_core, "HAZARD_LENGTH_SCALE", 1e12)
    df = pd.DataFrame(
        [{"PipeID": "F", "Material": "GI", "Age": 96, "Length": 100.0,
          "Diameter": 6, "Condition": 1, "CoF_Value": 1}]
    )
    path = tmp_path / "floor.csv"
    df.to_csv(path, index=False)

    out = tmp_path / "r"
    run_simulation(
        override_input_path=str(path),
        annual_budget=0.0,
        rehab_trigger=1.5,
        output_dir=str(out),
        generate_report=True,
        seed=71,
    )
    plan = pd.read_csv(out / "Optimal_Action_Plan.csv") if (out / "Optimal_Action_Plan.csv").exists() else pd.DataFrame(columns=["Action"])
    diag = pd.read_csv(out / "simulation_diagnostics.csv")
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    assert not (plan["Action"] == "Emergency_Replacement").any()
    assert diag["Pipes_At_Floor"].iloc[-1] == 1
    assert summary["Floor_Pipe_Years"] > 0
    assert summary["Emergency_Count"] == 0


def test_floor_dwell_is_reported(inventory_csv, tmp_path):
    out = tmp_path / "r"
    run_simulation(
        override_input_path=inventory_csv, annual_budget=BUDGET, rehab_trigger=TRIGGER,
        output_dir=str(out), generate_report=True, seed=72,
    )
    diag = pd.read_csv(out / "simulation_diagnostics.csv")
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    for c in ("Pipes_At_Floor", "Floor_Pipes_Below_Onset"):
        assert c in diag.columns
    assert diag["Pipes_At_Floor"].sum() == summary["Floor_Pipe_Years"]
    assert diag["Pipes_At_Floor"].max() == summary["Max_Pipes_At_Floor"]
    assert diag["Floor_Pipes_Below_Onset"].sum() == summary["Floor_Pipe_Years_Below_Onset"]
    assert (diag["Floor_Pipes_Below_Onset"] <= diag["Pipes_At_Floor"]).all()


def test_emergencies_fall_on_old_pipes(tmp_path):
    """The pipes that fail should, on average, be past their average life."""
    out = tmp_path / "r"
    run_simulation(
        override_input_path="Louisa_wConduits_Input_CSV.csv",
        annual_budget=500_000.0, rehab_trigger=1.5,
        output_dir=str(out), generate_report=True, seed=73, n_replicates=2,
    )
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    assert summary["Mean_Life_Ratio_At_Emergency"] > 1.0


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


# --- CLI reporting ---------------------------------------------------------------

def test_cli_undiscounted_block_is_consistent_with_replicates(inventory_csv, tmp_path):
    """The CLI's undiscounted streams must be non-negative and sum to its total.

    Regression: with --replicates > 1 the CLI subtracted a replicate-mean
    emergency cost from a single replicate's risk total, printing a negative
    repair cost.
    """
    import re
    import subprocess
    import sys

    out = tmp_path / "cli"
    proc = subprocess.run(
        [sys.executable, "leyp_runner.py", "--input", inventory_csv,
         "--budget", "250000", "--trigger", "2.5", "--seed", "9",
         "--replicates", "4", "--output-dir", str(out)],
        capture_output=True, text=True, check=True,
    )
    block = proc.stdout.split("Undiscounted cash", 1)[1]

    def amount(label):
        m = re.search(rf"{label}:\s+\$\s*(-?[\d,]+)", block)
        assert m, f"missing {label!r} in CLI output"
        return float(m.group(1).replace(",", ""))

    parts = [amount(k) for k in ("Investment \\(CIP\\)", "Emergency repairs", "Emergency replacements")]
    assert all(v >= 0 for v in parts), parts
    assert sum(parts) == pytest.approx(amount("Total"), abs=1.0)

    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    assert amount("Emergency repairs") == pytest.approx(summary["Repair_Cost"], abs=1.0)
