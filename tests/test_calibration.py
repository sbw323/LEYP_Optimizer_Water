"""Phase 4 tests: hazard stability, feedback saturation, and break calibration."""

import pytest

from leyp_config import (
    ALPHA,
    BREAK_CONDITION_PENALTY,
    BREAK_DAMAGE_CONDITION_FLOOR,
    HAZARD_LENGTH_SCALE,
    LEYP_BREAK_FEEDBACK_CAP,
    SEGMENT_BREAK_THRESHOLD,
)
from leyp_core import Pipe
from leyp_runner import run_simulation

NETWORK_MILES = 21.74
REAL_INVENTORY = "Louisa_wConduits_Input_CSV.csv"


# --- Saturating LEYP feedback --------------------------------------------------

def test_feedback_saturates_at_the_cap(pipe):
    """Beyond the cap, additional breaks must not raise hazard further."""
    pipe.n_breaks = LEYP_BREAK_FEEDBACK_CAP
    at_cap = pipe.calculate_hazard(10)
    pipe.n_breaks = LEYP_BREAK_FEEDBACK_CAP * 10
    assert pipe.calculate_hazard(10) == pytest.approx(at_cap)


def test_feedback_still_applies_below_the_cap(pipe):
    """Saturation must not disable the LEYP mechanism it bounds."""
    pipe.n_breaks = 0
    base = pipe.calculate_hazard(10)
    pipe.n_breaks = LEYP_BREAK_FEEDBACK_CAP - 1
    scaled = pipe.calculate_hazard(10)
    assert scaled == pytest.approx(base * (1 + ALPHA * (LEYP_BREAK_FEEDBACK_CAP - 1)))
    assert scaled > base


def test_feedback_multiplier_is_bounded(pipe):
    """The hazard multiplier from break history has a finite ceiling."""
    pipe.n_breaks = 0
    base = pipe.calculate_hazard(10)
    pipe.n_breaks = 10_000
    ceiling = 1 + ALPHA * LEYP_BREAK_FEEDBACK_CAP
    assert pipe.calculate_hazard(10) <= base * ceiling + 1e-9


# --- Break damage is not an independent failure path ---------------------------

def test_break_damage_cannot_condemn_a_pipe(pipe):
    """Damage floors above the failure threshold (review finding C1).

    Breaks already act through the LEYP term and the segment rule; the
    condition penalty exists to drive CIP eligibility, not to be a third
    independent route to failure.
    """
    pipe.current_condition = 1.2
    pipe.degradation_rate = 1e-9
    for seg in pipe.segments:
        seg.n_point_breaks = 0
    pipe.simulate_year(1)
    assert pipe.current_condition >= BREAK_DAMAGE_CONDITION_FLOOR
    assert pipe.current_condition > 1.001


def test_break_damage_still_worsens_condition(pipe):
    """The coupling must remain real enough to drive CIP eligibility."""
    pipe.current_condition = 5.0
    before = pipe.current_condition
    result = pipe.simulate_year(1)
    if result["breaks"] > 0 and not result["failed"]:
        expected = before - BREAK_CONDITION_PENALTY * result["breaks"]
        assert pipe.current_condition == pytest.approx(max(BREAK_DAMAGE_CONDITION_FLOOR, expected))


def test_segment_rule_still_condemns(pipe):
    """Flooring break damage must not weaken the confirmed failure rule."""
    pipe.segments[0].n_point_breaks = SEGMENT_BREAK_THRESHOLD
    result = pipe.simulate_year(1)
    assert result["failed"] is True
    assert pipe.current_condition == 1.0


# --- Untreated baseline --------------------------------------------------------

@pytest.fixture(scope="module")
def untreated():
    return run_simulation(
        override_input_path=REAL_INVENTORY,
        seed=20260830,
        n_replicates=3,
        generate_report=True,
        no_intervention=True,
    )


def test_untreated_baseline_does_no_work(untreated):
    """A do-nothing baseline must spend nothing and renew nothing."""
    investment, _, cip, emergency, _ = untreated
    assert investment == 0.0
    assert cip == 0.0
    assert emergency == 0.0


def test_untreated_hazard_model_is_stable(untreated):
    """The model must not diverge without renewal (review finding C2).

    Before the feedback cap an untreated run reached ~1e7 breaks/mile/year:
    renewal was masking an unbounded positive feedback loop.
    """
    rate = untreated[4] / (NETWORK_MILES * 100)
    assert rate < 5.0, f"hazard model diverging: {rate:.3g} breaks/mile/year"


def test_untreated_break_rate_is_defensible(untreated):
    """Phase 4 gate: calibrated against this network's age and material mix.

    Anchor: Folkman (2018) national averages weighted by this material mix
    give 0.077 breaks/mile/year across all ages. An untreated network already
    60% past design life should sit several times above that.
    """
    rate = untreated[4] / (NETWORK_MILES * 100)
    assert 0.15 <= rate <= 0.40, f"{rate:.3f} breaks/mile/year outside target band"


def test_intervention_reduces_breaks(untreated):
    """Renewal must lower the break rate relative to doing nothing."""
    treated = run_simulation(
        override_input_path=REAL_INVENTORY,
        annual_budget=399046.84,
        rehab_trigger=2.8,
        seed=20260830,
        n_replicates=3,
        generate_report=True,
    )
    assert treated[4] < untreated[4]


def test_hazard_scale_is_the_calibrated_value():
    """Pin the calibrated constant so a silent edit fails the suite."""
    assert HAZARD_LENGTH_SCALE == 1500.0


