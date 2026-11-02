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
from water_validation import (
    generate_validation_curve,
    plot_validation_curve,
    save_validation_data,
    summarize_validation,
)

CONFIG_FILE = "optimizer_config.yaml"


def annotate_front(df):
    """Add normalised objectives and distance to the ideal point.

    Both objectives are dollars, so Total_Cost is a meaningful scalar. It is
    still only one reading of a two-objective result, so the front is
    annotated with each solution's normalised position and its distance to
    the ideal point, making the investment/risk trade-off visible rather than
    collapsing it silently.
    """
    out = df.copy()
    for col, name in (("Investment_Cost", "Norm_Investment"), ("Risk_Cost", "Norm_Risk")):
        lo, hi = out[col].min(), out[col].max()
        out[name] = 0.0 if hi == lo else (out[col] - lo) / (hi - lo)
    out["Ideal_Distance"] = np.sqrt(out["Norm_Investment"] ** 2 + out["Norm_Risk"] ** 2)
    return out


def select_strategy(df, method="min_total_cost"):
    """Choose one strategy from the Pareto front.

    Args:
        df: Annotated front from annotate_front.
        method: "min_total_cost" picks the lowest total lifecycle cost.
            "knee" picks the solution closest to the ideal point once both
            objectives are normalised — the best-balanced trade-off, which
            does not assume the two dollar streams are interchangeable.

    Returns:
        Index of the selected row.
    """
    if method == "knee":
        return df["Ideal_Distance"].idxmin()
    if method != "min_total_cost":
        raise ValueError(f"Unknown selection method: {method}")
    return df["Total_Cost"].idxmin()


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
        self.discount_rate = config.get("discount_rate")

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
                discount_rate=self.discount_rate,
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
    # Objectives are present values at config["discount_rate"].
    cols = ["Investment_Cost", "Risk_Cost"]
    df = pd.DataFrame(res.F, columns=cols)
    df["Budget"] = res.X[:, 0]
    df["Rehab_Trigger"] = res.X[:, 1]
    df["Total_Cost"] = df["Investment_Cost"] + df["Risk_Cost"]
    df = annotate_front(df).sort_values("Total_Cost", ignore_index=True)

    results_path = os.path.join(output_dir, "nsga2_results.csv")
    safe_write_file(results_path, df.to_csv(index=False))
    print(f"Optimization results saved to {results_path}")

    # Generate cost curve visualization
    plot_cost_curves(df, output_dir)

    # --- VICTORY LAP: Generate Detailed Schedule for Best Strategy ---
    print("\n--- Generating Optimal Action Plan ---")

    # 1. Identify Best Strategy
    method = config.get("selection", "min_total_cost")
    best_idx = select_strategy(df, method)
    best = df.loc[best_idx]

    print(f"Selection rule: {method}")
    print(f"Best Strategy Found: Total Cost ${best['Total_Cost']:,.0f}")
    print(f"Parameters: Budget=${best['Budget']:,.0f} | Rehab_Trigger={best['Rehab_Trigger']:.2f}")

    # Report the alternative reading so the trade-off stays visible.
    other = "knee" if method == "min_total_cost" else "min_total_cost"
    alt = df.loc[select_strategy(df, other)]
    if not alt.equals(best):
        print(
            f"  ({other} would pick Budget=${alt['Budget']:,.0f}, "
            f"Trigger={alt['Rehab_Trigger']:.2f}, Total ${alt['Total_Cost']:,.0f})"
        )

    if best["Budget"] >= config["genes"]["budget"]["max"] * 0.98:
        print(
            "  [warning] The selected budget sits at the upper gene bound; the "
            "true optimum may lie above it. Consider raising genes.budget.max."
        )

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
            discount_rate=config.get("discount_rate"),
        )

        print(f"Victory lap completed - Investment: ${inv_cost:,.0f}, Risk: ${risk_cost:,.0f}, Breaks: {total_breaks}")

    except Exception as e:
        print(f"Error generating action plan: {e}")

    # --- VALIDATION CURVE GENERATION ---
    print("\n--- Generating Validation Curve ---")
    try:
        curve = generate_validation_curve(
            input_file_path=optimized_input_path,
            budget_min=0.0,
            budget_max=config["genes"]["budget"]["max"],
            n_points=config.get("validation", {}).get("n_points", 15),
            n_replicates=int(sim_cfg.get("n_replicates", 1)),
        )

        plot_validation_curve(curve, os.path.join(output_dir, "validation_curve.png"))
        save_validation_data(curve, os.path.join(output_dir, "validation_data.csv"))

        checks = summarize_validation(curve)
        print("  Curve reach: %.1f%% of pipes, %.1f%% of network length"
              % (checks["max_pct_replaced_by_number"], checks["max_pct_replaced_by_length"]))
        print("  Above diagonal over first 50%%: count %s, length %s"
              % (checks["above_diagonal_first_50_by_number"],
                 checks["above_diagonal_first_50_by_length"]))
        print("Validation curve generation completed successfully")

    except Exception as e:
        print(f"Error generating validation curve: {e}")

    # Cleanup optimization checkpoint on success
    opt_ckpt.cleanup()
    print("\n=== Optimization Complete ===")


