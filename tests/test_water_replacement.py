"""Replacement engine tests: cost, risk ranking, eligibility, budget behaviour."""

import pytest

from leyp_config import (
    ACTION_CIP_REPLACEMENT,
    CIP_REPLACEMENT_COST_PER_INCH_FT,
    DEFAULT_REPLACEMENT_MATERIAL,
    EMERGENCY_REPLACEMENT_COST_PER_FT,
)
from leyp_core import Pipe
from water_replacement import ReplacementManager


def make_pipe(pipe_attrs, **overrides):
    p = Pipe({**pipe_attrs, **overrides})
    p.current_condition = overrides.pop("condition", 2.0)
    return p


# --- Cost ---------------------------------------------------------------------

def test_calculate_cost_is_rate_times_diameter_times_length(pipe_attrs):
    p = Pipe({**pipe_attrs, "Diameter": 8.0, "Length": 250.0})
    mgr = ReplacementManager(budget=1e9)
    assert mgr.calculate_cost(p) == CIP_REPLACEMENT_COST_PER_INCH_FT * 8.0 * 250.0


def test_cost_rate_override_is_honoured(pipe):
    mgr = ReplacementManager(budget=1e9, cip_cost_rate=10.0)
    assert mgr.calculate_cost(pipe) == 10.0 * pipe.diameter * pipe.length


# --- Risk ranking -------------------------------------------------------------

def test_annualized_risk_scales_with_consequence(pipe_attrs):
    mgr = ReplacementManager(budget=1e9)
    low = Pipe({**pipe_attrs, "CoF_Value": 1.0})
    high = Pipe({**pipe_attrs, "CoF_Value": 3.0})
    high.degradation_rate = low.degradation_rate
    high.current_condition = low.current_condition
    assert mgr.get_annualized_risk(high) > mgr.get_annualized_risk(low)


def test_annualized_risk_uses_replacement_cost_per_ft(pipe):
    mgr = ReplacementManager(budget=1e9)
    expected = (pipe.length * pipe.cof * EMERGENCY_REPLACEMENT_COST_PER_FT) / max(
        0.1, pipe.current_ttf
    )
    assert mgr.get_annualized_risk(pipe) == pytest.approx(expected)


# --- Eligibility --------------------------------------------------------------

def test_pipe_above_trigger_is_not_replaced(pipe_attrs):
    p = Pipe(pipe_attrs)
    p.current_condition = 5.0
    mgr = ReplacementManager(budget=1e9, rehab_trigger=2.0)
    assert mgr.run_year([p], 1)["Count"] == 0


def test_pipe_at_or_below_trigger_is_replaced(pipe_attrs):
    p = Pipe(pipe_attrs)
    p.current_condition = 1.8
    mgr = ReplacementManager(budget=1e9, rehab_trigger=2.0)
    assert mgr.run_year([p], 1)["Count"] == 1


def test_failed_pipes_are_excluded_from_cip(pipe_attrs):
    """Pipes at the failure floor fall outside the CIP eligibility window."""
    p = Pipe(pipe_attrs)
    p.current_condition = 1.0
    mgr = ReplacementManager(budget=1e9, rehab_trigger=3.0)
    assert mgr.run_year([p], 1)["Count"] == 0


# --- Replacement effects ------------------------------------------------------

def test_replacement_resets_pipe_state(pipe_attrs):
    p = Pipe(pipe_attrs)
    p.current_condition = 1.5
    mgr = ReplacementManager(budget=1e9, rehab_trigger=2.0)
    mgr.run_year([p], 7)

    assert p.current_condition == 6.0
    assert p.material == DEFAULT_REPLACEMENT_MATERIAL
    assert p.initial_age == -7
    assert p.n_breaks == 0
    assert all(seg.n_point_breaks == 0 for seg in p.segments)


def test_replacement_is_logged_with_expected_fields(pipe_attrs):
    p = Pipe(pipe_attrs)
    p.current_condition = 1.5
    mgr = ReplacementManager(budget=1e9, rehab_trigger=2.0)
    mgr.run_year([p], 3)

    assert len(mgr.action_log) == 1
    entry = mgr.action_log[0]
    assert entry["Action"] == ACTION_CIP_REPLACEMENT
    assert entry["Year"] == 3
    assert entry["PostCondition"] == 6.0
    assert entry["Cost"] == mgr.calculate_cost(p)


# --- Budget -------------------------------------------------------------------

def _cheap_network(pipe_attrs, n, length=10.0):
    net = []
    for i in range(n):
        p = Pipe({**pipe_attrs, "PipeID": f"C{i}", "Length": length})
        p.current_condition = 2.0
        net.append(p)
    return net


def test_spend_never_exceeds_budget(pipe_attrs):
    """Invariant that must hold regardless of how the loop is structured."""
    net = _cheap_network(pipe_attrs, 20)
    unit = CIP_REPLACEMENT_COST_PER_INCH_FT * 6.0 * 10.0
    mgr = ReplacementManager(budget=unit * 5.5, rehab_trigger=2.5)
    report = mgr.run_year(net, 1)
    assert report["Spend"] <= mgr.budget
    assert report["Count"] == 5


def test_zero_budget_replaces_nothing(pipe_attrs):
    net = _cheap_network(pipe_attrs, 5)
    report = ReplacementManager(budget=0.0, rehab_trigger=3.0).run_year(net, 1)
    assert report["Count"] == 0
    assert report["Spend"] == 0.0


@pytest.mark.xfail(
    reason="Review finding B1 (Phase 2): run_year breaks out of the budget loop "
    "on the first unaffordable pipe instead of skipping it, stranding budget "
    "that cheaper eligible pipes could have used.",
    strict=True,
)
def test_unaffordable_pipe_does_not_strand_the_remaining_budget(pipe_attrs):
    """One oversized pipe at the top of the ranking must not end the year.

    The expensive pipe ranks first (risk scales with length) and cannot be
    afforded; the six cheap pipes behind it can be. A correct engine spends
    the budget on them.
    """
    huge = Pipe({**pipe_attrs, "PipeID": "HUGE", "Length": 100000.0})
    huge.current_condition = 2.0
    net = [huge] + _cheap_network(pipe_attrs, 6)

    unit = CIP_REPLACEMENT_COST_PER_INCH_FT * 6.0 * 10.0
    mgr = ReplacementManager(budget=unit * 6, rehab_trigger=2.5)
    report = mgr.run_year(net, 1)

    assert report["Count"] == 6
