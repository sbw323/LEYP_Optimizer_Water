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
- Three selection rules (min_total_cost, knee, max_marginal_return), the
  plateau of near-minimum strategies, a report figure and a strategy summary
"""

import argparse
import os

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import yaml
from matplotlib.path import Path
from matplotlib.text import Text
from matplotlib.transforms import Bbox
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
from leyp_config import (
    BACKLOG_CONDITION,
    DISCOUNT_RATE,
    NSGA2_CHECKPOINT_EVERY_N_GEN,
    NSGA2_CHECKPOINT_PATH,
    SIMULATION_YEARS,
)
from leyp_preprocessor import preprocess_network
from leyp_runner import (
    action_plan_frame,
    representative_replicate,
    run_simulation,
    simulate,
)
from water_validation import (
    generate_validation_curve,
    plot_validation_curve,
    save_validation_data,
    summarize_validation,
)

CONFIG_FILE = "optimizer_config.yaml"
OPTIMIZED_INPUT_PATH = "temp_optimization_input.csv"

SELECTION_METHODS = ("min_total_cost", "knee", "max_marginal_return")
MARGINAL_WINDOW = 5          # front points per window when locating the steepest stretch
DEFAULT_PLATEAU_TOLERANCE = 0.05

# Marker, colour, size and display name of each rule's strategy on the charts.
PICK_STYLES = {
    "min_total_cost": ("*", "tab:red", 230, "Minimum total cost"),
    "knee": ("D", "tab:blue", 95, "Knee (balanced)"),
    "max_marginal_return": ("^", "tab:green", 110, "Max marginal return"),
}

# Columns add_nominal_costs adds by re-simulating each front strategy.
REEVALUATION_COLUMNS = (
    "Nominal_Investment", "Nominal_Risk", "Nominal_Total", "Avg_Annual_Spend",
    "Failures", "Emergency_Replaced_Pipes", "Backlog_Pipes", "Backlog_Length",
)


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


def marginal_return(df, window=MARGINAL_WINDOW):
    """Risk avoided per dollar invested along the front.

    Measured over a sliding window of ``window`` consecutive points (sorted by
    investment) and assigned to the window's centre point: single segments of
    an NSGA-II front are too noisy to use directly. A value of 1 means one
    more dollar of investment avoids one dollar of risk.

    Returns:
        Series aligned with ``df``'s index; NaN where no window is centred.
    """
    front = df.sort_values("Investment_Cost", kind="stable")
    inv = front["Investment_Cost"].to_numpy(dtype=float)
    risk = front["Risk_Cost"].to_numpy(dtype=float)
    idx = front.index.to_numpy()
    out = pd.Series(np.nan, index=df.index)
    n = len(front)
    if n < 2:
        return out
    w = max(2, min(int(window), n))
    for i in range(n - w + 1):
        d_inv = inv[i + w - 1] - inv[i]
        if d_inv > 0:
            out[idx[i + w // 2]] = (risk[i] - risk[i + w - 1]) / d_inv
    return out


def select_strategy(df, method="min_total_cost", window=MARGINAL_WINDOW):
    """Choose one strategy from the Pareto front.

    Args:
        df: Front with Investment_Cost, Risk_Cost and Total_Cost.
        method:
            "min_total_cost": lowest investment + risk — where the front's
                slope is -1, so a dollar invested avoids a dollar of risk.
            "knee": closest to the ideal point (least investment, least risk)
                once both objectives are min-max normalised to [0, 1].
            "max_marginal_return": the steepest stretch of the front, where
                each additional dollar invested avoids the most risk (slope
                measured over ``window`` points). A best-value-per-dollar
                point, usually at low investment with high remaining risk —
                not a recommended spend level.
        window: Front points per slope window for max_marginal_return.

    Returns:
        Index of the selected row.
    """
    if method == "min_total_cost":
        return df["Total_Cost"].idxmin()
    if method == "knee":
        norm = []
        for col in ("Investment_Cost", "Risk_Cost"):
            lo, hi = df[col].min(), df[col].max()
            norm.append(pd.Series(0.0, index=df.index) if hi == lo else (df[col] - lo) / (hi - lo))
        return np.sqrt(norm[0] ** 2 + norm[1] ** 2).idxmin()
    if method == "max_marginal_return":
        mr = marginal_return(df, window)
        if mr.isna().all():
            return df["Investment_Cost"].idxmin()
        return mr.idxmax()
    raise ValueError(f"Unknown selection method: {method!r}. Use one of {SELECTION_METHODS}")


def plateau_points(df, tolerance=DEFAULT_PLATEAU_TOLERANCE):
    """Front points whose Total_Cost is within ``tolerance`` of the minimum.

    Total cost is flat over a wide range of budgets near the optimum, so the
    min_total_cost pick is one draw from this set. Points inside it are
    interchangeable to within the tolerance.
    """
    return df[df["Total_Cost"] <= (1.0 + tolerance) * df["Total_Cost"].min()]


def selection_settings(config):
    """The configured selection rule and plateau tolerance, validated."""
    method = config.get("selection", "min_total_cost")
    if method not in SELECTION_METHODS:
        raise ValueError(f"optimizer_config.yaml selection: {method!r} is not one of {SELECTION_METHODS}")
    tol = float(config.get("plateau_tolerance", DEFAULT_PLATEAU_TOLERANCE))
    if tol < 0:
        raise ValueError(f"plateau_tolerance must be >= 0, got {tol}")
    return method, tol


def nominal_view(df):
    """The front with its objective columns replaced by the nominal sums.

    The three selection rules and the plateau can then be applied to nominal
    costs exactly as they are to the objectives.
    """
    missing = {"Nominal_Investment", "Nominal_Risk"} - set(df.columns)
    if missing:
        raise ValueError(f"run add_nominal_costs first; missing {sorted(missing)}")
    out = df.copy()
    out["Investment_Cost"] = df["Nominal_Investment"]
    out["Risk_Cost"] = df["Nominal_Risk"]
    out["Total_Cost"] = df["Nominal_Investment"] + df["Nominal_Risk"]
    return out


def add_nominal_costs(df, config, input_path):
    """Add nominal costs and per-strategy counts to a Pareto front.

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
        A copy of ``df`` with Discount_Rate (the rate of the objectives),
        Nominal_Investment, Nominal_Risk, Nominal_Total, Avg_Annual_Spend
        (nominal investment per year of the horizon), Failures (breaks over
        the horizon), Emergency_Replaced_Pipes, and the inherited backlog
        (Backlog_Pipes, Backlog_Length in ft) — all means over replicates.
    """
    sim = config.get("simulation", {}) or {}
    out = df.copy()
    rows, worst = [], 0.0
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
        rows.append(r)

    # The rate the simulation applied (its default when the config has none).
    out["Discount_Rate"] = float(rows[0]["discount_rate"]) if rows else float(
        config.get("discount_rate") or 0.0)
    out["Nominal_Investment"] = [r["nominal_investment_cost"] for r in rows]
    out["Nominal_Risk"] = [r["nominal_risk_cost"] for r in rows]
    out["Nominal_Total"] = out["Nominal_Investment"] + out["Nominal_Risk"]
    out["Avg_Annual_Spend"] = out["Nominal_Investment"] / SIMULATION_YEARS
    out["Failures"] = [r["total_breaks"] for r in rows]
    out["Emergency_Replaced_Pipes"] = [r["emergency_pipes"] for r in rows]
    out["Backlog_Pipes"] = [r["backlog_pipes"] for r in rows]
    out["Backlog_Length"] = [r["backlog_length"] for r in rows]
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



def _configured_rate(config):
    """The objectives' discount rate: the config's, else the model default."""
    rate = config.get("discount_rate")
    return float(DISCOUNT_RATE if rate is None else rate)


def _prepare_input(config):
    """Preprocess the inventory into OPTIMIZED_INPUT_PATH; return that path."""
    preprocess_network(input_path=config["master_input_file"], output_path=OPTIMIZED_INPUT_PATH,
                       skip_segmentation=config.get("skip_segmentation", False))
    return OPTIMIZED_INPUT_PATH


def run_search(config, input_path, discount_rate=None, checkpoint_path=NSGA2_CHECKPOINT_PATH):
    """One NSGA-II search, returning its front with nominal costs and counts.

    The search is the one a full run performs (algorithm, seeds, genes and
    replicates from the config); only the discount rate of the objectives can
    be overridden. Each rate should checkpoint to its own file, so a resumed
    search can never continue one started at a different rate.

    Returns:
        (front, checkpoint). The front is annotated, sorted by total cost and
        carries the add_nominal_costs columns. The caller removes the
        checkpoint once the front is safely written.
    """
    cfg = dict(config)
    if discount_rate is not None:
        cfg["discount_rate"] = float(discount_rate)
    alg = cfg["algorithm"]

    opt_ckpt = OptimizationCheckpoint(
        checkpoint_path=checkpoint_path,
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

    print(f"Starting Evolution ({alg['n_gen']} gens, objectives "
          f"{_basis_text(_configured_rate(cfg))})...")
    res = minimize(
        Water_LEYP_Problem(cfg, input_path),
        algorithm,
        get_termination("n_gen", alg["n_gen"]),
        callback=opt_ckpt.get_callback(),
        seed=alg["seed"],
        verbose=True,
    )

    # Objectives are present values at the search's discount rate.
    df = pd.DataFrame(res.F, columns=["Investment_Cost", "Risk_Cost"])
    df["Budget"] = res.X[:, 0]
    df["Rehab_Trigger"] = res.X[:, 1]
    df["Total_Cost"] = df["Investment_Cost"] + df["Risk_Cost"]
    df = annotate_front(df).sort_values("Total_Cost", ignore_index=True)

    print("\n--- Computing nominal costs along the front ---")
    return add_nominal_costs(df, cfg, input_path), opt_ckpt


def run_optimization(cost_curve_only=False):
    """Run the NSGA-II search and write its outputs.

    Args:
        cost_curve_only: Write only the nominal Pareto cost curve
            (pareto_cost_curve.png and pareto_cost_curve_data.csv). The action
            plan, cost summary, diagnostics, nsga2_results.csv and the other
            charts are not written, so files already in the output directory
            from an earlier full run are left as they were. No plan export and
            no sensitivity sweep.
    """
    print("=== LEYP Genetic Optimizer (NSGA-II) ===")
    if cost_curve_only:
        print("Mode: cost curve only")
    config = load_config()
    # Checked before the search, not after twenty minutes of it.
    method, plateau_tol = selection_settings(config)
    sens = sensitivity_settings(config)
    export_plans = bool(config.get("export_strategy_plans", True))
    output_dir = config["output_base_dir"]

    os.makedirs(output_dir, exist_ok=True)

    try:
        optimized_input_path = _prepare_input(config)
    except Exception as e:
        print(f"Data Prep Error: {e}")
        return

    df, opt_ckpt = run_search(config, optimized_input_path)

    if cost_curve_only:
        curve = plot_pareto_cost_curve(df, output_dir, selection=method, band_tolerance=plateau_tol)
        data_path = os.path.join(output_dir, "pareto_cost_curve_data.csv")
        safe_write_file(data_path, df.to_csv(index=False))
        print(f"Cost curve data saved to {data_path}")
        for rule, idx in curve["picks"].items():
            row = df.loc[idx]
            print(f"  {PICK_STYLES[rule][3]} (nominal): budget ${row['Budget']:,.0f}/yr, "
                  f"trigger {row['Rehab_Trigger']:.2f}, average spend ${row['Avg_Annual_Spend']:,.0f}/yr")
        print("  Action plan, validation curve and other outputs were not regenerated.")
        opt_ckpt.cleanup()
        print("\n=== Optimization Complete ===")
        return df

    results_path = os.path.join(output_dir, "nsga2_results.csv")
    safe_write_file(results_path, df.to_csv(index=False))
    print(f"Optimization results saved to {results_path}")

    # Charts, the strategy summary and the selection report
    print("\n--- Selecting a strategy ---")
    report = write_report_outputs(df, output_dir, method, plateau_tol, export_plans=export_plans)

    # --- VICTORY LAP: Generate Detailed Schedule for Best Strategy ---
    print("\n--- Generating Optimal Action Plan ---")
    best = df.loc[report["selected_index"]]
    print(f"Best Strategy Found ({method}): Total Cost ${best['Total_Cost']:,.0f}")
    print(f"Parameters: Budget=${best['Budget']:,.0f} | Rehab_Trigger={best['Rehab_Trigger']:.2f}")

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

    if export_plans:
        print("\n--- Exporting one action plan per marked strategy ---")
        export_strategy_plans(df, output_dir, config, optimized_input_path)

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

    # The main run's front is safely written: drop its checkpoint before the
    # sweep, which checkpoints each rate to a file of its own.
    opt_ckpt.cleanup()

    if sens["enabled"]:
        print("\n--- Discount-rate sensitivity sweep ---")
        run_sensitivity(config, optimized_input_path, base_front=df)

    print("\n=== Optimization Complete ===")
    return df


# --- Strategy plan export ----------------------------------------------------------

# One action plan per strategy marked on the optimisation chart, named after
# its call-out.
PLAN_FILES = {
    "min_total_cost": "Action_Plan_Min_Total_Cost.csv",
    "knee": "Action_Plan_Knee_Balanced.csv",
    "max_marginal_return": "Action_Plan_Max_Marginal_Return.csv",
}


def export_strategy_plans(df, output_dir, config, input_path, window=MARGINAL_WINDOW):
    """Write one action plan per rule's strategy (PLAN_FILES).

    Each strategy is re-simulated with the run's seed, replicates and rate,
    and the replicate nearest the mean total cost is written, the same choice
    as Optimal_Action_Plan.csv, so the selected strategy's export is
    identical to it. Rules that pick the same strategy each still get their
    file.

    Returns:
        {rule: path written}.
    """
    sim = config.get("simulation", {}) or {}
    written = {}
    for rule in SELECTION_METHODS:
        row = df.loc[select_strategy(df, rule, window)]
        r = simulate(
            input_path,
            annual_budget=float(row["Budget"]),
            rehab_trigger=float(row["Rehab_Trigger"]),
            seed=sim.get("seed"),
            n_replicates=int(sim.get("n_replicates", 1)),
            discount_rate=config.get("discount_rate"),
        )
        plan = action_plan_frame(representative_replicate(r["replicates"])["actions"])
        path = os.path.join(output_dir, PLAN_FILES[rule])
        safe_write_file(path, plan.to_csv(index=False))
        written[rule] = path
        print(f"  {PICK_STYLES[rule][3]}: budget ${row['Budget']:,.0f}/yr, trigger "
              f"{row['Rehab_Trigger']:.2f} -> {path} ({len(plan)} actions)")
    return written


# --- Discount-rate sensitivity sweep ------------------------------------------------

DEFAULT_SENSITIVITY_RATES = (0.0, 0.03, 0.05)
_RULE_SHORT = {"min_total_cost": "min-total", "knee": "knee", "max_marginal_return": "max-marginal-return"}


def sensitivity_settings(config):
    """The validated `sensitivity` block: enabled, discount_rates, strategy.

    The sweep's strategy is independent of `selection` and defaults to knee.
    """
    sens = config.get("sensitivity") or {}
    rates = sens.get("discount_rates", list(DEFAULT_SENSITIVITY_RATES))
    if not isinstance(rates, (list, tuple)) or not rates:
        raise ValueError("sensitivity.discount_rates must be a non-empty list of rates")
    rates = [float(r) for r in rates]
    if any(not 0.0 <= r < 1.0 for r in rates):
        raise ValueError(f"sensitivity.discount_rates must lie in [0, 1), got {rates}")
    if len({round(r, 12) for r in rates}) != len(rates):
        raise ValueError(f"sensitivity.discount_rates has duplicates: {rates}")
    strategy = sens.get("strategy", "knee")
    if strategy not in SELECTION_METHODS:
        raise ValueError(f"sensitivity.strategy: {strategy!r} is not one of {SELECTION_METHODS}")
    return {"enabled": bool(sens.get("enabled", False)), "discount_rates": rates,
            "strategy": strategy}


def _rate_label(rate):
    """Column heading: Nominal, 3%, 5%."""
    return "Nominal" if rate == 0 else f"{rate * 100:g}%"


def _rate_tag(rate):
    """File-name tag: 0pct, 3pct, 2.5pct."""
    return f"{rate * 100:g}pct"


def _plain_money(dollars):
    """$144k or $1.94M, for tables (no mathtext escape)."""
    return f"${dollars / 1e6:,.2f}M" if dollars >= 1e6 else f"${dollars / 1e3:,.0f}k"


def sensitivity_table(picks, strategy, years=SIMULATION_YEARS):
    """The sensitivity summary: one column per rate, three rows.

    Args:
        picks: One dict per rate, in column order, with rate, annual_budget,
            rehab_trigger, avg_annual_spend, failures and at_budget_ceiling.
        strategy: The rule the picks were made with (labels the rows).

    Returns:
        DataFrame indexed by row label, columns Nominal / 3% / 5% ...
    """
    who = _RULE_SHORT[strategy]
    rows = {
        f"Recommended avg spend ({who} pick)":
            [f"{_plain_money(p['avg_annual_spend'])}/yr" for p in picks],
        f"Failures (breaks) over {years} yr at the {who} pick":
            [f"{p['failures']:,.0f}" for p in picks],
        f"Strategy type at the {who} pick":
            [f"{_plain_money(p['annual_budget'])}/yr budget, replace at condition "
             f"≤ {p['rehab_trigger']:.2f}" + (", at the budget ceiling" if p["at_budget_ceiling"] else "")
             for p in picks],
    }
    table = pd.DataFrame(rows, index=[_rate_label(p["rate"]) for p in picks]).T
    table.index.name = "metric"
    return table


def _markdown(table):
    head = "| | " + " | ".join(table.columns) + " |"
    rule = "|---|" + "---|" * len(table.columns)
    body = ["| " + label + " | " + " | ".join(row) + " |" for label, row in table.iterrows()]
    return "\n".join([head, rule, *body])


def run_sensitivity(config, input_path, base_front=None):
    """Re-optimise at each sensitivity rate and tabulate the chosen strategy.

    The discount rate changes the objectives, so it changes the front and the
    strategy each rule picks; re-pricing one fixed plan would repeat the same
    spend, failures and strategy in every column. A front already searched at
    one of the rates (``base_front``) is reused for that column.

    Writes discount_sensitivity.csv (the summary), discount_sensitivity_detail.csv
    (the picks as numbers) and sensitivity/nsga2_results_rate_<rate>.csv (each
    rate's front) to the output directory, and prints the table as markdown.

    Returns:
        Dict with the summary table, the detail frame and the fronts by rate.
    """
    sens = sensitivity_settings(config)
    strategy = sens["strategy"]
    output_dir = config["output_base_dir"]
    sweep_dir = os.path.join(output_dir, "sensitivity")
    os.makedirs(sweep_dir, exist_ok=True)
    ceiling = float(config["genes"]["budget"]["max"])

    picks, fronts = [], {}
    for rate in sens["discount_rates"]:
        if base_front is not None and abs(_discount_rate(base_front) - rate) < 1e-12:
            front, source = base_front, "reused"
            print(f"\n[{_rate_label(rate)}] reusing the front already searched at this rate")
        else:
            print(f"\n[{_rate_label(rate)}] searching with objectives at {_basis_text(rate)}")
            front, ckpt = run_search(config, input_path, discount_rate=rate,
                                     checkpoint_path=f"nsga2_checkpoint_rate_{_rate_tag(rate)}.pkl")
            source = "searched"
        path = os.path.join(sweep_dir, f"nsga2_results_rate_{_rate_tag(rate)}.csv")
        safe_write_file(path, front.to_csv(index=False))
        if source == "searched":
            ckpt.cleanup()
        fronts[rate] = front

        row = front.loc[select_strategy(front, strategy)]
        picks.append({
            "rate": rate, "rate_label": _rate_label(rate), "source": source, "strategy": strategy,
            "annual_budget": float(row["Budget"]), "rehab_trigger": float(row["Rehab_Trigger"]),
            "at_budget_ceiling": bool(row["Budget"] >= 0.98 * ceiling),
            "avg_annual_spend": float(row["Avg_Annual_Spend"]),
            "failures": float(row["Failures"]),
            "emergency_replaced_pipes": float(row["Emergency_Replaced_Pipes"]),
            "investment_objective": float(row["Investment_Cost"]),
            "risk_objective": float(row["Risk_Cost"]),
            "total_objective": float(row["Total_Cost"]),
            "n_front_points": len(front),
        })

    table = sensitivity_table(picks, strategy)
    detail = pd.DataFrame(picks)
    safe_write_file(os.path.join(output_dir, "discount_sensitivity.csv"), table.to_csv())
    safe_write_file(os.path.join(output_dir, "discount_sensitivity_detail.csv"),
                    detail.to_csv(index=False))
    print(f"\nDiscount-rate sensitivity ({strategy} pick), saved to "
          f"{os.path.join(output_dir, 'discount_sensitivity.csv')}:\n")
    print(_markdown(table))
    if detail["at_budget_ceiling"].any():
        print("\n  [warning] At least one pick sits at the budget gene ceiling; the optimum "
              "at that rate may lie above it.")
    return {"table": table, "detail": detail, "fronts": fronts}


def _verified_saved_front(config, input_path, rates):
    """The saved nsga2_results.csv, if it can stand in for one sweep rate.

    It must exist, have been searched at one of ``rates``, and reproduce
    exactly under the current model and config (every front point is
    re-simulated). Otherwise None, with the reason printed.
    """
    path = os.path.join(config["output_base_dir"], "nsga2_results.csv")
    if not os.path.exists(path):
        print(f"No saved front at {path}; every rate will be searched.")
        return None
    df = pd.read_csv(path)
    rate = float(df["Discount_Rate"].iloc[0]) if "Discount_Rate" in df.columns else _configured_rate(config)
    if not any(abs(rate - r) < 1e-12 for r in rates):
        print(f"Saved front was searched at {_basis_text(rate)}, not a sweep rate; it is not reused.")
        return None
    print(f"Checking the saved front ({len(df)} strategies at {_basis_text(rate)}) still reproduces...")
    checked = add_nominal_costs(df, {**config, "discount_rate": rate}, input_path)
    if checked.attrs["pv_reproduction_error"] > 1.0:
        print("  It does not (different model or configuration); every rate will be searched.")
        return None
    print("  It does; reusing it for that rate.")
    return checked


def run_sensitivity_only():
    """The discount-rate sweep alone: no action plan, charts or validation.

    Reuses the saved front for its rate when it still reproduces, so that
    column agrees with the committed action plan; searches the other rates.
    """
    print("=== LEYP Optimizer: discount-rate sensitivity sweep ===")
    config = load_config()
    sens = sensitivity_settings(config)
    os.makedirs(config["output_base_dir"], exist_ok=True)
    input_path = _prepare_input(config)
    base = _verified_saved_front(config, input_path, sens["discount_rates"])
    result = run_sensitivity(config, input_path, base_front=base)
    print("\n=== Sensitivity sweep complete ===")
    return result


# --- Reporting -----------------------------------------------------------------


def _discount_rate(df):
    """The rate the front's objectives were discounted at (0 = nominal)."""
    if "Discount_Rate" in df.columns and len(df):
        return float(df["Discount_Rate"].iloc[0])
    return 0.0


def _basis_text(rate, convention=False):
    if not rate:
        return "nominal"
    return f"present value at {rate:.1%}" + (" (end-of-year)" if convention else "")


def _nominal_investment(df):
    """Nominal investment over the horizon for every front row.

    Equal to Investment_Cost when the objectives are undiscounted; otherwise
    it must have been added by add_nominal_costs.
    """
    if "Nominal_Investment" in df.columns:
        return df["Nominal_Investment"]
    if _discount_rate(df) == 0.0 and "Discount_Rate" in df.columns:
        return df["Investment_Cost"]
    raise ValueError("run add_nominal_costs first; missing ['Nominal_Investment']")


def _format_spend(dollars_per_year):
    """$159k/yr or $1.65M/yr, with the dollar sign escaped from mathtext."""
    if dollars_per_year >= 1e6:
        return f"\\${dollars_per_year / 1e6:,.2f}M/yr"
    return f"\\${dollars_per_year / 1e3:,.0f}k/yr"


def _format_tolerance(tol):
    return f"{tol * 100:g}%"


def _million_axis(axis, span_m):
    """Ticks in whole $ millions where the range allows, never an offset."""
    if span_m >= 3:
        axis.set_major_locator(mticker.MaxNLocator(integer=True, steps=[1, 2, 2.5, 5, 10]))
        axis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    else:
        axis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:,.2g}"))


