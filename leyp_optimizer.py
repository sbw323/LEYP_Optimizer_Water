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

import argparse
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
from leyp_config import NSGA2_CHECKPOINT_EVERY_N_GEN, NSGA2_CHECKPOINT_PATH, SIMULATION_YEARS
from leyp_preprocessor import preprocess_network
from leyp_runner import run_simulation, simulate
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


def add_nominal_costs(df, config, input_path):
    """Add undiscounted (nominal) cost columns to a Pareto front.

    The optimizer's objectives are present values, so nominal totals are not
    in its result. Each front strategy is re-simulated with the seed, replicate
    count and discount rate the optimizer used. With common random numbers
    that reproduces its evaluation exactly; the reproduced present values are
    checked against the stored objectives and the largest difference is kept
    in ``df.attrs["pv_reproduction_error"]``.

    Args:
        df: Front with Budget, Rehab_Trigger, Investment_Cost and Risk_Cost.
        config: Optimizer configuration (simulation and discount_rate keys).
        input_path: Inventory CSV the optimizer evaluated against.

    Returns:
        A copy of ``df`` with Nominal_Investment, Nominal_Risk, Nominal_Total
        and Avg_Annual_Spend (nominal investment per year of the horizon).
    """
    sim = config.get("simulation", {}) or {}
    out = df.copy()
    nominal_inv, nominal_risk, worst = [], [], 0.0
    for _, row in out.iterrows():
        r = simulate(
            input_path,
            annual_budget=float(row["Budget"]),
            rehab_trigger=float(row["Rehab_Trigger"]),
            seed=sim.get("seed"),
            n_replicates=int(sim.get("n_replicates", 1)),
            discount_rate=config.get("discount_rate"),
        )
        worst = max(
            worst,
            abs(r["investment_cost"] - row["Investment_Cost"]),
            abs(r["risk_cost"] - row["Risk_Cost"]),
        )
        nominal_inv.append(r["nominal_investment_cost"])
        nominal_risk.append(r["nominal_risk_cost"])

    out["Nominal_Investment"] = nominal_inv
    out["Nominal_Risk"] = nominal_risk
    out["Nominal_Total"] = out["Nominal_Investment"] + out["Nominal_Risk"]
    out["Avg_Annual_Spend"] = out["Nominal_Investment"] / SIMULATION_YEARS
    out.attrs["pv_reproduction_error"] = worst

    if sim.get("seed") is None:
        print("  [note] simulation.seed is unset, so nominal costs are a fresh draw,"
              " not the optimizer's own evaluation.")
    elif worst > 1.0:
        print(f"  [warning] re-simulation differs from the optimizer's objectives by up to"
              f" ${worst:,.0f}; nominal costs may not match the plotted strategies.")
    return out


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


def run_optimization(cost_curve_only=False):
    """Run the NSGA-II search and write its outputs.

    Args:
        cost_curve_only: Write only the Pareto cost curve
            (pareto_cost_curve.png and pareto_cost_curve_data.csv). The action
            plan, cost summary, diagnostics, nsga2_results.csv and the other
            charts are not written, so files already in the output directory
            from an earlier full run are left as they were.
    """
    print("=== LEYP Genetic Optimizer (NSGA-II) ===")
    if cost_curve_only:
        print("Mode: cost curve only")
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

    print("\n--- Computing nominal costs along the front ---")
    df = add_nominal_costs(df, config, optimized_input_path)
    method = config.get("selection", "min_total_cost")

    if cost_curve_only:
        curve = plot_pareto_cost_curve(df, output_dir, selection=method)
        data_path = os.path.join(output_dir, "pareto_cost_curve_data.csv")
        safe_write_file(data_path, df.to_csv(index=False))
        print(f"Cost curve data saved to {data_path}")
        for label, idx in (("Minimum total cost", curve["min_index"]), ("Knee", curve["knee_index"])):
            row = df.loc[idx]
            print(f"  {label}: budget ${row['Budget']:,.0f}/yr, trigger {row['Rehab_Trigger']:.2f}, "
                  f"average spend ${row['Avg_Annual_Spend']:,.0f}/yr")
        print("  Action plan, validation curve and other outputs were not regenerated.")
        opt_ckpt.cleanup()
        print("\n=== Optimization Complete ===")
        return df

    results_path = os.path.join(output_dir, "nsga2_results.csv")
    safe_write_file(results_path, df.to_csv(index=False))
    print(f"Optimization results saved to {results_path}")

    # Generate cost curve visualizations
    plot_cost_curves(df, output_dir)
    plot_pareto_cost_curve(df, output_dir, selection=method)

    # --- VICTORY LAP: Generate Detailed Schedule for Best Strategy ---
    print("\n--- Generating Optimal Action Plan ---")

    # 1. Identify Best Strategy
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
    return df


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


