"""
leyp_optimizer.py

Water main replacement planning optimizer using NSGA-II multi-objective
evolutionary algorithm. Optimizes budget allocation and replacement trigger
thresholds to minimize investment cost and risk cost.

Features:
- 2 objectives: investment cost, risk cost
- 2 decision variables: budget, rehab_trigger
- NSGA-II algorithm with preemption-safe checkpointing
- Pareto frontier analysis and optimal action plan generation
"""

import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from pymoo.algorithms.moo.nsga2 import NSGA2
from pymoo.core.problem import ElementwiseProblem
from pymoo.operators.crossover.sbx import SBX
from pymoo.operators.mutation.pm import PM
from pymoo.operators.sampling.rnd import FloatRandomSampling
from pymoo.optimize import minimize
from pymoo.termination import get_termination

from checkpoint import (
    OptimizationCheckpoint,
    safe_write_file,
)
from leyp_config import NSGA2_CHECKPOINT_EVERY_N_GEN, NSGA2_CHECKPOINT_PATH
from leyp_preprocessor import preprocess_network
from leyp_runner import run_simulation
from water_validation import generate_validation_curve, plot_validation_curve, save_validation_data

CONFIG_FILE = "optimizer_config.yaml"


def load_config():
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"Missing config file: {CONFIG_FILE}")
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


class Water_LEYP_Problem(ElementwiseProblem):
    """Water main replacement optimization problem for NSGA-II.

    A multi-objective optimization problem with 2 decision variables:
    - budget: Annual CIP replacement budget ($)
    - rehab_trigger: Condition threshold for replacement eligibility (1-6 scale)

    And 2 objectives to minimize:
    - Investment cost: Total planned CIP replacement spending
    - Risk cost: Total emergency repair and replacement costs

    Args:
        config: Configuration dictionary with gene bounds
        input_file_path: Path to preprocessed pipe inventory CSV
    """

    def __init__(self, config, input_file_path):
        genes = config["genes"]
        sim = config.get("simulation", {}) or {}

        # Common random numbers: every candidate is evaluated against the same
        # stochastic draw, so differences in the objectives are attributable to
        # the genes rather than to simulation noise.
        self.seed = sim.get("seed")
        self.n_replicates = int(sim.get("n_replicates", 1))

        super().__init__(
            n_var=2,  # Budget, Rehab_trigger
            n_obj=2,  # Investment_Cost, Risk_Cost
            n_ieq_constr=0,  # No constraints
            xl=np.array(
                [
                    genes["budget"]["min"],
                    genes["rehab_trigger"]["min"],
                ]
            ),
            xu=np.array(
                [
                    genes["budget"]["max"],
                    genes["rehab_trigger"]["max"],
                ]
            ),
        )
        self.input_file = input_file_path

    def _evaluate(self, x, out, *args, **kwargs):
        """Evaluate a candidate solution.

        Args:
            x: Decision variables [budget, rehab_trigger]
            out: Output dictionary for objectives and constraints
        """
        # Unpack 2 genes
        budget, rehab_trigger = x[0], x[1]

        try:
            # Run simulation with candidate parameters
            inv_cost, risk_cost = run_simulation(
                use_mock_data=False,
                override_input_path=self.input_file,
                annual_budget=budget,
                rehab_trigger=rehab_trigger,
                seed=self.seed,
                n_replicates=self.n_replicates,
            )
        except Exception as e:
            print(f"[Optimizer Error] {e}")
            inv_cost, risk_cost = 1e9, 1e9

        out["F"] = [inv_cost, risk_cost]


