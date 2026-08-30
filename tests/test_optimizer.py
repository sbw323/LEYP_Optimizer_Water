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
