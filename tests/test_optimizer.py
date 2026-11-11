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

    At $25/inch-ft this network costs about $17.6M to renew once. The check
    keeps the $84.4M it cost at the original $120/inch-ft, so the ceiling
    stays adequate if unit costs are raised again.
    """
    from leyp_config import SIMULATION_YEARS

    assert config["genes"]["budget"]["max"] * SIMULATION_YEARS >= 84_400_000


def test_replicates_are_averaged(config):
    assert config["simulation"]["n_replicates"] >= 3


def test_selection_rule_is_recognised(config):
    from leyp_optimizer import SELECTION_METHODS, selection_settings

    method, tol = selection_settings(config)
    assert method in SELECTION_METHODS and 0 <= tol < 0.5


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
    """The lowest swept budget whose total is within 0.1% of the minimum.

    Above the budget that funds all eligible work the total is flat (extra
    budget goes unspent), so several budgets tie; taking the start of that
    flat stretch keeps a tie from deciding the answer.
    """
    n_replicates = n_replicates or _configured_replicates()
    from leyp_runner import simulate

    totals = {}
    for b in _SWEEP:
        r = simulate("Louisa_wConduits_Input_CSV.csv", annual_budget=b, rehab_trigger=1.5,
                     seed=20260830, n_replicates=n_replicates)
        totals[b] = r["investment_cost"] + r["risk_cost"]
    floor = min(totals.values()) * 1.001
    return min(b for b, t in totals.items() if t <= floor)


def test_budget_optimum_is_interior():
    """The optimizer no longer drives the budget to a bound."""
    best = _min_cost_budget()
    assert best not in (_SWEEP[0], _SWEEP[-1]), f"optimum at a bound: ${best:,}"


def test_wearout_raises_the_funded_optimum(monkeypatch):
    """Wear-out is what makes a large capital programme worth funding.

    It concentrates risk in old pipes that CIP can reach by risk-per-dollar,
    so the cost-minimising budget is higher with it than without. (At $120
    per inch-ft the optimum went to a bound without wear-out; at $25 CIP is
    cheap enough that a small programme pays even then, so the finding is
    now that wear-out raises the optimum, not that it creates one.)
    """
    import leyp_core

    with_wearout = _min_cost_budget()
    monkeypatch.setattr(leyp_core, "WEAROUT_MAX_MULTIPLIER", 1.0)
    without = _min_cost_budget()
    assert with_wearout > without, f"wear-out ${with_wearout:,} vs none ${without:,}"
