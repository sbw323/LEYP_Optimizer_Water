"""
Monte Carlo simulation runner for water main replacement optimization.

Orchestrates 100-year simulation with three-phase annual loop:
degrade -> planned CIP replacement -> break simulation with emergency costs.

The simulation is stochastic (pipe TTF sampling, seeded break history, and
Poisson break events all draw from NumPy's global random stream).  Pass
``seed`` to make a run reproducible and ``n_replicates`` to average over
several draws instead of reporting a single sample.
"""

import os

import numpy as np
import pandas as pd

from checkpoint import safe_write_file
from leyp_config import (
    ACTION_BREAK_EVENT,
    BACKLOG_CONDITION,
    ACTION_EMERGENCY_REPLACEMENT,
    ANNUAL_BUDGET,
    COLUMN_MAP,
    DEFAULT_REPLACEMENT_MATERIAL,
    EMERGENCY_REPLACEMENT_COST_PER_FT,
    N_SEGMENTS_PER_PIPE,
    SIMULATION_YEARS,
    TRIGGERS,
)
from leyp_core import Pipe, VirtualSegment
from water_replacement import ReplacementManager

FEET_PER_MILE = 5280.0

# Root causes recorded for every emergency replacement.  Kept as constants so
# the diagnostics writer and the tests agree on spelling.
CAUSE_DEGRADATION = "degradation"       # condition decayed past the failure floor
CAUSE_BREAK_FAILURE = "break_failure"   # a segment reached SEGMENT_BREAK_THRESHOLD
CAUSE_BREAK_DAMAGE = "break_damage"     # break damage pushed condition past the floor
EMERGENCY_CAUSES = (CAUSE_DEGRADATION, CAUSE_BREAK_FAILURE, CAUSE_BREAK_DAMAGE)


def _load_network_frame(target_file: str) -> pd.DataFrame:
    """Read the pipe inventory CSV, raising a clear error if it is unusable."""
    try:
        return pd.read_csv(target_file)
    except Exception as e:
        raise FileNotFoundError(f"Could not load input: {e}")


def _build_network(raw_df: pd.DataFrame) -> list:
    """Instantiate Pipe objects from the inventory frame.

    Draws from the global NumPy stream (TTF sampling and break seeding), so
    the caller is responsible for seeding beforehand when reproducibility
    is required.
    """
    network = []
    for _, row in raw_df.iterrows():
        pipe_attrs = {}
        for csv_header, internal_key in COLUMN_MAP.items():
            pipe_attrs[internal_key] = row.get(csv_header, None)

        # Default CoF to 1.0 if missing (prevents crashes)
        if pipe_attrs.get("CoF_Value") is None:
            pipe_attrs["CoF_Value"] = 1.0

        network.append(Pipe(pipe_attrs))
    return network