# Call-out positions tried for each marker, nearest first: offset in points,
# then horizontal and vertical alignment of the text against that offset.
_CALLOUT_CANDIDATES = [
    ((sx * d, sy * round(d * 0.8)), "left" if sx > 0 else "right", "bottom" if sy > 0 else "top")
    for d in (14, 30, 50, 75, 105, 140)
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1))
]


def _place_callouts(fig, ax, callouts, line_xy, marker_xy):
    """Place each call-out where it overlaps no line, marker or other call-out.

    Candidate positions are tried nearest first around the marker; the first
    with no overlap and inside the axes wins. If none is clear, the one with
    the fewest overlaps is kept. Positions further than the nearest ring get a
    thin leader line back to the marker.

    Returns:
        One dict per call-out: the annotation, its display bbox and its
        number of overlaps (0 when clear).
    """
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    to_display = ax.transData.transform
    paths = [Path(to_display(np.column_stack([x, y]))) for x, y in line_xy]
    pad = 3.0
    marker_r = 9.0 * fig.dpi / 72.0
    marker_boxes = [
        Bbox.from_bounds(px - marker_r, py - marker_r, 2 * marker_r, 2 * marker_r)
        for px, py in (to_display(xy) for xy in marker_xy)
    ]
    axes_box = ax.get_window_extent(renderer)
    placed = []
    for c in callouts:
        best = None
        for n, (offset, ha, va) in enumerate(_CALLOUT_CANDIDATES):
            leader = None
            if n >= 4:
                leader = dict(arrowstyle="-", color=c["color"], linewidth=0.8, alpha=0.7,
                              shrinkA=0, shrinkB=6)
            ann = ax.annotate(
                c["text"], c["xy"], xytext=offset, textcoords="offset points",
                ha=ha, va=va, fontsize=9, fontweight="bold", color=c["color"],
                bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor="none", alpha=0.85),
                arrowprops=leader, zorder=7,
            )
            ann.update_positions(renderer)  # else the extent ignores the offset
            box = Text.get_window_extent(ann, renderer).padded(pad)
            overlaps = sum(p.intersects_bbox(box, filled=False) for p in paths)
            overlaps += sum(box.overlaps(m) for m in marker_boxes)
            overlaps += sum(box.overlaps(p["bbox"]) for p in placed)
            if not (axes_box.contains(box.x0, box.y0) and axes_box.contains(box.x1, box.y1)):
                overlaps += 1
            if best is None or overlaps < best["conflicts"]:
                if best is not None:
                    best["annotation"].remove()
                best = {"annotation": ann, "bbox": box, "conflicts": overlaps, "text": c["text"]}
            else:
                ann.remove()
            if overlaps == 0:
                break
        placed.append(best)
    return placed


