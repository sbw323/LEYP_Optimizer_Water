"""Phase 1 tests: break seeding must not condemn pipes, and backlog reporting."""

import numpy as np
import pandas as pd
import pytest

from leyp_config import COLUMN_MAP, SEGMENT_BREAK_THRESHOLD
from leyp_core import Pipe
from leyp_runner import run_simulation


# --- Item 6: no pipe may start the simulation already failed -------------------

@pytest.mark.parametrize("age", [50, 76, 96, 150, 400])
def test_no_seeded_segment_reaches_failure_threshold(pipe_attrs, age):
    """Seeded history is capped below the threshold at every age."""
    for draw in range(200):
        np.random.seed(draw)
        p = Pipe({**pipe_attrs, "Age": age})
        assert all(
            seg.n_point_breaks < SEGMENT_BREAK_THRESHOLD for seg in p.segments
        ), f"age {age}, draw {draw}: seeding condemned the pipe at t=0"


def test_no_pipe_in_the_real_inventory_is_condemned_at_t0():
    """Review finding A1: 40 of 388 pipes used to start already failed."""
    df = pd.read_csv("Louisa_wConduits_Input_CSV.csv")
    for seed in (1, 20260830, 999):
        np.random.seed(seed)
        network = [Pipe({k: r.get(k, None) for k in COLUMN_MAP}) for _, r in df.iterrows()]
        condemned = [
            p.id
            for p in network
            if any(s.n_point_breaks >= SEGMENT_BREAK_THRESHOLD for s in p.segments)
        ]
        assert condemned == [], f"seed {seed}: {len(condemned)} pipes condemned at t=0"


def test_seeding_still_produces_history_for_old_pipes(pipe_attrs):
    """The cap must not silence the LEYP feedback term it exists to drive."""
    totals = []
    for draw in range(100):
        np.random.seed(draw)
        totals.append(Pipe({**pipe_attrs, "Age": 96}).n_breaks)
    assert max(totals) > 0
    assert np.mean(totals) > 1.0


def test_new_pipes_are_seeded_with_no_history(pipe_attrs):
    p = Pipe({**pipe_attrs, "Age": 0})
    assert p.n_breaks == 0


def test_seeded_breaks_count_toward_the_failure_rule(pipe_attrs):
    """The rule is unchanged: a segment at the cap fails on its next break."""
    p = Pipe({**pipe_attrs, "Age": 96})
    p.segments[0].n_point_breaks = SEGMENT_BREAK_THRESHOLD - 1
    p.segments[0].simulate_breaks(1e6)  # force at least one break
    assert p.segments[0].has_failed(SEGMENT_BREAK_THRESHOLD)


# --- Item 7: inherited backlog reporting ---------------------------------------

@pytest.fixture
def backlog_run(inventory_csv, tmp_path):
    out = tmp_path / "results"
    run_simulation(
        override_input_path=inventory_csv,
        annual_budget=250000.0,
        rehab_trigger=2.5,
        output_dir=str(out),
        generate_report=True,
        seed=31,
    )
    return (
        pd.read_csv(out / "Optimal_Action_Plan.csv"),
        pd.read_csv(out / "cost_summary.csv").iloc[0],
        pd.read_csv(out / "simulation_diagnostics.csv"),
    )


def test_backlog_metrics_are_reported(backlog_run):
    _, summary, _ = backlog_run
    for key in (
        "Backlog_Pipes",
        "Backlog_Length_Ft",
        "Backlog_Share_Of_Network",
        "Backlog_Cleared_By_CIP",
        "Backlog_Cleared_By_Emergency",
        "Backlog_Never_Cleared",
    ):
        assert key in summary.index


def test_backlog_is_tagged_once_per_pipe(backlog_run):
    """A pipe can only clear the inherited backlog once, at its first replacement."""
    plan, _, _ = backlog_run
    tagged = plan[plan["Backlog"]]
    assert tagged["PipeID"].is_unique


def test_backlog_counts_reconcile(backlog_run):
    plan, summary, _ = backlog_run
    tagged = plan[plan["Backlog"]]
    assert len(tagged) == summary["Backlog_Cleared_By_CIP"] + summary["Backlog_Cleared_By_Emergency"]
    assert (
        summary["Backlog_Cleared_By_CIP"]
        + summary["Backlog_Cleared_By_Emergency"]
        + summary["Backlog_Never_Cleared"]
        == summary["Backlog_Pipes"]
    )


def test_backlog_remaining_is_non_increasing(backlog_run):
    """Backlog can only be worked off, never grow."""
    _, _, diag = backlog_run
    remaining = list(diag["Backlog_Remaining"])
    assert remaining == sorted(remaining, reverse=True)


def test_backlog_tagging_does_not_change_costs(inventory_csv):
    """Item 7 is reporting only — the returned objectives must be untouched."""
    kwargs = dict(
        override_input_path=inventory_csv,
        annual_budget=250000.0,
        rehab_trigger=2.5,
        seed=32,
    )
    plain = run_simulation(**kwargs)
    reported = run_simulation(**kwargs, generate_report=True)
    assert plain == reported[:2]


def test_backlog_is_bounded_by_the_network(backlog_run):
    _, summary, _ = backlog_run
    assert 0.0 <= summary["Backlog_Share_Of_Network"] <= 1.0