def _simulate_once(
    raw_df: pd.DataFrame,
    annual_budget: float,
    rehab_trigger: float,
    seed: int | None = None,
    no_intervention: bool = False,
) -> dict:
    """Run a single 100-year replicate.

    Args:
        raw_df: Pipe inventory frame.
        annual_budget: Annual CIP budget in dollars.
        rehab_trigger: Condition threshold for CIP eligibility.
        seed: Seed applied to the global NumPy stream before the network is
            built.  ``None`` leaves the stream untouched (non-reproducible).
        no_intervention: True runs a genuine do-nothing baseline: no CIP and
            no emergency renewal, with pipes left in service accruing breaks.
            A zero budget alone is not a do-nothing baseline, because
            emergency replacement still renews the network for free.

    Returns:
        Dict of cost totals, the combined action log, and per-year diagnostics.
    """
    if seed is not None:
        np.random.seed(seed)

    network = _build_network(raw_df)
    replacement_manager = ReplacementManager(budget=annual_budget, rehab_trigger=rehab_trigger)

    # --- THREE-COST-STREAM ACCOUNTING ---
    cip_cost = 0.0  # Planned CIP replacements
    repair_cost = 0.0  # Emergency repairs (per-break costs)
    emergency_cost = 0.0  # Emergency replacements (failed pipe costs)
    total_breaks = 0  # Cumulative break events across all pipes

    all_actions = []  # Action log combining CIP, emergency, and break events
    yearly = []  # Per-year diagnostic rows

    # Per-year emergency tallies, reset at the top of each year.
    year_emergency = {}

    # --- Inherited backlog (review finding A3) ---
    # Pipes already in replacement-worthy condition in year 1 are a backlog the
    # utility inherited, not deterioration this plan caused.  Their first
    # replacement is tagged so the action plan distinguishes "working off the
    # backlog" from steady-state renewal.  This is reporting only: it does not
    # change which pipes are replaced or what they cost.
    #
    # Measured against the fixed BACKLOG_CONDITION standard, not the run's
    # rehab_trigger.  The backlog is a property of the inventory, so it has to
    # mean the same thing across strategies; scoring it against the trigger
    # made it read as zero whenever the optimizer chose a low trigger, which
    # described the strategy rather than the network.
    backlog_ids = {
        pipe.id for pipe in network if pipe.current_condition <= BACKLOG_CONDITION
    }
    backlog_length = sum(p.length for p in network if p.id in backlog_ids)
    backlog_pending = set(backlog_ids)
    backlog_cleared = {"cip": 0, "emergency": 0}
    backlog_emergency_cost = 0.0

    def _tag_backlog(action, pipe, via):
        """Mark an action as clearing inherited backlog, first replacement only."""
        first = pipe.id in backlog_pending
        action["Backlog"] = first
        if first:
            backlog_pending.discard(pipe.id)
            backlog_cleared[via] += 1
        return first

    def _emergency_replace(pipe, year, cause):
        """Charge emergency replacement cost and reset pipe to new state.

        Mirrors the CIP replacement reset in ReplacementManager.execute_replacement
        so that emergency-replaced pipes re-enter the simulation as new HDPE pipes.

        Args:
            pipe: Pipe object being replaced.
            year: Current simulation year.
            cause: One of EMERGENCY_CAUSES, recorded for diagnostics.
        """
        nonlocal emergency_cost, backlog_emergency_cost

        cost = pipe.length * EMERGENCY_REPLACEMENT_COST_PER_FT
        emergency_cost += cost

        year_emergency[cause] = year_emergency.get(cause, 0) + 1
        year_emergency["cost"] = year_emergency.get("cost", 0.0) + cost

        # Capture pre-replacement state for logging
        pre_condition = pipe.current_condition
        original_material = pipe.material

        action = {
                "Year": year,
                "PipeID": pipe.id,
                "Action": ACTION_EMERGENCY_REPLACEMENT,
                "PreCondition": pre_condition,
                "PostCondition": 6.0,
                "Condition_Before": pre_condition,
                "Priority": 0.0,
                "Cost": cost,
                "Length": pipe.length,
                "Diameter": pipe.diameter,
                "Material": original_material,
                "NewMaterial": DEFAULT_REPLACEMENT_MATERIAL,
                "Cause": cause,
        }
        if _tag_backlog(action, pipe, "emergency"):
            backlog_emergency_cost += cost
            year_emergency["backlog"] = year_emergency.get("backlog", 0) + 1
        all_actions.append(action)

        # --- Reset pipe state (mirrors CIP replacement) ---
        pipe.current_condition = 6.0
        pipe.material = DEFAULT_REPLACEMENT_MATERIAL
        pipe.initial_age = -year
        pipe.has_failed_in_sim = False

        seg_len = pipe.length / N_SEGMENTS_PER_PIPE
        pipe.segments = [
            VirtualSegment(seg_len) for _ in range(N_SEGMENTS_PER_PIPE)
        ]

        pipe.reset_physics_params()
        pipe.update_leyp_state()

    for year in range(1, SIMULATION_YEARS + 1):
        year_emergency = {}
        year_breaks = 0
        year_break_events = 0
        year_repair_cost = 0.0

        # 1. DEGRADE — apply natural aging
        for pipe in network:
            pipe.degrade()

        year_backlog_cip = 0
        if no_intervention:
            cip_report = {
                "Year": year, "Spend": 0.0, "Count": 0,
                "Eligible": 0, "Deferred": 0,
                "Unfundable": 0, "Unfundable_Length": 0.0,
            }
        else:
            # 2. PLANNED CIP REPLACEMENT — budget-constrained proactive
            # replacements.  This runs BEFORE the emergency sweep (review
            # finding B3): pipes that aged out during degradation get one
            # chance at planned renewal instead of being handed straight to
            # the emergency stream.
            cip_log_start = len(replacement_manager.action_log)
            cip_report = replacement_manager.run_year(network, year)
            cip_cost += cip_report["Spend"]

            # Tag this year's CIP actions against the inherited backlog.
            # Reading the log slice keeps the tagging chronological.
            by_id = {p.id: p for p in network}
            for action in replacement_manager.action_log[cip_log_start:]:
                if _tag_backlog(action, by_id[action["PipeID"]], "cip"):
                    year_backlog_cip += 1

            # 3. EMERGENCY REPLACEMENT — pipes that aged out and CIP could not fund
            for pipe in network:
                if pipe.current_condition <= 1.001:
                    _emergency_replace(pipe, year, CAUSE_DEGRADATION)

        # 4. BREAK SIMULATION AND EMERGENCY RESPONSE
        for pipe in network:
            pre_break_condition = pipe.current_condition
            sim_result = pipe.simulate_year(year)

            # Accumulate emergency repair costs for individual breaks
            repair_cost += sim_result["repair_cost"]
            year_repair_cost += sim_result["repair_cost"]

            # Log break events (B5 fix: make breaks visible in action log)
            if sim_result["breaks"] > 0:
                total_breaks += sim_result["breaks"]
                year_breaks += sim_result["breaks"]
                year_break_events += 1
                all_actions.append(
                    {
                        "Year": year,
                        "PipeID": pipe.id,
                        "Action": ACTION_BREAK_EVENT,
                        "PreCondition": pre_break_condition,
                        "PostCondition": pipe.current_condition,
                        "Condition_Before": pre_break_condition,
                        "Priority": 0.0,
                        "Cost": sim_result["repair_cost"],
                        "Length": pipe.length,
                        "Diameter": pipe.diameter,
                        "Material": pipe.material,
                        "NewMaterial": None,
                        "Breaks": sim_result["breaks"],
                    }
                )

            # Handle pipe failure from break accumulation.  Under
            # no_intervention the pipe is left in service and keeps breaking,
            # which is what makes the baseline a true do-nothing case.
            if not no_intervention and (
                sim_result["failed"] or pipe.current_condition <= 1.001
            ):
                cause = CAUSE_BREAK_FAILURE if sim_result["failed"] else CAUSE_BREAK_DAMAGE
                _emergency_replace(pipe, year, cause)

        row = {
            "Year": year,
            "Budget": annual_budget,
            "CIP_Count": cip_report["Count"],
            "CIP_Spend": cip_report["Spend"],
            "Budget_Utilization": (
                cip_report["Spend"] / annual_budget if annual_budget > 0 else 0.0
            ),
            "Emergency_Count": sum(year_emergency.get(c, 0) for c in EMERGENCY_CAUSES),
            "Emergency_Cost": year_emergency.get("cost", 0.0),
            "Break_Events": year_break_events,
            "Breaks": year_breaks,
            "Repair_Cost": year_repair_cost,
            "Backlog_CIP": year_backlog_cip,
            "Backlog_Emergency": year_emergency.get("backlog", 0),
            "Backlog_Remaining": len(backlog_pending),
            "Eligible": cip_report["Eligible"],
            "Deferred": cip_report["Deferred"],
            "Unfundable": cip_report["Unfundable"],
            "Unfundable_Length": cip_report["Unfundable_Length"],
        }
        for cause in EMERGENCY_CAUSES:
            row[f"Emergency_{cause}"] = year_emergency.get(cause, 0)
        yearly.append(row)

    # --- COMBINE ACTION LOGS ---
    all_actions.extend(replacement_manager.action_log)

    return {
        "investment_cost": cip_cost,
        "risk_cost": repair_cost + emergency_cost,
        "cip_cost": cip_cost,
        "repair_cost": repair_cost,
        "emergency_cost": emergency_cost,
        "total_breaks": total_breaks,
        "actions": all_actions,
        "yearly": yearly,
        "backlog_pipes": len(backlog_ids),
        "backlog_length": backlog_length,
        "backlog_cleared_by_cip": backlog_cleared["cip"],
        "backlog_cleared_by_emergency": backlog_cleared["emergency"],
        "backlog_emergency_cost": backlog_emergency_cost,
        "backlog_never_cleared": len(backlog_pending),
        "network_pipes": len(network),
    }


