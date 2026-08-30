"""
water_validation.py

Validation curve generation for water main replacement planning.

Plots how much of the network a budget renews against how much failure that
renewal avoids, and compares the result to the diagonal a non-prioritising
(random) programme would trace. A model that ranks pipes well sits above the
diagonal over the early part of the curve.

Two panels, measured on genuinely different bases:

  count-based  x = % of pipes proactively replaced
               y = % of breaks avoided
  length-based x = % of network length proactively replaced
               y = % of emergency-replacement length avoided

Both y axes measure what the CIP programme buys, so the reference is a
reactive-only policy: no planned replacement, but failures still repaired and
replaced as they occur. That is the real counterfactual a utility faces, and
it makes the y axes attributable to the proactive replacement on the x axis.

Measuring instead against a true no-intervention run (no emergency response
either) credits the CIP programme with everything emergency response already
achieves: on this network that alone avoids ~86% of no-intervention breaks, so
the curve would start at 86% on a zero budget and clear the diagonal without
the CIP programme doing anything. That figure is still reported, as context
for how much of the outcome reactive work alone delivers.
"""

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from checkpoint import safe_write_file
from leyp_runner import simulate

# Fixed across every budget point so the only variable is the budget level.
VALIDATION_SEED = 12345
VALIDATION_TRIGGER = 3.5


def _pct(part: float, whole: float) -> float:
    """Percentage, clamped to [0, 100], safe when the denominator is zero."""
    if whole <= 0:
        return 0.0
    return float(min(100.0, max(0.0, 100.0 * part / whole)))


def _avoided(baseline: float, current: float) -> float:
    """Percentage of a baseline quantity avoided."""
    if baseline <= 0:
        return 0.0
    return float(min(100.0, max(0.0, 100.0 * (baseline - current) / baseline)))


def generate_validation_curve(
    input_file_path: str,
    budget_min: float = 0.0,
    budget_max: float = 2_000_000.0,
    n_points: int = 15,
    n_replicates: int = 5,
) -> pd.DataFrame:
    """Sweep the budget and measure renewal coverage against failure avoided.

    Args:
        input_file_path: Path to the pipe inventory CSV.
        budget_min: Lowest annual budget to test ($); included as the
            reactive-only reference point.
        budget_max: Highest annual budget to test ($). Must be high enough for
            the sweep to reach full network coverage, or the curve cannot be
            read over the range the acceptance test asks about.
        n_points: Number of budget points.
        n_replicates: Stochastic replicates averaged at each point.

    Returns:
        DataFrame with one row per budget point: the budget, both x measures,
        both y measures, and the cross-replicate standard deviation of each y.
    """
    print(f"Generating validation curve with {n_points} budget points...")

    print("Running reactive-only baseline (no CIP, failures still answered)...")
    baseline = simulate(
        input_path=input_file_path,
        annual_budget=0.0,
        rehab_trigger=VALIDATION_TRIGGER,
        seed=VALIDATION_SEED,
        n_replicates=n_replicates,
    )
    baseline_breaks = baseline["total_breaks"]
    baseline_emergency_length = baseline["emergency_replacement_length"]
    total_pipes = baseline["n_pipes"]
    total_length = baseline["network_length"]

    # Context only: how much reactive work alone already achieves, relative to
    # doing nothing at all. Not the curve's denominator — see module docstring.
    print("Running no-intervention reference (context only)...")
    no_intervention_breaks = simulate(
        input_path=input_file_path,
        annual_budget=0.0,
        rehab_trigger=VALIDATION_TRIGGER,
        seed=VALIDATION_SEED,
        n_replicates=n_replicates,
        no_intervention=True,
    )["total_breaks"]
    print(
        "  reactive-only response alone avoids %.1f%% of no-intervention breaks"
        % _avoided(no_intervention_breaks, baseline_breaks)
    )

    # Coverage is steeply non-linear in budget: on this network the first
    # $150k/yr already renews ~60% of pipes over the horizon.  Linear spacing
    # therefore leaves the low-coverage half of the curve almost unsampled,
    # which is exactly the region the acceptance test asks about.  Log spacing
    # puts points where the curve actually moves.
    budgets = [budget_min] + list(
        np.geomspace(max(budget_min, budget_max / 400.0), budget_max, n_points - 1)
    )

    rows = []
    for i, budget in enumerate(budgets):
        print(f"Testing budget {i + 1}/{n_points}: ${budget:,.0f}")
        run = simulate(
            input_path=input_file_path,
            annual_budget=float(budget),
            rehab_trigger=VALIDATION_TRIGGER,
            seed=VALIDATION_SEED,
            n_replicates=n_replicates,
        )
        reps = run["replicates"]

        rows.append(
            {
                "budget": float(budget),
                # Counted from the action log, not inferred from an assumed
                # average pipe (review finding E1).
                "pct_replaced_by_number": _pct(run["cip_pipes"], total_pipes),
                "pct_avoided_by_number": _avoided(baseline_breaks, run["total_breaks"]),
                "pct_replaced_by_length": _pct(run["cip_pipe_length"], total_length),
                "pct_avoided_by_length": _avoided(
                    baseline_emergency_length, run["emergency_replacement_length"]
                ),
                # Spread across replicates, so the plot can show uncertainty
                # instead of hiding it behind an imposed monotonic filter.
                "sd_avoided_by_number": float(
                    np.std([_avoided(baseline_breaks, r["total_breaks"]) for r in reps])
                ),
                "sd_avoided_by_length": float(
                    np.std([
                        _avoided(baseline_emergency_length, r["emergency_replacement_length"])
                        for r in reps
                    ])
                ),
                "no_intervention_breaks": no_intervention_breaks,
                "reactive_only_breaks": baseline_breaks,
            }
        )

    print("Validation curve generation completed")
    return pd.DataFrame(rows)