def run_optimization():
    print("=== LEYP Genetic Optimizer (NSGA-II) ===")
    config = load_config()
    raw_input = config["master_input_file"]
    output_dir = config["output_base_dir"]
    skip_seg = config.get("skip_segmentation", False)

    os.makedirs(output_dir, exist_ok=True)

    # Preprocessing
    optimized_input_path = "temp_optimization_input.csv"
    try:
        preprocess_network(
            input_path=raw_input, output_path=optimized_input_path, skip_segmentation=skip_seg
        )
    except Exception as e:
        print(f"Data Prep Error: {e}")
        return

    # Algorithm Setup with Checkpoint Integration
    alg = config["algorithm"]

    opt_ckpt = OptimizationCheckpoint(
        checkpoint_path=NSGA2_CHECKPOINT_PATH,
        save_every_n_gen=NSGA2_CHECKPOINT_EVERY_N_GEN,
    )

    algorithm = opt_ckpt.restore_or_create(
        lambda: NSGA2(
            pop_size=alg["pop_size"],
            n_offsprings=alg["n_offsprings"],
            sampling=FloatRandomSampling(),
            crossover=SBX(prob=0.9, eta=15),
            mutation=PM(prob=0.2, eta=20),
            eliminate_duplicates=True,
        )
    )

    if opt_ckpt.resumed_from_gen > 0:
        print(f"Resuming from generation {opt_ckpt.resumed_from_gen}")

    termination = get_termination("n_gen", alg["n_gen"])

    print(f"Starting Evolution ({alg['n_gen']} gens)...")
    res = minimize(
        Water_LEYP_Problem(config, optimized_input_path),
        algorithm,
        termination,
        callback=opt_ckpt.get_callback(),
        seed=alg["seed"],
        verbose=True,
    )

    # Results Processing
    cols = ["Investment_Cost", "Risk_Cost"]
    df = pd.DataFrame(res.F, columns=cols)
    df["Budget"] = res.X[:, 0]
    df["Rehab_Trigger"] = res.X[:, 1]
    df["Total_Cost"] = df["Investment_Cost"] + df["Risk_Cost"]

    results_path = os.path.join(output_dir, "nsga2_results.csv")
    safe_write_file(results_path, df.to_csv(index=False))
    print(f"Optimization results saved to {results_path}")

    # Generate cost curve visualization
    plot_cost_curves(df, output_dir)

    # --- VICTORY LAP: Generate Detailed Schedule for Best Strategy ---
    print("\n--- Generating Optimal Action Plan ---")

    # 1. Identify Best Strategy
    best_idx = df["Total_Cost"].idxmin()
    best = df.loc[best_idx]

    print(f"Best Strategy Found: Total Cost ${best['Total_Cost']:,.0f}")
    print(f"Parameters: Budget=${best['Budget']:,.0f} | Rehab_Trigger={best['Rehab_Trigger']:.2f}")

    # 2. Re-Run Simulation with Logging Enabled
    sim_cfg = config.get("simulation", {}) or {}
    try:
        inv_cost, risk_cost, cip_cost, emergency_cost, total_breaks = run_simulation(
            use_mock_data=False,
            override_input_path=optimized_input_path,
            output_dir=output_dir,  # Enable report generation to output directory
            annual_budget=best["Budget"],
            rehab_trigger=best["Rehab_Trigger"],
            generate_report=True,  # <--- TRIGGERS THE REPORT
            seed=sim_cfg.get("seed"),
            n_replicates=int(sim_cfg.get("n_replicates", 1)),
        )

        print(f"Victory lap completed - Investment: ${inv_cost:,.0f}, Risk: ${risk_cost:,.0f}, Breaks: {total_breaks}")

    except Exception as e:
        print(f"Error generating action plan: {e}")

    # --- VALIDATION CURVE GENERATION ---
    print("\n--- Generating Validation Curve ---")
    try:
        pct_replaced_by_number, pct_avoided_by_number, pct_replaced_by_length, pct_avoided_by_length = generate_validation_curve(
            input_file_path=optimized_input_path,
            budget_min=config["genes"]["budget"]["min"],
            budget_max=config["genes"]["budget"]["max"],
            n_points=15,
            n_replicates=int(sim_cfg.get("n_replicates", 1)),
        )

        # Save validation curve plot
        validation_plot_path = os.path.join(output_dir, "validation_curve.png")
        plot_validation_curve(
            pct_replaced_by_number,
            pct_avoided_by_number,
            pct_replaced_by_length,
            pct_avoided_by_length,
            validation_plot_path
        )

        # Save validation data
        validation_data_path = os.path.join(output_dir, "validation_data.csv")
        save_validation_data(
            pct_replaced_by_number,
            pct_avoided_by_number,
            pct_replaced_by_length,
            pct_avoided_by_length,
            validation_data_path
        )

        print("Validation curve generation completed successfully")

    except Exception as e:
        print(f"Error generating validation curve: {e}")

    # Cleanup optimization checkpoint on success
    opt_ckpt.cleanup()
    print("\n=== Optimization Complete ===")


def plot_cost_curves(df, output_dir):
    """
    Generate cost curve visualization (Investment vs Risk costs).
    
    Args:
        df: Results DataFrame with Investment_Cost, Risk_Cost columns
        output_dir: Directory for output files
    """
    # Create pareto front plot
    plt.figure(figsize=(10, 8))
    scatter = plt.scatter(
        df["Investment_Cost"],
        df["Risk_Cost"],
        c=df["Total_Cost"],
        cmap="viridis",
        alpha=0.7
    )
    plt.colorbar(scatter, label="Total Cost ($)")

    # Format and save cost curve plot
    plt.xlabel("Investment Cost ($)")
    plt.ylabel("Risk Cost ($)")
    plt.title("Pareto Frontier: Investment vs Risk Cost")
    plt.grid(True, alpha=0.3)

    cost_curve_path = os.path.join(output_dir, "optimization_curve.png")
    plt.savefig(cost_curve_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Cost curve saved to {cost_curve_path}")


if __name__ == "__main__":
    run_optimization()