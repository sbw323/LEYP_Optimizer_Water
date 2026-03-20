"""
Monte Carlo simulation runner for water main replacement optimization.

Orchestrates 100-year simulation with three-phase annual loop:
degrade → planned CIP replacement → break simulation with emergency costs.
"""

import os

import pandas as pd

from config.checkpoint import safe_write_file
from leyp_config import (
    ACTION_EMERGENCY_REPLACEMENT,
    ANNUAL_BUDGET,
    COLUMN_MAP,
    EMERGENCY_REPLACEMENT_COST_PER_FT,
    SIMULATION_YEARS,
    TRIGGERS,
)
from leyp_core import Pipe
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

    all_actions = []  # Action log combining CIP and emergency actions

    for year in range(1, SIMULATION_YEARS + 1):
        # 1. DEGRADE - Apply natural aging to all pipes
        for pipe in network:
            pipe.degrade()

        # 2. PLANNED CIP REPLACEMENT - Execute budget-constrained replacements
        cip_report = replacement_manager.run_year(network, year)
        cip_cost += cip_report["Spend"]

        # 3. BREAK SIMULATION AND EMERGENCY RESPONSE
        for pipe in network:
            # Only simulate breaks for non-failed pipes
            if pipe.current_condition > 1.001:
                sim_result = pipe.simulate_year(year)

                # Add emergency repair costs
                repair_cost += sim_result["repair_cost"]

                # Handle pipe failure from breaks or degradation
                if sim_result["failed"] or pipe.current_condition <= 1.001:
                    # Emergency replacement cost
                    emergency_replacement_cost = pipe.length * EMERGENCY_REPLACEMENT_COST_PER_FT
                    emergency_cost += emergency_replacement_cost

                    # Log emergency replacement action
                    all_actions.append(
                        {
                            "Year": year,
                            "PipeID": pipe.id,
                            "Action": ACTION_EMERGENCY_REPLACEMENT,
                            "PreCondition": pipe.current_condition,
                            "PostCondition": 1.0,  # Emergency replacement leaves pipe at failure state
                            "Condition_Before": pipe.current_condition,  # Alias for validator compatibility
                            "Priority": 0.0,  # Emergency actions have no priority ranking
                            "Cost": emergency_replacement_cost,
                            "Length": pipe.length,
                            "Diameter": pipe.diameter,
                            "Material": pipe.material,
                            "NewMaterial": None,  # Emergency replacements don't specify new material
                        }
                    )

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
            )

        return investment_cost, risk_cost, cip_cost, emergency_cost
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
) -> None:
    """
    Generate simulation reports using atomic file writes.

    Args:
        output_dir: Directory for output files
        all_actions: Combined list of CIP and emergency actions
        cip_cost: Total planned CIP replacement costs
        repair_cost: Total emergency repair costs
        emergency_cost: Total emergency replacement costs
        investment_cost: Total investment (= cip_cost)
        risk_cost: Total risk costs (= repair_cost + emergency_cost)
    """
    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Generate action log CSV
    if all_actions:
        action_df = pd.DataFrame(all_actions)
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
            }
        ]
    )

    summary_csv_path = os.path.join(output_dir, "cost_summary.csv")
    safe_write_file(summary_csv_path, cost_summary.to_csv(index=False))