# --- Break-driven failure at adequate funding ----------------------------------

def test_break_events_exceed_emergencies_when_funding_is_adequate(tmp_path):
    """Phase 4 gate, second clause (review finding C3).

    At the reference budget this fails because unfunded age-out dominates —
    a budget constraint (finding B5), not a calibration defect. Given a
    budget that can keep up, break events outnumber emergency replacements.
    """
    import pandas as pd

    out = tmp_path / "r"
    run_simulation(
        override_input_path=REAL_INVENTORY,
        annual_budget=1_000_000.0,
        rehab_trigger=2.8,
        output_dir=str(out),
        generate_report=True,
        seed=20260830,
        n_replicates=3,
    )
    plan = pd.read_csv(out / "Optimal_Action_Plan.csv")
    summary = pd.read_csv(out / "cost_summary.csv").iloc[0]
    break_events = (plan["Action"] == "Break_Event").sum()
    assert break_events > summary["Emergency_Count"]


def test_unfunded_age_out_falls_as_budget_rises(tmp_path):
    """Degradation emergencies must be explainable as a funding shortfall."""
    import pandas as pd

    def degradation_emergencies(budget):
        out = tmp_path / f"b{int(budget)}"
        run_simulation(
            override_input_path=REAL_INVENTORY,
            annual_budget=budget,
            rehab_trigger=2.8,
            output_dir=str(out),
            generate_report=True,
            seed=20260830,
            n_replicates=3,
        )
        return pd.read_csv(out / "cost_summary.csv").iloc[0]["Emergency_degradation"]

    assert degradation_emergencies(3_000_000.0) < degradation_emergencies(100_000.0)


# --- Planning realism: CIP cannot intercept a pipe the year it fails ----------

def test_cip_decides_on_last_assessment_condition(pipe_attrs, tmp_path):
    """CIP must act on condition as observed before the year's degradation.

    Deciding after degradation let the programme intercept a pipe in the very
    year it crossed the failure floor — perfect foresight plus same-year
    execution, which no capital programme can schedule, and which made
    replacing at the last possible moment look optimal.
    """
    import pandas as pd

    from leyp_runner import run_simulation

    # One pipe that starts just above the floor. With unlimited budget and a
    # trigger below its starting condition, CIP cannot see it this year, so
    # it must age out into the emergency stream rather than be intercepted.
    df = pd.DataFrame(
        [{"PipeID": "X", "Material": "GI", "Age": 200, "Length": 100.0,
          "Diameter": 6, "Condition": 1, "CoF_Value": 1}]
    )
    path = tmp_path / "one.csv"
    df.to_csv(path, index=False)

    out = tmp_path / "r"
    run_simulation(
        override_input_path=str(path),
        annual_budget=1e9,
        rehab_trigger=1.0,          # below the pipe's starting condition
        output_dir=str(out),
        generate_report=True,
        seed=61,
    )
    plan = pd.read_csv(out / "Optimal_Action_Plan.csv")
    assert (plan["Action"] == "Emergency_Replacement").any()


def test_last_moment_replacement_is_not_optimal(tmp_path):
    """A trigger at the failure floor should now cost more than one with margin."""
    from leyp_runner import simulate

    def total(trigger):
        r = simulate(
            REAL_INVENTORY, annual_budget=1_000_000.0, rehab_trigger=trigger,
            seed=20260830, n_replicates=3,
        )
        return r["investment_cost"] + r["risk_cost"]

    assert total(1.5) < total(1.05)


# --- NPV discounting -----------------------------------------------------------

def test_objectives_are_present_values():
    """Discounted objectives must be below their undiscounted totals."""
    from leyp_runner import simulate

    r = simulate(REAL_INVENTORY, annual_budget=1_000_000.0, rehab_trigger=1.5,
                 seed=20260830, n_replicates=2)
    assert r["investment_cost"] < r["nominal_investment_cost"]
    assert r["risk_cost"] < r["nominal_risk_cost"]


def test_zero_discount_rate_recovers_nominal_totals():
    from leyp_runner import simulate

    r = simulate(REAL_INVENTORY, annual_budget=1_000_000.0, rehab_trigger=1.5,
                 seed=20260830, n_replicates=2, discount_rate=0.0)
    assert r["investment_cost"] == pytest.approx(r["nominal_investment_cost"])
    assert r["risk_cost"] == pytest.approx(r["nominal_risk_cost"])


def test_higher_discount_rate_lowers_present_value():
    from leyp_runner import simulate

    def pv(rate):
        r = simulate(REAL_INVENTORY, annual_budget=1_000_000.0, rehab_trigger=1.5,
                     seed=20260830, n_replicates=2, discount_rate=rate)
        return r["investment_cost"] + r["risk_cost"]

    assert pv(0.07) < pv(0.03) < pv(0.0)


def test_discount_factors_decline_over_the_horizon(tmp_path):
    import pandas as pd

    from leyp_runner import run_simulation

    out = tmp_path / "r"
    run_simulation(
        override_input_path=REAL_INVENTORY, annual_budget=1_000_000.0,
        rehab_trigger=1.5, output_dir=str(out), generate_report=True,
        seed=62, n_replicates=1,
    )
    diag = pd.read_csv(out / "simulation_diagnostics.csv")
    factors = diag["Discount_Factor"].to_numpy()
    assert (factors[:-1] > factors[1:]).all()
    assert factors[0] < 1.0