def _within_tolerance_runs(x, total, tol):
    """Contiguous spans of the front whose total is within ``tol`` of the minimum.

    Args:
        x: Investment at each front point, sorted ascending.
        total: Total cost at each point.
        tol: Fractional tolerance, e.g. 0.05 for 5%.

    Returns:
        (x_start, x_end) for each run of consecutive points within tolerance.
        A run of one point has x_start == x_end.
    """
    x = np.asarray(x, dtype=float)
    ok = np.asarray(total, dtype=float) <= np.min(total) * (1.0 + tol)
    runs, start = [], None
    for i, flag in enumerate(ok):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((x[start], x[i - 1]))
            start = None
    if start is not None:
        runs.append((x[start], x[-1]))
    return runs


def _format_spend(dollars_per_year):
    """$159k/yr or $1.65M/yr, with the dollar sign escaped from mathtext."""
    if dollars_per_year >= 1e6:
        return f"\\${dollars_per_year / 1e6:,.2f}M/yr"
    return f"\\${dollars_per_year / 1e3:,.0f}k/yr"


def plot_pareto_cost_curve(df, output_dir, selection="min_total_cost",
                           band_tolerance=0.05, filename="pareto_cost_curve.png"):
    """Plot nominal 100-year cost against nominal investment along the front.

    Investment, risk and their total are drawn against investment, with the
    span of strategies whose total is within ``band_tolerance`` of the minimum
    shaded. The minimum-total point (star) and the knee (diamond) are labelled
    with their average annual spend: nominal investment over the horizon
    divided by its length, which is the average cash budget the strategy needs.

    "Minimum total cost" here is the lowest point of the plotted *nominal*
    total. The package's ``min_total_cost`` selection rule minimises the
    *present-value* total instead, and the two can be different strategies.
    The configured rule's pick is marked "— selected"; if that is the PV
    minimum and it is neither plotted point, it gets its own marker, so the
    legend never calls a point selected when it was not.

    Args:
        df: Front from add_nominal_costs.
        output_dir: Directory for the PNG.
        selection: The configured selection rule, "min_total_cost" or "knee".
        band_tolerance: Shaded band width as a fraction of the minimum total.
        filename: Output file name.

    Returns:
        Dict with the output path, the index of the plotted minimum, the knee
        and the selected strategy, the shaded runs ($M), and the legend labels.
    """
    required = {"Nominal_Investment", "Nominal_Risk", "Nominal_Total", "Avg_Annual_Spend"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"run add_nominal_costs first; missing {sorted(missing)}")
    if "Ideal_Distance" not in df.columns:
        df = annotate_front(df)

    front = df.sort_values("Nominal_Investment")
    x = front["Nominal_Investment"] / 1e6
    risk = front["Nominal_Risk"] / 1e6
    total = front["Nominal_Total"] / 1e6

    min_idx = front["Nominal_Total"].idxmin()
    knee_idx = select_strategy(df, "knee")
    selected_idx = select_strategy(df, selection)

    # Sized so the saved PNG (tight bbox, 200 dpi) is ~1766 x 1079 px.
    fig, ax = plt.subplots(figsize=(10.4, 4.95))
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(True, color="#e5e5e5", linewidth=0.9)
    ax.set_axisbelow(True)

    runs = _within_tolerance_runs(x.to_numpy(), total.to_numpy(), band_tolerance)
    span = float(x.max() - x.min()) or 1.0
    band = None
    for x0, x1 in runs:
        if x1 - x0 < 0.004 * span:  # keep a one-point run visible
            x0, x1 = x0 - 0.002 * span, x1 + 0.002 * span
        band = ax.axvspan(x0, x1, color="#d9d9d9", linewidth=0, zorder=0)

    inv_line, = ax.plot(x, x, "--", color="tab:blue", alpha=0.6, linewidth=1.6, zorder=2)
    risk_line, = ax.plot(x, risk, "--", color="tab:red", alpha=0.75, linewidth=1.6, zorder=2)
    total_line, = ax.plot(x, total, "-", color="black", linewidth=2.3, zorder=3)

    def point(idx):
        row = front.loc[idx]
        return row["Nominal_Investment"] / 1e6, row["Nominal_Total"] / 1e6, row["Avg_Annual_Spend"]

    def suffix(idx):
        return " — selected" if idx == selected_idx else ""

    mx, my, mspend = point(min_idx)
    kx, ky, kspend = point(knee_idx)
    star = ax.scatter([mx], [my], marker="*", s=380, color="tab:red",
                      edgecolors="black", linewidths=0.8, zorder=5)
    diamond = ax.scatter([kx], [ky], marker="D", s=160, color="tab:blue",
                         edgecolors="black", linewidths=0.8, zorder=6)

    text_style = dict(textcoords="offset points", fontweight="bold", fontsize=10.5,
                      va="bottom", annotation_clip=False)
    knee_offset, knee_ha = ((22, 26), "left") if knee_idx != min_idx else ((-22, 26), "right")
    ax.annotate(f"Knee (balanced)\n{_format_spend(kspend)} average spend", (kx, ky),
                xytext=knee_offset, ha=knee_ha, color="tab:blue", **text_style)
    ax.annotate(f"Minimum total cost\n{_format_spend(mspend)} average spend", (mx, my),
                xytext=(14, 22), ha="left", color="tab:red",
                bbox=dict(boxstyle="round,pad=0.25", facecolor="white",
                          edgecolor="none", alpha=0.85), **text_style)

    handles = [band, inv_line, risk_line, total_line, star, diamond]
    labels = [f"Total within {band_tolerance:.0%} of minimum", "Investment", "Risk",
              "Total (investment + risk)", "Minimum total cost" + suffix(min_idx),
              "Knee (balanced)" + suffix(knee_idx)]

    if selected_idx not in (min_idx, knee_idx):
        sx, sy, sspend = point(selected_idx)
        picked = ax.scatter([sx], [sy], marker="o", s=120, facecolors="white",
                            edgecolors="tab:red", linewidths=2.0, zorder=5)
        ax.annotate(f"Lowest present-value cost\n{_format_spend(sspend)} average spend",
                    (sx, sy), xytext=(14, -14), ha="left", color="tab:red",
                    **{**text_style, "va": "top"})
        handles.append(picked)
        labels.append("Lowest present-value cost — selected")

    ax.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=3,
              frameon=False, fontsize=10, handlelength=2.2, columnspacing=2.5)

    years = SIMULATION_YEARS
    ax.set_title(f"Optimisation output: {years}-year cost against investment along the Pareto front",
                 loc="left", fontsize=13)
    ax.set_xlabel(f"{years}-year investment, nominal (\\$ million)", fontsize=11)
    ax.set_ylabel(f"{years}-year cost, nominal (\\$ million)", fontsize=11)

    path = os.path.join(output_dir, filename)
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Pareto cost curve saved to {path}")
    return {"path": path, "min_index": min_idx, "knee_index": knee_idx,
            "selected_index": selected_idx, "band_runs": runs, "legend_labels": labels}


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="LEYP-Water NSGA-II optimizer: search budget and trigger, then write the "
                    "Pareto front, action plan, validation curve and charts."
    )
    parser.add_argument(
        "--cost-curve-only", action="store_true",
        help="Run the search and write only the Pareto cost curve "
             "(pareto_cost_curve.png and pareto_cost_curve_data.csv). Skips the action plan, "
             "validation curve and other outputs; existing files are left as they were.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    run_optimization(cost_curve_only=args.cost_curve_only)