def run_simulation(
    use_mock_data: bool = False,
    override_input_path: str | None = None,
    output_dir: str | None = None,
    annual_budget: float | None = None,
    rehab_trigger: float | None = None,
    generate_report: bool = False,
    seed: int | None = None,
    n_replicates: int = 1,
    no_intervention: bool = False,
) -> tuple:
    """Run the 100-year replacement simulation.

    Args:
        use_mock_data: Retained for backward compatibility; the real inventory
            is used either way.
        override_input_path: Pipe inventory CSV to use instead of the configured default.
        output_dir: Directory for report output (requires generate_report).
        annual_budget: Annual CIP budget in dollars.
        rehab_trigger: Condition threshold for CIP eligibility.
        generate_report: Return the detailed 5-tuple and write reports.
        seed: Base seed for the run.  Replicate *i* uses ``seed + i``, so a
            given (seed, budget, trigger) always reproduces exactly.  ``None``
            leaves the global stream untouched, giving a non-reproducible draw.
        n_replicates: Number of stochastic replicates to average.  Costs and
            break counts are returned as means; reports are written from the
            replicate closest to the mean total cost.
        no_intervention: True runs a do-nothing baseline with no CIP and no
            emergency renewal — the reference case for break-rate calibration
            and for the validation curve's denominator.

    Returns:
        (investment_cost, risk_cost) or, with generate_report,
        (investment_cost, risk_cost, cip_cost, emergency_cost, total_breaks).
        All values are means across replicates.

    Note:
        Seeding applies to NumPy's global stream, so concurrent evaluations in
        the same process are not independently reproducible.  Run replicates
        in separate processes if parallelising.
    """
    if n_replicates < 1:
        raise ValueError(f"n_replicates must be >= 1, got {n_replicates}")

    # --- A. LOAD DATA ---
    from leyp_config import REAL_DATA_PATH

    target_file = override_input_path if override_input_path else REAL_DATA_PATH
    raw_df = _load_network_frame(target_file)

    # --- B. RESOLVE PARAMETERS ---
    use_budget = annual_budget if annual_budget is not None else ANNUAL_BUDGET
    use_trigger = rehab_trigger if rehab_trigger is not None else TRIGGERS["Rehab"]

    # --- C. RUN REPLICATES ---
    replicates = [
        _simulate_once(
            raw_df,
            annual_budget=use_budget,
            rehab_trigger=use_trigger,
            seed=None if seed is None else seed + i,
            no_intervention=no_intervention,
        )
        for i in range(n_replicates)
    ]

    def _mean(key):
        return float(np.mean([r[key] for r in replicates]))

    investment_cost = _mean("investment_cost")
    risk_cost = _mean("risk_cost")
    cip_cost = _mean("cip_cost")
    emergency_cost = _mean("emergency_cost")
    total_breaks = _mean("total_breaks")

    if generate_report:
        if output_dir:
            # Report from the replicate nearest the mean total cost, so the
            # action plan is representative of the returned figures rather
            # than an arbitrary draw.
            mean_total = investment_cost + risk_cost
            representative = min(
                replicates,
                key=lambda r: abs((r["investment_cost"] + r["risk_cost"]) - mean_total),
            )
            _generate_reports(
                output_dir,
                representative,
                replicates,
                network_length_ft=float(raw_df["Length"].sum()),
            )

        return investment_cost, risk_cost, cip_cost, emergency_cost, total_breaks

    return investment_cost, risk_cost