def plot_optimization_curve(df, path, selection="min_total_cost",
                            plateau_tolerance=DEFAULT_PLATEAU_TOLERANCE, basis="objective",
                            years=SIMULATION_YEARS, window=MARGINAL_WINDOW):
    """Report figure: horizon cost against investment along the Pareto front.

    Investment (dashed), risk (dashed) and their total (solid) are drawn
    against investment, the front sorted by investment. A shaded band spans
    the investment range of the plateau — the points whose total is within
    ``plateau_tolerance`` of the minimum. The strategies picked by the three
    selection rules are marked, each with a call-out giving its average spend
    per year: nominal investment over the horizon divided by its length,
    never the budget and never a present value.

    Args:
        df: Front from add_nominal_costs (objectives plus nominal columns).
        path: Output PNG path.
        selection: The configured rule; its strategy is flagged "— selected".
        plateau_tolerance: Plateau width as a fraction of the minimum total.
        basis: "objective" plots the objectives (present value at the run's
            rate, or nominal if undiscounted); "nominal" plots the nominal
            sums and applies the three rules to them. On a nominal chart the
            selected strategy (chosen on the objectives) can differ from the
            nominal pick of the same rule; it then gets its own hollow ring.
        years: Horizon length.
        window: Front points per slope window for max_marginal_return.

    Returns:
        Dict with the path, the rules' picks (front index) on this basis, the
        selected index, the plateau index and range, the legend labels, the
        axis labels and the call-outs with their overlap counts.
    """
    spend = _nominal_investment(df) / years
    selected_idx = select_strategy(df, selection, window)
    if basis == "nominal":
        view, basis_text = nominal_view(df), "nominal"
    elif basis == "objective":
        view, basis_text = df, _basis_text(_discount_rate(df))
    else:
        raise ValueError(f"basis must be 'objective' or 'nominal', got {basis!r}")

    picks = {m: select_strategy(view, m, window) for m in SELECTION_METHODS}
    plateau = plateau_points(view, plateau_tolerance)
    front = view.sort_values("Investment_Cost", kind="stable")
    x = (front["Investment_Cost"] / 1e6).to_numpy()
    risk = (front["Risk_Cost"] / 1e6).to_numpy()
    total = (front["Total_Cost"] / 1e6).to_numpy()

    fig, ax = plt.subplots(figsize=(9, 5.6))
    span = float(x.max() - x.min()) or 1.0
    p0, p1 = plateau["Investment_Cost"].min() / 1e6, plateau["Investment_Cost"].max() / 1e6
    if p1 - p0 < 0.006 * span:  # one point: keep the band visible
        p0, p1 = p0 - 0.003 * span, p1 + 0.003 * span
    band = ax.axvspan(p0, p1, color="0.85", linewidth=0, zorder=0)
    inv_line, = ax.plot(x, x, "--", color="tab:blue", alpha=0.6, linewidth=1.2, zorder=2)
    risk_line, = ax.plot(x, risk, "--", color="tab:red", alpha=0.7, linewidth=1.2, zorder=2)
    total_line, = ax.plot(x, total, "-", color="black", linewidth=2, zorder=3)

    def at(idx):
        return view.loc[idx, "Investment_Cost"] / 1e6, view.loc[idx, "Total_Cost"] / 1e6

    handles = [band, inv_line, risk_line, total_line]
    labels = [f"Total within {_format_tolerance(plateau_tolerance)} of minimum", "Investment", "Risk",
              "Total (investment + risk)"]
    marker_xy, groups = [], {}
    for m in SELECTION_METHODS:
        marker, colour, size, name = PICK_STYLES[m]
        idx = picks[m]
        flagged = m == selection and idx == selected_idx
        handles.append(ax.scatter(*at(idx), marker=marker, c=colour, s=size, edgecolors="black",
                                  linewidths=0.8, zorder=5))
        labels.append(name + (" — selected" if flagged else ""))
        marker_xy.append(at(idx))
        groups.setdefault(idx, []).append((name, colour))

    if picks[selection] != selected_idx:
        # Nominal chart only: the selected strategy is not this rule's nominal pick.
        name, colour = PICK_STYLES[selection][3], PICK_STYLES[selection][1]
        handles.append(ax.scatter(*at(selected_idx), marker="o", s=120, facecolors="white",
                                  edgecolors=colour, linewidths=2.0, zorder=5))
        labels.append(f"{name}, present value — selected")
        marker_xy.append(at(selected_idx))
        groups.setdefault(selected_idx, []).append((f"{name}, present value", colour))

    title = f"Optimisation output: {years}-year cost against investment along the Pareto front"
    xlabel = f"{years}-year investment, {basis_text} ($ million)"
    ylabel = f"{years}-year cost, {basis_text} ($ million)"
    ax.set_title(title, fontsize=12, loc="left")
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    _million_axis(ax.xaxis, span)
    _million_axis(ax.yaxis, float(max(total.max(), risk.max(), x.max())))
    ax.grid(True, color="0.9")
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=3,
              frameon=False, fontsize=9, handlelength=1.8, columnspacing=1.6)
    ax.set_xlim(ax.get_xlim())
    ax.set_ylim(ax.get_ylim())
    fig.tight_layout()

    # One call-out per marked strategy; rules that pick the same strategy share it.
    callouts = []
    for idx, members in groups.items():
        names = " = ".join(n for n, _ in members)
        callouts.append({"xy": at(idx), "color": members[0][1],
                         "text": f"{names}\n{_format_spend(spend[idx])} average spend"})
    placed = _place_callouts(fig, ax, callouts,
                             [(x, x), (x, risk), (x, total)], marker_xy)

    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Chart saved to {path}")
    return {
        "path": path, "basis": basis_text, "picks": picks, "selected_index": selected_idx,
        "plateau_index": list(plateau.index),
        "plateau_range": (plateau["Investment_Cost"].min(), plateau["Investment_Cost"].max()),
        "legend_labels": labels, "title": title, "xlabel": xlabel, "ylabel": ylabel,
        "callouts": [{"text": p["text"], "conflicts": p["conflicts"]} for p in placed],
        "callout_conflicts": sum(p["conflicts"] for p in placed),
    }


