# Final Features — Implementation Plan

Status: **for review**, nothing implemented yet. Covers three pieces of work, in this order:

0. Bring the tests and docs into line with the owner-confirmed parameters
   (`HAZARD_LENGTH_SCALE = 8600`, `CIP_REPLACEMENT_COST_PER_INCH_FT = $25`).
1. **Strategy plan export**: one action-plan CSV per strategy marked on the optimisation chart.
2. **Discount-rate sensitivity sweep**: the chosen strategy (default: knee) at 0%, 3% and 5%.

The model itself (physics, costs, objectives, search) is not changed by any of it. Section 4
lists the decisions I need from you; each has a recommended default so work can start on a yes.

"Front matter" is read as `optimizer_config.yaml`, where the run's other options live.

---

## 0. Parameter alignment: 8,600 and $25

The four tests failing since `c242c75` and the docs that still quote 4,300 and $120.

### 0.1 Tests

| Test | Why it fails now | Change |
|---|---|---|
| `test_hazard_scale_is_the_calibrated_value` | Pins 4,300 | Pin **8,600**; docstring says the value is set by the owner, not derived |
| `test_calibration_window_rate_is_defensible` | Window rate is **0.122** breaks/mi/yr, below the 0.15–0.40 band | Band becomes **0.10–0.40**. Docstring drops "several times above the national average" and states what 0.12 is: about 1.6× Folkman's all-ages 0.077 for this material mix |
| `test_budget_is_deployed_when_work_is_available` | At $25/inch-ft, $399k/yr buys about 5× more pipe than at $120, so eligible work runs out in some years (utilisation 0.889, needs > 0.90) | Scale the test budget by the cost ratio (25/120): **$83k/yr** gives utilisation **0.994**, no zero-spend years. The gate itself is unchanged |
| `test_wearout_is_load_bearing_for_interior_optimum` | Measured below. With cheap CIP, the optimum is interior **without** wear-out too ($200k), so the claim "without wear-out the optimum sits at a bound" is no longer true | Restate the claim to what the data now shows: **wear-out raises the funded optimum** (best budget with wear-out > best without) |

Budget sweep at trigger 1.5, 8 replicates, total PV cost ($M):

| Budget /yr | $10k | $200k | $500k | $1.0M | $1.5M | $2.0M | $3.0M | $5.0M |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| With wear-out | 51.42 | 19.25 | 12.33 | 12.20 | **11.89** | 11.89 | 11.89 | 11.89 |
| Without wear-out | 12.54 | **8.81** | 9.02 | 10.94 | 10.66 | 10.66 | 10.66 | 10.66 |

The table exposes a second, latent problem. With wear-out, the total is **identical from
$1.5M upward**: at trigger 1.5 all eligible work is already funded, so extra budget goes
unspent. `test_budget_optimum_is_interior` passes only because `min()` returns the first of
four tied budgets. I'll make both tests take **the lowest budget within 0.1% of the minimum**
(the start of the flat stretch), so a tie can't decide the answer.

Every other test passes unchanged at the new values, including the emergency-premium test.

### 0.2 Docs and comments

| File | Change |
|---|---|
| `leyp_config.py` | Scale comment: owner-set at 8,600 (2× the value derived on the 10-year window), window rate 0.122, the date. Emergency-premium comment: at $25/inch-ft the premium is **33× for a 6-inch main and 17× for a 12-inch**, not 6.9× and 3.5× |
| `docs/MODEL_ASSUMPTIONS.md` | §1 unit costs and the derived $/ft table by diameter; §3 calibration (scale, rate, `hazard × 5,280 ÷ 8,600`) and the two derived by-material rate tables, all recomputed with the method used before; §8 limits |
| `README.md` | Calibration paragraph, investment-cost formula, config excerpt (`120.00`, `4300.0`) |
| `docs/CIP_PROCESS_FLOW.md` | CIP node "$25 per inch-ft" |
| `optimizer_config.yaml`, `tests/test_optimizer.py` docstring | The "$84.4M to renew once" figure becomes about **$17.6M** at $25 |
| `OPEN_ITEMS.md` | Current-state lines (0.287 target, 4,300) updated. **MOD-4** and **ENG-4** note the scale is now set by the owner rather than derived |
| `verification/eol/REPORT.md` | A dated addendum: V3.3's pin is now 8,600 by owner decision. The rest stays as the record of what was verified then |

`REVIEW_PLAN.md` and `EOL_MODELLING_PLAN.md` are records of earlier decisions. I'll leave them
as they are.

> **Worth knowing, not blocking:** the emergency premium is the reason planned renewal pays.
> At $25/inch-ft it is roughly 33× for a 6-inch main, against the 2–5× the config comment
> calls typical utility practice. The optimiser will lean harder on CIP than a 2–5× premium
> would justify. If $25 is meant as a low-cost scenario rather than the base case, the
> sensitivity sweep in §2 would be a natural place to vary it too. Say if you want that.

---

## 1. Strategy plan export

