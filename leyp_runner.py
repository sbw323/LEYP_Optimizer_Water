"""
Monte Carlo simulation runner for water main replacement optimization.

Orchestrates 100-year simulation with three-phase annual loop:
degrade → planned CIP replacement → break simulation with emergency costs.
"""

import os

import pandas as pd

from checkpoint import safe_write_file
from leyp_config import (
    ACTION_BREAK_EVENT,
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


def run_simulation(
    use_mock_data: bool = False,
    override_input_path: str | None = None,
    output_dir: str | None = None,
    annual_budget: float | None = None,
    rehab_trigger: float | None = None,
    generate_report: bool = False,
) -> tuple:

    # --- A. LOAD DATA ---
    if use_mock_data:
        from leyp_config import REAL_DATA_PATH

        target_file = REAL_DATA_PATH
    else:
        from leyp_config import REAL_DATA_PATH

        target_file = override_input_path if override_input_path else REAL_DATA_PATH

    try:
        raw_df = pd.read_csv(target_file)
    except Exception as e:
        raise FileNotFoundError(f"Could not load input: {e}")

    # --- B. INITIALIZE NETWORK ---
    network = []
    for _, row in raw_df.iterrows():
        pipe_attrs = {}
        for csv_header, internal_key in COLUMN_MAP.items():
            pipe_attrs[internal_key] = row.get(csv_header, None)

        # Default CoF to 1.0 if missing (prevents crashes)
        if pipe_attrs.get("CoF_Value") is None:
            pipe_attrs["CoF_Value"] = 1.0

        network.append(Pipe(pipe_attrs))

    # --- C. INITIALIZE REPLACEMENT MANAGER ---
    use_budget = annual_budget if annual_budget is not None else ANNUAL_BUDGET
    use_trigger = rehab_trigger if rehab_trigger is not None else TRIGGERS['Rehab']

    replacement_manager = ReplacementManager(budget=use_budget, rehab_trigger=use_trigger)

    # --- D. THREE-COST-STREAM ACCOUNTING ---
    cip_cost = 0.0  # Planned CIP replacements
    repair_cost = 0.0  # Emergency repairs (per-break costs)
    emergency_cost = 0.0  # Emergency replacements (failed pipe costs)
    total_breaks = 0  # Cumulative break events across all pipes

    all_actions = []  # Action log combining CIP, emergency, and break events

    # Track pipes already at failure condition from initialization (B4).
    # These are pre-existing failures the utility inherited — they should
    # not be charged emergency replacement cost at simulation start.
    initially_dead = {pipe.id for pipe in network if pipe.current_condition <= 1.001}

    def _emergency_replace(pipe, year):
        """Charge emergency replacement cost and reset pipe to new state.

        Mirrors the CIP replacement reset in ReplacementManager.execute_replacement
        so that emergency-replaced pipes re-enter the simulation as new HDPE pipes.
        """
        nonlocal emergency_cost

        cost = pipe.length * EMERGENCY_REPLACEMENT_COST_PER_FT
        emergency_cost += cost

        # Capture pre-replacement state for logging
        pre_condition = pipe.current_condition
        original_material = pipe.material

        all_actions.append(
            {
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
            }
        )

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

        # If the pipe was in the initially_dead set and was later
        # CIP-replaced, then degraded back to failure, it IS a new
        # simulation event — remove from the bypass set.
        initially_dead.discard(pipe.id)

    for year in range(1, SIMULATION_YEARS + 1):
        # 1. DEGRADE — apply natural aging, catch degradation-caused failures
        for pipe in network:
            if pipe.current_condition <= 1.001:
                continue  # Already dead — no degradation to apply

            pipe.degrade()

            # Degradation brought pipe below failure threshold
            if pipe.current_condition <= 1.001:
                _emergency_replace(pipe, year)

        # 2. PLANNED CIP REPLACEMENT — budget-constrained proactive replacements
        cip_report = replacement_manager.run_year(network, year)
        cip_cost += cip_report["Spend"]

        # 3. BREAK SIMULATION AND EMERGENCY RESPONSE
        for pipe in network:
            if pipe.current_condition <= 1.001:
                # Skip pipes at failure condition. Pre-existing failures
                # (initially_dead) are left in place until B2/B4 is fixed.
                continue

            pre_break_condition = pipe.current_condition
            sim_result = pipe.simulate_year(year)

            # Accumulate emergency repair costs for individual breaks
            repair_cost += sim_result["repair_cost"]

            # Log break events (B5 fix: make breaks visible in action log)
            if sim_result["breaks"] > 0:
                total_breaks += sim_result["breaks"]
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

            # Handle pipe failure from break accumulation
            if sim_result["failed"] or pipe.current_condition <= 1.001:
                _emergency_replace(pipe, year)

    # --- E. COMBINE ACTION LOGS ---
    # Add CIP actions from replacement manager
    all_actions.extend(replacement_manager.action_log)

    # --- F. RETURN VALUES ---
    investment_cost = cip_cost  # Total planned investment
    risk_cost = repair_cost + emergency_cost  # Total emergency/risk costs

    if generate_report:
        # Create action log DataFrame
        action_log_df = pd.DataFrame(all_actions)

        # Generate report outputs if output directory specified
        if output_dir:
            _generate_reports(
                output_dir,
                all_actions,
                cip_cost,
                repair_cost,
                emergency_cost,
                investment_cost,
                risk_cost,
                total_breaks,
            )

        return investment_cost, risk_cost, cip_cost, emergency_cost, total_breaks
    else:
        return investment_cost, risk_cost


def _generate_reports(
    output_dir: str,
    all_actions: list,
    cip_cost: float,
    repair_cost: float,
    emergency_cost: float,
    investment_cost: float,
    risk_cost: float,
    total_breaks: int = 0,
) -> None:
    """
    Generate simulation reports using atomic file writes.

    Args:
        output_dir: Directory for output files
        all_actions: Combined list of CIP, emergency, and break event actions
        cip_cost: Total planned CIP replacement costs
        repair_cost: Total emergency repair costs
        emergency_cost: Total emergency replacement costs
        investment_cost: Total investment (= cip_cost)
        risk_cost: Total risk costs (= repair_cost + emergency_cost)
        total_breaks: Total break events across all pipes and years
    """
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Generate action log CSV
    if all_actions:
        action_df = pd.DataFrame(all_actions)
        # Sort chronologically (CIP actions from ReplacementManager are
        # appended after the simulation loop and land out of order)
        action_df.sort_values("Year", inplace=True, ignore_index=True)
        # Fill missing Breaks column for non-break rows
        if "Breaks" in action_df.columns:
            action_df["Breaks"] = action_df["Breaks"].fillna(0).astype(int)
        action_csv_path = os.path.join(output_dir, "Optimal_Action_Plan.csv")
        safe_write_file(action_csv_path, action_df.to_csv(index=False))

    # Generate cost summary CSV
    cost_summary = pd.DataFrame(
        [
            {
                "CIP_Cost": cip_cost,
                "Repair_Cost": repair_cost,
                "Emergency_Cost": emergency_cost,
                "Total_Investment": investment_cost,
                "Total_Risk": risk_cost,
                "Total_Cost": investment_cost + risk_cost,
                "Total_Breaks": total_breaks,
            }
        ]
    )

    summary_csv_path = os.path.join(output_dir, "cost_summary.csv")
    safe_write_file(summary_csv_path, cost_summary.to_csv(index=False))


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
    args = parser.parse_args()

    generate = args.output_dir is not None

    print("=== LEYP-Water: Single Simulation Run ===")
    print(f"  Budget:  {args.budget if args.budget else 'default (' + str(ANNUAL_BUDGET) + ')'}")
    print(f"  Trigger: {args.trigger if args.trigger else 'default (' + str(TRIGGERS['Rehab']) + ')'}")
    print(f"  Input:   {args.input if args.input else 'default'}")
    print(f"  Report:  {'→ ' + args.output_dir if generate else 'off (pass --output-dir to enable)'}")
    print()

    try:
        result = run_simulation(
            override_input_path=args.input,
            annual_budget=args.budget,
            rehab_trigger=args.trigger,
            output_dir=args.output_dir,
            generate_report=generate,
        )

        if generate:
            inv, risk, cip, emerg, breaks = result
            total = inv + risk
            print(f"\n--- Results (100-year horizon) ---")
            print(f"  Investment Cost (CIP):      ${inv:>14,.0f}")
            print(f"  Risk Cost (repairs+emerg):   ${risk:>14,.0f}")
            print(f"    - Emergency repairs:       ${(risk - emerg):>14,.0f}")
            print(f"    - Emergency replacements:  ${emerg:>14,.0f}")
            print(f"  Total Cost:                  ${total:>14,.0f}")
            print(f"  Total Breaks:                {breaks:>14,d}")
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
            print(f"\n--- Results (100-year horizon) ---")
            print(f"  Investment Cost: ${inv:>14,.0f}")
            print(f"  Risk Cost:       ${risk:>14,.0f}")
            print(f"  Total Cost:      ${total:>14,.0f}")
            print(f"\n  (pass --output-dir <path> for detailed action log)")

    except Exception as e:
        print(f"\n[ERROR] Simulation failed: {e}")
        raise SystemExit(1)