def plot_pareto_cost_curve(df, output_dir, selection="min_total_cost",
                           band_tolerance=DEFAULT_PLATEAU_TOLERANCE,
                           filename="pareto_cost_curve.png", window=MARGINAL_WINDOW):
    """The report figure on a nominal basis (undiscounted constant dollars).

    Same layout as optimization_curve.png; the plateau and the three rules are
    applied to the nominal sums. See plot_optimization_curve.
    """
    return plot_optimization_curve(df, os.path.join(output_dir, filename), selection=selection,
                                   plateau_tolerance=band_tolerance, basis="nominal",
                                   window=window)


def plot_pareto_front(df, output_dir, filename="pareto_front.png"):
    """Plot the Pareto front itself: risk against investment, with the picks.

    The front is drawn in order of investment so it reads as a trade-off curve,
    coloured by total cost. The three rules' strategies and the ideal point
    are marked, with a dashed line from the ideal point to the knee so
    "closest to ideal" is visible rather than asserted. Axes are present
    values at the run's rate.
    """
    if "Ideal_Distance" not in df.columns:
        df = annotate_front(df)
    basis = _basis_text(_discount_rate(df))
    front = df.sort_values("Investment_Cost")
    inv_m, risk_m = front["Investment_Cost"] / 1e6, front["Risk_Cost"] / 1e6

    fig, ax = plt.subplots(figsize=(11, 8))
    ax.plot(inv_m, risk_m, "-", color="0.55", linewidth=1, alpha=0.8, zorder=1)
    scatter = ax.scatter(
        inv_m, risk_m, c=front["Total_Cost"] / 1e6, cmap="viridis",
        s=42, alpha=0.9, zorder=2, edgecolors="white", linewidths=0.5,
    )
    fig.colorbar(scatter, ax=ax, label=f"Total cost, {basis} (\\$M)")

    ideal = (df["Investment_Cost"].min() / 1e6, df["Risk_Cost"].min() / 1e6)
    ax.plot(*ideal, "x", color="0.3", markersize=9, markeredgewidth=1.5, zorder=3)
    ax.annotate("ideal point", ideal, xytext=(8, -14), textcoords="offset points",
                fontsize=8, color="0.3")

    offsets = {"min_total_cost": (12, 12), "knee": (-46, 10), "max_marginal_return": (12, -16)}
    for method in SELECTION_METHODS:
        marker, colour, size, _ = PICK_STYLES[method]
        row = df.loc[select_strategy(df, method)]
        x, y = row["Investment_Cost"] / 1e6, row["Risk_Cost"] / 1e6
        ax.scatter([x], [y], marker=marker, s=size * 1.05, color=colour,
                   edgecolors="black", linewidths=0.8, zorder=4,
                   # Dollar signs are escaped: matplotlib otherwise reads them
                   # as mathtext delimiters and italicises the text between.
                   label=(f"{method}: budget \\${row['Budget']/1e6:.2f}M/yr, "
                          f"trigger {row['Rehab_Trigger']:.2f}, "
                          f"total \\${row['Total_Cost']/1e6:.1f}M"))
        ax.annotate(method, (x, y), xytext=offsets[method], textcoords="offset points",
                    fontsize=9, color=colour, fontweight="bold")
        if method == "knee":
            ax.plot([ideal[0], x], [ideal[1], y], "--", color="tab:blue",
                    linewidth=1, alpha=0.6, zorder=3)

    ax.set_xlabel(f"Investment cost, {basis} (\\$M)")
    ax.set_ylabel(f"Risk cost, {basis} (\\$M)")
    ax.set_title("Pareto front: investment vs risk, with strategy picks")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.95)

    path = os.path.join(output_dir, filename)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Pareto front saved to {path}")
    return path