def _years_to_clear_backlog(yearly_df: pd.DataFrame) -> float:
    """First year the inherited backlog reaches zero, or NaN if never cleared."""
    cleared = yearly_df[yearly_df["Backlog_Remaining"] == 0]
    return float(cleared["Year"].iloc[0]) if not cleared.empty else float("nan")


def _generate_reports(
    output_dir: str,
    representative: dict,
    replicates: list,
    network_length_ft: float,
) -> None:
    """Write the action plan, cost summary, and diagnostics using atomic writes.

    Args:
        output_dir: Directory for output files.
        representative: The replicate whose action log and per-year rows are written.
        replicates: All replicates, used for cross-replicate spread statistics.
        network_length_ft: Total inventory length, for breaks-per-mile-year.
    """
    os.makedirs(output_dir, exist_ok=True)

    # --- Action log ---
    all_actions = representative["actions"]
    if all_actions:
        action_df = pd.DataFrame(all_actions)
        # Sort chronologically (CIP actions from ReplacementManager are
        # appended after the simulation loop and land out of order)
        action_df.sort_values("Year", inplace=True, ignore_index=True)
        # Fill missing Breaks column for non-break rows
        if "Breaks" in action_df.columns:
            action_df["Breaks"] = action_df["Breaks"].fillna(0).astype(int)
        if "Cause" in action_df.columns:
            action_df["Cause"] = action_df["Cause"].fillna("")
        if "Backlog" in action_df.columns:
            action_df["Backlog"] = action_df["Backlog"].fillna(False).astype(bool)
        action_csv_path = os.path.join(output_dir, "Optimal_Action_Plan.csv")
        safe_write_file(action_csv_path, action_df.to_csv(index=False))

    # --- Per-year diagnostics ---
    yearly_df = pd.DataFrame(representative["yearly"])
    safe_write_file(
        os.path.join(output_dir, "simulation_diagnostics.csv"),
        yearly_df.to_csv(index=False),
    )

    # --- Cost summary, with the health metrics the review tracks ---
    cip_cost = representative["cip_cost"]
    repair_cost = representative["repair_cost"]
    emergency_cost = representative["emergency_cost"]
    investment_cost = representative["investment_cost"]
    risk_cost = representative["risk_cost"]
    total_breaks = representative["total_breaks"]

    cip_count = int(yearly_df["CIP_Count"].sum())
    emergency_count = int(yearly_df["Emergency_Count"].sum())
    n_years = len(yearly_df)
    miles = network_length_ft / FEET_PER_MILE

    early = yearly_df[yearly_df["Year"] <= 2]["Emergency_Count"].sum()
    mean_annual_emergency = emergency_count / n_years if n_years else 0.0

    totals = [r["investment_cost"] + r["risk_cost"] for r in replicates]

    summary = {
        "CIP_Cost": cip_cost,
        "Repair_Cost": repair_cost,
        "Emergency_Cost": emergency_cost,
        "Total_Investment": investment_cost,
        "Total_Risk": risk_cost,
        "Total_Cost": investment_cost + risk_cost,
        "Total_Breaks": total_breaks,
        # --- Review health metrics ---
        "CIP_Count": cip_count,
        "Emergency_Count": emergency_count,
        "CIP_To_Emergency_Ratio": (
            cip_count / emergency_count if emergency_count else float("inf")
        ),
        "Mean_Budget_Utilization": float(yearly_df["Budget_Utilization"].mean()),
        "Zero_Spend_Years": int((yearly_df["CIP_Spend"] == 0).sum()),
        "Breaks_Per_Mile_Year": (
            total_breaks / (miles * n_years) if miles and n_years else 0.0
        ),
        "Investment_Share_Of_Total": (
            investment_cost / (investment_cost + risk_cost)
            if (investment_cost + risk_cost)
            else 0.0
        ),
        "Yr1_2_Emergency_Vs_Annual_Mean": (
            (early / 2.0) / mean_annual_emergency if mean_annual_emergency else 0.0
        ),
        # Absolute counts, reported alongside the ratio above: once emergencies
        # become rare the ratio divides by a very small mean and overstates any
        # early-year cluster, so it must not be read on its own.
        "Yr1_2_Emergency_Count": int(early),
        "Max_Annual_Emergency_Count": int(yearly_df["Emergency_Count"].max()),
        "Mean_Unfundable_Per_Year": float(yearly_df["Unfundable"].mean()),
        "N_Replicates": len(replicates),
        "Total_Cost_SD": float(np.std(totals)) if len(replicates) > 1 else 0.0,
        # --- Inherited backlog (A3): eligible for replacement in year 1 ---
        "Backlog_Pipes": representative["backlog_pipes"],
        "Backlog_Length_Ft": representative["backlog_length"],
        "Backlog_Share_Of_Network": (
            representative["backlog_pipes"] / representative["network_pipes"]
            if representative["network_pipes"]
            else 0.0
        ),
        "Backlog_Cleared_By_CIP": representative["backlog_cleared_by_cip"],
        "Backlog_Cleared_By_Emergency": representative["backlog_cleared_by_emergency"],
        "Backlog_Emergency_Cost": representative["backlog_emergency_cost"],
        "Backlog_Never_Cleared": representative["backlog_never_cleared"],
        "Backlog_Years_To_Clear": _years_to_clear_backlog(yearly_df),
    }
    for cause in EMERGENCY_CAUSES:
        summary[f"Emergency_{cause}"] = int(yearly_df[f"Emergency_{cause}"].sum())

    safe_write_file(
        os.path.join(output_dir, "cost_summary.csv"),
        pd.DataFrame([summary]).to_csv(index=False),
    )