def _panel(ax, x, y, sd, xlabel, ylabel, title, colour):
    """Draw one validation panel with its spread band and diagonal."""
    order = np.argsort(x)
    x, y, sd = np.asarray(x)[order], np.asarray(y)[order], np.asarray(sd)[order]

    ax.plot([0, 100], [0, 100], "r--", linewidth=1, alpha=0.7,
            label="No prioritisation (diagonal)")
    ax.fill_between(x, np.clip(y - sd, 0, 100), np.clip(y + sd, 0, 100),
                    color=colour, alpha=0.20, linewidth=0,
                    label="±1 sd across replicates")
    ax.plot(x, y, "-o", color=colour, linewidth=2, markersize=4,
            label="Model performance")

    ax.axvspan(0, 50, color="grey", alpha=0.07, zorder=0)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="lower right", fontsize=8)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)


def plot_validation_curve(curve: pd.DataFrame, output_path: str) -> None:
    """Render both validation panels to a PNG.

    Args:
        curve: DataFrame from generate_validation_curve.
        output_path: Path to save the PNG.
    """
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6))

    _panel(
        ax1,
        curve["pct_replaced_by_number"], curve["pct_avoided_by_number"],
        curve["sd_avoided_by_number"],
        "% pipes proactively replaced (by count)",
        "% breaks avoided",
        "Count-based: breaks avoided vs pipes renewed",
        "tab:blue",
    )
    _panel(
        ax2,
        curve["pct_replaced_by_length"], curve["pct_avoided_by_length"],
        curve["sd_avoided_by_length"],
        "% network length proactively replaced",
        "% emergency-replacement length avoided",
        "Length-based: emergency length avoided vs length renewed",
        "tab:green",
    )

    fig.suptitle(
        "Shaded band = first 50% of replacement activity, where risk-based "
        "prioritisation should sit above the diagonal",
        fontsize=9, y=0.02,
    )
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Validation curve plot saved to {output_path}")


def save_validation_data(curve: pd.DataFrame, output_path: str) -> None:
    """Save the validation curve data using an atomic write."""
    safe_write_file(output_path, curve.to_csv(index=False))
    print(f"Validation data saved to {output_path}")


def summarize_validation(curve: pd.DataFrame) -> dict:
    """Evaluate the README's acceptance test on a generated curve.

    The stated criterion is that risk-based prioritisation should keep the
    curve above the diagonal over the first 50% of replacement activity.

    Args:
        curve: DataFrame from generate_validation_curve.

    Returns:
        Reach of each axis and whether each panel clears the diagonal early.
    """
    def above_diagonal_early(x_col, y_col):
        early = curve[curve[x_col] <= 50.0]
        if early.empty:
            return False
        return bool((early[y_col] >= early[x_col]).all())

    return {
        "max_pct_replaced_by_number": float(curve["pct_replaced_by_number"].max()),
        "max_pct_replaced_by_length": float(curve["pct_replaced_by_length"].max()),
        "max_pct_avoided_by_number": float(curve["pct_avoided_by_number"].max()),
        "max_pct_avoided_by_length": float(curve["pct_avoided_by_length"].max()),
        "above_diagonal_first_50_by_number": above_diagonal_early(
            "pct_replaced_by_number", "pct_avoided_by_number"
        ),
        "above_diagonal_first_50_by_length": above_diagonal_early(
            "pct_replaced_by_length", "pct_avoided_by_length"
        ),
    }