def _range(series, fmt="{:.0f}"):
    return f"{fmt.format(series.min())}-{fmt.format(series.max())}"


def write_strategy_summary(df, path, selection="min_total_cost",
                           plateau_tolerance=DEFAULT_PLATEAU_TOLERANCE,
                           years=SIMULATION_YEARS, window=MARGINAL_WINDOW, plan_files=None):
    """One row per rule's strategy plus one for the plateau.

    Holds what report text quotes so the figure can stay uncluttered.
    ``basis`` says what the investment, risk and total columns are (present
    value at the run's rate, or nominal); the nominal investment and the
    average annual spend are always nominal. The risk avoided per dollar is
    the windowed slope of the front (about 1 at the minimum-total point).
    With ``plan_files`` ({rule: file name}) a plan_file column names each
    strategy's exported action plan.

    Returns:
        The summary DataFrame, also written to ``path``.
    """
    rate = _discount_rate(df)
    basis = _basis_text(rate, convention=True)
    nominal_inv = _nominal_investment(df)
    mr = marginal_return(df, window)
    plateau = plateau_points(df, plateau_tolerance)
    h = f"{years}yr"

    def col(row, name):
        return row[name] if name in row.index else np.nan

    rows = []
    for m in SELECTION_METHODS:
        idx = select_strategy(df, m, window)
        row = df.loc[idx]
        rows.append({
            "strategy": m, "selected": m == selection, "basis": basis,
            "risk_avoided_per_dollar_invested": mr.get(idx, np.nan),
            "annual_budget": row["Budget"], "rehab_trigger": row["Rehab_Trigger"],
            f"investment_{h}": row["Investment_Cost"], f"risk_{h}": row["Risk_Cost"],
            f"total_{h}": row["Total_Cost"],
            f"investment_{h}_nominal": nominal_inv[idx],
            "avg_annual_spend": nominal_inv[idx] / years,
            f"failures_{h}": col(row, "Failures"),
            f"emergency_replaced_pipes_{h}": col(row, "Emergency_Replaced_Pipes"),
            "backlog_pipes": col(row, "Backlog_Pipes"),
            "backlog_length_ft": col(row, "Backlog_Length"),
        })
        if plan_files:
            rows[-1]["plan_file"] = plan_files[m]
    rows.append({
        "strategy": f"plateau (total within {_format_tolerance(plateau_tolerance)} of minimum)",
        "selected": False, "basis": basis,
        "annual_budget": _range(plateau["Budget"]),
        "rehab_trigger": _range(plateau["Rehab_Trigger"], "{:.2f}"),
        f"investment_{h}": _range(plateau["Investment_Cost"]),
        f"total_{h}": _range(plateau["Total_Cost"]),
        f"investment_{h}_nominal": _range(nominal_inv[plateau.index]),
        "avg_annual_spend": _range(nominal_inv[plateau.index] / years),
        "n_front_points": len(plateau),
    })
    summary = pd.DataFrame(rows)
    safe_write_file(path, summary.to_csv(index=False))
    print(f"Strategy summary saved to {path}")
    return summary