### What you get

With the option on, a full run writes one action plan per strategy marked on
`optimization_curve.png`, named after its call-out:

| Call-out on the chart | File |
|---|---|
| Minimum total cost | `Action_Plan_Min_Total_Cost.csv` |
| Knee (balanced) | `Action_Plan_Knee_Balanced.csv` |
| Max marginal return | `Action_Plan_Max_Marginal_Return.csv` |

Each file has the same columns as `Optimal_Action_Plan.csv` (year, pipe, action, cost, cause,
backlog flag, priority…). `Optimal_Action_Plan.csv` stays, for the selected strategy, so
nothing that reads it breaks. The selected strategy's export is byte-identical to it.

### Config

```yaml
# 9. OUTPUTS
# Write one action plan per strategy marked on the optimisation chart
# (Action_Plan_<call-out>.csv). About 30 s extra.
export_strategy_plans: true
```

The default is on. It costs three simulations (8 replicates each), about 30 s.

### How

- `leyp_runner`: lift the action-log formatting out of `_generate_reports` into a small helper
  (sort by year, fill `Breaks`/`Cause`/`Backlog`). `Optimal_Action_Plan.csv` is then written
  through the same helper, so the two cannot drift. Output is unchanged.
- `leyp_optimizer.export_strategy_plans(df, output_dir, config)`: for each rule's strategy,
  `simulate()` with the run's seed, replicates and rate. It writes the replicate nearest the
  mean total cost, the same choice the main action plan uses. When two rules pick the same
  strategy, both files are written with the same content, so the file set is always the same.
- Called from a full run, and from `--plot-only`. Exporting re-simulates but does not
  re-optimise, so it fits a redraw. `--plot-only` still does not touch
  `Optimal_Action_Plan.csv`.
- `selected_strategies.csv` gains a `plan_file` column naming each strategy's file.

### Tests

- Option on: a tiny run writes exactly the three files. The selected strategy's file equals
  `Optimal_Action_Plan.csv` byte for byte, and each file's total CIP cost equals its
  strategy's nominal investment in that replicate.
- Option off: none of the three files is written.
- `--plot-only` with the option on writes the three files, and leaves `Optimal_Action_Plan.csv`
  and `nsga2_results.csv` unchanged.
- Rules that coincide still give three files.

---

## 2. Discount-rate sensitivity sweep

### What it answers

How does the recommendation change if the analysis discounts the future more, less, or not
at all? The discount rate changes the objectives, so it changes the Pareto front and which
strategy each rule picks. **The sweep therefore re-optimises at each rate.** It does not
re-price one fixed plan: a fixed plan's spend, failures and strategy are identical at every
rate by construction, so that version would be a table of repeated numbers.

### Output: `discount_sensitivity.csv`

Columns are the rates; rows are about the chosen strategy (knee by default). The plateau row is
dropped, as you asked. With today's committed run, the 3% column would read:

| | Nominal | 3% | 5% |
|---|---|---|---|
| Recommended avg spend (knee pick) | … | $144k/yr | … |
| Failures over 100 yr at the knee pick | … | 52 | … |
| Strategy type at the knee pick | … | $178k/yr budget, replace at condition ≤ 1.25 | … |

- **Recommended avg spend:** nominal 100-year investment ÷ 100, as on the chart call-outs.
- **Failures:** breaks over the 100 years, mean over replicates, rounded.
- **Strategy type:** Water has two genes, so the cell is the budget and the replacement trigger.
  Pipe's "budget, rehab share" has no equivalent here. If the pick sits at the $2M budget
  ceiling (likely at 0%), the cell says "at the budget ceiling", since the optimum may lie
  above it.
- Row labels follow the strategy, e.g. "(min-total pick)" if you sweep `min_total_cost`.

Alongside it:

- `discount_sensitivity_detail.csv`: the same picks as numbers, one row per rate. It holds the
  budget, trigger, average spend, breaks, emergency-replaced pipes, objective-basis total and
  the gene-ceiling flag, for anyone who wants to chart or quote them.
- `sensitivity/nsga2_results_rate_0pct.csv` and `..._5pct.csv`: each rate's full front, so any
  cell can be traced back.
- The table printed to the console as markdown, so it can be pasted into a report.

### Config and CLI

```yaml
# 10. DISCOUNT-RATE SENSITIVITY
# Re-optimises at each rate (about 20 min per extra rate) and tabulates the
# chosen strategy's average spend, failures and strategy type per rate.
sensitivity:
  enabled: false            # true: run after a full optimisation
  discount_rates: [0.0, 0.03, 0.05]
  strategy: knee            # min_total_cost | knee | max_marginal_return
```

```bash
python leyp_optimizer.py --sensitivity-only   # the sweep alone, no action plan or charts
```

`enabled` defaults to **false** because each extra rate is a full NSGA-II search (about 20 min
at the current settings, so roughly 40 min on top of a normal run). `--sensitivity-only` runs
it on demand.

### How