def plot_cost_curves(df, output_dir):
    """Plot the Pareto front with both strategy picks and the ideal point marked.

    The front is drawn in order of investment so it reads as a trade-off curve.
    The ``min_total_cost`` pick (lowest investment + risk) and the ``knee`` pick
    (closest to the ideal point once both axes are normalised) are both marked,
    with a dashed line from the ideal point to the knee so "closest to ideal" is
    visible rather than asserted. Axes are in $M, present value.

    Args:
        df: Results DataFrame with Investment_Cost, Risk_Cost, Budget,
            Rehab_Trigger and Total_Cost (annotated with Ideal_Distance if not
            already).
        output_dir: Directory for output files.
    """
    if "Ideal_Distance" not in df.columns:
        df = annotate_front(df)
    front = df.sort_values("Investment_Cost")
    inv_m, risk_m = front["Investment_Cost"] / 1e6, front["Risk_Cost"] / 1e6

    fig, ax = plt.subplots(figsize=(11, 8))
    ax.plot(inv_m, risk_m, "-", color="0.55", linewidth=1, alpha=0.8, zorder=1)
    scatter = ax.scatter(
        inv_m, risk_m, c=front["Total_Cost"] / 1e6, cmap="viridis",
        s=42, alpha=0.9, zorder=2, edgecolors="white", linewidths=0.5,
    )
    fig.colorbar(scatter, ax=ax, label="Total cost, PV ($M)")

    # Ideal point and the two picks.
    ideal = (df["Investment_Cost"].min() / 1e6, df["Risk_Cost"].min() / 1e6)
    ax.plot(*ideal, "x", color="0.3", markersize=9, markeredgewidth=1.5, zorder=3)
    ax.annotate("ideal point", ideal, xytext=(8, -14), textcoords="offset points",
                fontsize=8, color="0.3")

    picks = (
        ("min_total_cost", "*", "tab:red", 240, (12, 12)),
        ("knee", "D", "tab:blue", 90, (-46, 10)),
    )
    for method, marker, colour, size, offset in picks:
        row = df.loc[select_strategy(df, method)]
        x, y = row["Investment_Cost"] / 1e6, row["Risk_Cost"] / 1e6
        ax.scatter([x], [y], marker=marker, s=size, color=colour,
                   edgecolors="black", linewidths=0.8, zorder=4,
                   # Dollar signs are escaped: matplotlib otherwise reads them
                   # as mathtext delimiters and italicises the text between.
                   label=(f"{method}: budget \\${row['Budget']/1e6:.2f}M/yr, "
                          f"trigger {row['Rehab_Trigger']:.2f}, "
                          f"total \\${row['Total_Cost']/1e6:.1f}M"))
        ax.annotate(method, (x, y), xytext=offset, textcoords="offset points",
                    fontsize=9, color=colour, fontweight="bold")
        if method == "knee":
            ax.plot([ideal[0], x], [ideal[1], y], "--", color="tab:blue",
                    linewidth=1, alpha=0.6, zorder=3)

    ax.set_xlabel("Investment cost, PV ($M)")
    ax.set_ylabel("Risk cost, PV ($M)")
    ax.set_title("Pareto front: investment vs risk, with strategy picks")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.95)

    cost_curve_path = os.path.join(output_dir, "optimization_curve.png")
    fig.savefig(cost_curve_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Cost curve saved to {cost_curve_path}")


if __name__ == "__main__":
    run_optimization()