def write_report_outputs(df, output_dir, selection="min_total_cost",
                         plateau_tolerance=DEFAULT_PLATEAU_TOLERANCE, window=MARGINAL_WINDOW,
                         export_plans=False):
    """Charts, strategy summary and console report for a front.

    Writes optimization_curve.png (objective basis), pareto_cost_curve.png
    (nominal), pareto_front.png (risk against investment) and
    selected_strategies.csv. Used by a full run and by --plot-only.

    Returns:
        Dict with the picks, the selected index, the plateau, the summary and
        the two cost-curve results from plot_optimization_curve.
    """
    picks = {m: select_strategy(df, m, window) for m in SELECTION_METHODS}
    plateau = plateau_points(df, plateau_tolerance)
    basis = _basis_text(_discount_rate(df))
    spend = _nominal_investment(df) / SIMULATION_YEARS

    print(f"Selection rule: {selection}")
    for m, idx in picks.items():
        row = df.loc[idx]
        tag = " (selected)" if m == selection else ""
        print(f"  {m + tag:<32} budget ${row['Budget']:>11,.0f}/yr  trigger {row['Rehab_Trigger']:.2f}  "
              f"total ${row['Total_Cost'] / 1e6:,.2f}M ({basis})  "
              f"average spend ${spend[idx]:,.0f}/yr")
    print(f"Plateau (total within {_format_tolerance(plateau_tolerance)} of minimum): "
          f"{len(plateau)} front points, budgets ${plateau['Budget'].min():,.0f}-"
          f"${plateau['Budget'].max():,.0f}/yr, investment ${plateau['Investment_Cost'].min():,.0f}-"
          f"${plateau['Investment_Cost'].max():,.0f} ({basis})")
    best = df.loc[picks[selection]]
    if "Backlog_Pipes" in df.columns and best["Backlog_Pipes"] > 0:
        print(f"Inherited backlog (reported, not optimised): {best['Backlog_Pipes']:.0f} pipes, "
              f"{best['Backlog_Length']:,.0f} ft at condition <= {BACKLOG_CONDITION:g} in year 1")

    curve = plot_optimization_curve(df, os.path.join(output_dir, "optimization_curve.png"),
                                    selection, plateau_tolerance, window=window)
    nominal = plot_pareto_cost_curve(df, output_dir, selection, plateau_tolerance, window=window)
    plot_pareto_front(df, output_dir)
    summary = write_strategy_summary(df, os.path.join(output_dir, "selected_strategies.csv"),
                                     selection, plateau_tolerance, window=window,
                                     plan_files=PLAN_FILES if export_plans else None)
    return {"picks": picks, "selected_index": picks[selection], "plateau": plateau,
            "summary": summary, "curve": curve, "nominal_curve": nominal}