- `run_search(config, input_path, discount_rate, checkpoint_path)`: the existing search
  (algorithm, seeds, genes, replicates), factored out of `run_optimization` unchanged, with
  the rate as a parameter. The main run calls it at the configured rate, so its front is
  exactly what it is today.
- **Reuse, not repeat.**
  - In a full run, the column at the configured rate (3%) is the run's own front.
  - With `--sensitivity-only`, the saved `nsga2_results.csv` is reused for that column, if its
    `Discount_Rate` matches and re-simulation reproduces its objectives (the same check
    `--plot-only` already makes). Otherwise that rate is searched as well.
  - So the 3% column always agrees with the committed action plan, and only the other two
    rates cost a search.
- **Checkpoints stay apart.** Each rate checkpoints to its own file
  (`nsga2_checkpoint_rate_5pct.pkl`). A preempted sweep then resumes the right rate and can
  never resume a 5% search from a 3% checkpoint, which the single shared file today would
  allow.
- Each rate's front goes through `add_nominal_costs` (nominal spend, breaks), then the chosen
  rule.
- `sensitivity.strategy` and every rate are checked before any search starts. An unknown rule,
  a negative rate or an empty list fails at once.

### Tests

These run on the three-pipe inventory with a tiny search, so they take seconds.

- **Shape:** three columns labelled `Nominal`, `3%`, `5%`, and the three rows, labelled for the
  configured strategy.
- **Reuse:** in a full run with the sweep on, NSGA-II runs exactly three times (main + 0% +
  5%), and the 3% column equals the main run's knee.
- **`--sensitivity-only`:** with a reproducible saved front it searches twice; with a missing
  or stale front it searches three times. It writes no action plan and no charts.
- **Strategy rule:** `strategy: min_total_cost` changes the picks and row labels; an unknown
  rule fails before the search.
- **Checkpoints:** files are per rate and cleaned up after a successful sweep.
- **Formatting, on synthetic picks:** spend formatting, rounding of failures, and the ceiling
  note.

---

## 3. Work order and acceptance

| Step | Commit | Suite after |
|---|---|---|
| 0 | Tests and docs for 8,600 / $25 | **All green** (the 4 current failures fixed) |
| 1 | Action-log helper in `leyp_runner` (no output change) + strategy plan export | Green |
| 2 | `run_search` extracted (no output change) + per-rate checkpoints | Green |
| 3 | Sensitivity sweep, table, `--sensitivity-only`, config | Green |
| 4 | README, MODEL_ASSUMPTIONS §9, process flow; regenerated outputs | Green |

**Acceptance**

- The full suite passes with no failures.
- `python leyp_optimizer.py` with `export_strategy_plans: true` writes the three
  `Action_Plan_*.csv` files, and the selected one equals `Optimal_Action_Plan.csv`.
- With `sensitivity.enabled: true` (or `--sensitivity-only`) the run writes
  `discount_sensitivity.csv` in the layout above, with the 3% column matching the main run.
- A run with both options off produces exactly today's outputs: same front, same picks, same
  files.

**Regenerated outputs.** The committed results came from your Mac. A fresh search here finds a
different front (knee $433k/yr against your $178k/yr). As shown last time, that comes from the
machine, not the code. So:

- The three exported plans are committed by redrawing your saved front with `--plot-only`.
  Re-simulation reproduces it exactly, so they match your action plan.
- The 0% and 5% searches are run here to verify the sweep. For the committed
  `discount_sensitivity.csv`, run `python leyp_optimizer.py --sensitivity-only` on your Mac:
  it reuses your 3% front and searches the other two rates on the same machine that produced
  it.

---

## 4. Decisions for you

| # | Question | Recommended default |
|---|---|---|
| D1 | You called the third plan "peak marginal risk"; its call-out reads **"Max marginal return"** (most risk avoided per extra dollar). Keep that wording for the file and call-out, or rename both, e.g. to "Peak marginal return"? | **Keep "Max marginal return"**: it matches LEYP_Pipe's chart, so the two packages' figures stay aligned |
| D2 | One value per rate, or ranges (like your example's "$232–268k/yr") from repeating each search with several NSGA-II seeds? Ranges multiply the sweep's cost by the number of seeds | **One value per rate**, with an optional `sensitivity.algorithm_seeds: [...]` list that switches cells to min–max ranges |
| D3 | Should the sweep's strategy follow `selection`, or be set independently? | **Independent, `sensitivity.strategy: knee`**, as you asked. It currently coincides with `selection: knee` |
| D4 | "Failures": breaks, or emergency replacements (pipes replaced after a segment reaches 3 breaks)? | **Breaks** in the table; emergency replacements in the detail CSV |
| D5 | Sweep on by default, or on demand? | **Off by default** (`enabled: false`) plus `--sensitivity-only`, because it adds about 40 min |
| D6 | Commit the sensitivity results computed here, or wait for your Mac run (see above)? | **Wait for your Mac run**. I'll verify here and commit the code |