# ============================================================
# CLI Entry Point
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="LEYP-Water single simulation run")
    parser.add_argument("--input", type=str, default=None,
                        help="Path to pipe inventory CSV (default: leyp_config.REAL_DATA_PATH)")
    parser.add_argument("--budget", type=float, default=None,
                        help="Annual CIP budget in dollars (default: leyp_config.ANNUAL_BUDGET)")
    parser.add_argument("--trigger", type=float, default=None,
                        help="Replacement condition trigger (default: leyp_config.TRIGGERS['Rehab'])")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Directory for report output (enables detailed reporting)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Base random seed; makes the run reproducible")
    parser.add_argument("--replicates", type=int, default=1,
                        help="Number of stochastic replicates to average (default: 1)")
    args = parser.parse_args()

    generate = args.output_dir is not None

    print("=== LEYP-Water: Single Simulation Run ===")
    print(f"  Budget:     {args.budget if args.budget else 'default (' + str(ANNUAL_BUDGET) + ')'}")
    print(f"  Trigger:    {args.trigger if args.trigger else 'default (' + str(TRIGGERS['Rehab']) + ')'}")
    print(f"  Input:      {args.input if args.input else 'default'}")
    print(f"  Seed:       {args.seed if args.seed is not None else 'unseeded (non-reproducible)'}")
    print(f"  Replicates: {args.replicates}")
    print(f"  Report:     {'-> ' + args.output_dir if generate else 'off (pass --output-dir to enable)'}")
    print()

    try:
        result = run_simulation(
            override_input_path=args.input,
            annual_budget=args.budget,
            rehab_trigger=args.trigger,
            output_dir=args.output_dir,
            generate_report=generate,
            seed=args.seed,
            n_replicates=args.replicates,
        )

        if generate:
            inv, risk, cip, emerg, breaks = result
            total = inv + risk
            print("\n--- Results (100-year horizon) ---")
            print(f"  Investment Cost (CIP):       ${inv:>14,.0f}")
            print(f"  Risk Cost (repairs+emerg):   ${risk:>14,.0f}")
            print(f"    - Emergency repairs:       ${(risk - emerg):>14,.0f}")
            print(f"    - Emergency replacements:  ${emerg:>14,.0f}")
            print(f"  Total Cost:                  ${total:>14,.0f}")
            print(f"  Total Breaks:                 {breaks:>14,.1f}")
            if args.output_dir:
                print(f"\n  Reports written to: {args.output_dir}/")

                # --- Validation Curve ---
                print("\n--- Generating Validation Curve ---")
                try:
                    from water_validation import (
                        generate_validation_curve,
                        plot_validation_curve,
                        save_validation_data,
                    )
                    from leyp_config import REAL_DATA_PATH

                    val_input = args.input if args.input else REAL_DATA_PATH
                    val_budget_max = (args.budget if args.budget else ANNUAL_BUDGET) * 10

                    pct_rn, pct_an, pct_rl, pct_al = generate_validation_curve(
                        input_file_path=val_input,
                        budget_min=0,
                        budget_max=val_budget_max,
                        n_points=10,
                    )

                    if pct_rn:  # Non-empty results
                        plot_path = os.path.join(args.output_dir, "validation_curve.png")
                        plot_validation_curve(pct_rn, pct_an, pct_rl, pct_al, plot_path)

                        data_path = os.path.join(args.output_dir, "validation_data.csv")
                        save_validation_data(pct_rn, pct_an, pct_rl, pct_al, data_path)
                    else:
                        print("  Validation curve returned empty results.")

                except Exception as e:
                    print(f"  Error generating validation curve: {e}")
        else:
            inv, risk = result
            total = inv + risk
            print("\n--- Results (100-year horizon) ---")
            print(f"  Investment Cost: ${inv:>14,.0f}")
            print(f"  Risk Cost:       ${risk:>14,.0f}")
            print(f"  Total Cost:      ${total:>14,.0f}")
            print("\n  (pass --output-dir <path> for detailed action log)")

    except Exception as e:
        print(f"\n[ERROR] Simulation failed: {e}")
        raise SystemExit(1)