def replot():
    """Redraw the charts and summary from the saved front without re-optimising.

    Reads nsga2_results.csv from the output directory and applies the config's
    current ``selection`` and ``plateau_tolerance``. A front saved before the
    nominal and count columns existed is re-simulated once (exact with the
    configured seed) and saved back with them. With export_strategy_plans on,
    the per-strategy action plans are re-simulated and written (no search).
    Optimal_Action_Plan.csv, the cost summary, diagnostics and validation
    curve need a full run and are not touched.
    """
    print("=== LEYP Optimizer: redraw from the saved front ===")
    config = load_config()
    method, plateau_tol = selection_settings(config)
    export_plans = bool(config.get("export_strategy_plans", True))
    output_dir = config["output_base_dir"]
    results_path = os.path.join(output_dir, "nsga2_results.csv")
    if not os.path.exists(results_path):
        raise FileNotFoundError(f"{results_path} not found; run a full optimisation first")
    df = pd.read_csv(results_path)

    missing = [c for c in REEVALUATION_COLUMNS + ("Discount_Rate",) if c not in df.columns]
    if missing:
        if (config.get("simulation") or {}).get("seed") is None:
            raise ValueError(f"{results_path} lacks {missing} and simulation.seed is unset, so the "
                             "front cannot be re-simulated exactly; run a full optimisation")
        print(f"Saved front lacks {missing}; re-simulating its {len(df)} strategies...")
        if not os.path.exists(OPTIMIZED_INPUT_PATH):
            _prepare_input(config)
        df = add_nominal_costs(df, config, OPTIMIZED_INPUT_PATH)
        error = df.attrs["pv_reproduction_error"]
        if error > 1.0:
            raise RuntimeError(
                f"Re-simulation differs from the saved objectives by up to ${error:,.0f}: the saved "
                "front came from a different model or configuration. Run a full optimisation.")
        safe_write_file(results_path, df.to_csv(index=False))
        print(f"  Reproduced the saved objectives exactly; added the columns to {results_path}")

    report = write_report_outputs(df, output_dir, method, plateau_tol, export_plans=export_plans)
    if export_plans:
        print("\n--- Exporting one action plan per marked strategy ---")
        if not os.path.exists(OPTIMIZED_INPUT_PATH):
            _prepare_input(config)
        report["plan_files"] = export_strategy_plans(df, output_dir, config, OPTIMIZED_INPUT_PATH)
    print("Optimal_Action_Plan.csv, cost_summary.csv, simulation_diagnostics.csv and the validation "
          "curve were not regenerated: they describe the strategy chosen by the last full run. "
          "Run a full optimisation if `selection` has changed since.")
    return report


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="LEYP-Water NSGA-II optimizer: search budget and trigger, then write the "
                    "Pareto front, action plan, validation curve and charts."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--cost-curve-only", action="store_true",
        help="Run the search and write only the nominal Pareto cost curve "
             "(pareto_cost_curve.png and pareto_cost_curve_data.csv). Skips the action plan, "
             "validation curve and other outputs; existing files are left as they were.",
    )
    mode.add_argument(
        "--plot-only", action="store_true",
        help="No search: redraw optimization_curve.png, pareto_cost_curve.png, pareto_front.png "
             "and selected_strategies.csv from the saved nsga2_results.csv with the config's "
             "current selection and plateau_tolerance, and the per-strategy action plans if "
             "export_strategy_plans is on. Optimal_Action_Plan.csv is not regenerated.",
    )
    mode.add_argument(
        "--sensitivity-only", action="store_true",
        help="Run only the discount-rate sensitivity sweep (optimizer_config.yaml "
             "sensitivity block): re-optimise at each rate and write discount_sensitivity.csv. "
             "Reuses the saved front for its rate when it still reproduces.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.plot_only:
        replot()
    elif args.sensitivity_only:
        run_sensitivity_only()
    else:
        run_optimization(cost_curve_only=args.cost_curve_only)
