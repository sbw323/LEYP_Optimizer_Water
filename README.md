# LEYP-Water: Water Main Replacement Planning Optimizer

## Overview

**LEYP-Water** is a Monte Carlo simulation and multi-objective optimization framework for generating optimized water main replacement plans. It models pipe degradation physics, simulates break events over a 100-year planning horizon, applies replacement strategies within budget constraints, and uses the NSGA-II genetic algorithm to discover Pareto-optimal trade-offs between investment cost and emergency risk cost. Both objectives are reported as present values.

The system features crash-safe checkpointing for preemptible cloud VMs, ensuring optimization can resume from interruptions without loss of progress. All file outputs use atomic writes to prevent corruption during preemption events.

The package is inspired by the **LEYP (Linear Extension of the Yule Process)** statistical model originally developed by Cemagref/IRSTEA for water main break prediction. This implementation adapts the core concepts — Weibull baseline hazard, material covariates, and the "previous breaks increase future break rate" feedback loop — into a forward-looking simulation engine suitable for water utility capital improvement planning.

---

## What It Does

Given a pipe inventory CSV with attributes (ID, age, condition score, material, diameter, length, and consequence-of-failure), the package:

1. **Preprocesses** the network into analysis-ready segments (optional segmentation for long pipes).
2. **Simulates** 100 years of pipe life, including stochastic degradation, Poisson-distributed break events on virtual sub-segments, and condition-driven failure.
3. **Applies investment logic** each year: budget-constrained replacement prioritized by risk reduction per dollar.
4. **Optimizes** two decision variables simultaneously via NSGA-II to minimize the present value of both investment spend and emergency cost across the planning horizon.
5. **Outputs** a Pareto front of strategies, a year-by-year action plan for the selected solution, per-year diagnostics, and validation curves.

---

## Package Architecture

| Module | Role | Key Responsibility |
|---|---|---|
| `leyp_config.py` | Configuration & Constants | All tunable parameters: material properties, Weibull parameters, degradation physics, cost model, discount rate, replacement triggers, and checkpoint settings. |
| `leyp_preprocessor.py` | Data Preparation | Reads raw pipe inventory CSV, standardizes column headers, optionally segments long pipes, and writes an optimized input file. |
| `leyp_core.py` | Physics Engine | `Pipe` and `VirtualSegment`. Weibull hazard with saturating LEYP feedback, lognormal time-to-failure sampling, exponential condition degradation, and Poisson break simulation on four virtual sub-segments per pipe. |
| `water_replacement.py` | Replacement Decision Engine | `ReplacementManager` implements budget-constrained replacement prioritized by risk reduction per dollar. |
| `leyp_runner.py` | Simulation Executor | Orchestrates a 100-year run, accumulates the three cost streams and their present values, and writes the action plan, cost summary, and diagnostics. |
| `leyp_optimizer.py` | NSGA-II Optimization Wrapper | `pymoo` `ElementwiseProblem` with 2 genes, 2 objectives, checkpoint/resume, Pareto front annotation, and strategy selection. |
| `water_validation.py` | Performance Analysis | Three-panel validation curve: breaks avoided, emergency length avoided, and cost-effectiveness. |
| `optimizer_config.yaml` | Optimization Settings | Gene bounds, discount rate, determinism, selection rule, validation and algorithm settings. |

---

## Core Concepts

### Condition Rating Scale

Pipes use a **1–6 condition score** following water utility asset management conventions:

- **6** = Excellent (new or recently replaced)
- **5** = Good (minor defects)
- **4** = Fair
- **3** = Moderate (observable deterioration)
- **2** = Poor (significant defects, replacement eligible)
- **1** = Failed / Critical

### Water Main Materials

| Material | Base Life (years) | Weibull β | Weibull η | Notes |
|---|---|---|---|---|
| HDPE | 150 | 1.0 | 120 | Most durable plastic |
| PVC | 120 | 1.1 | 110 | Standard plastic pipe |
| DI (Ductile Iron) | 100 | 1.5 | 90 | Modern iron standard |
| Steel | 80 | 1.4 | 80 | Carbon steel |
| CI (Cast Iron) | 75 | 1.8 | 75 | Legacy iron pipe |
| PCCP | 75 | 1.6 | 70 | Prestressed concrete |
| CU (Copper) | 85 | 1.2 | 85 | Service connections |
| AC (Asbestos Cement) | 60 | 2.0 | 60 | Legacy material |
| GI (Galvanized Iron) | 50 | 2.2 | 45 | Legacy material |

### Initialization Physics

Initial condition decays exponentially with age, so pipes older than their design life start low but non-zero rather than being clamped:

```
life_fraction     = age / base_life[material]
age_condition     = 1.0 + 5.0 * exp(-1.5 * life_fraction)
initial_condition = min(6.0, age_condition + boost * (6.0 - age_condition))
```

The CSV `Condition` column acts as a modifier (`boost = max(0, (Condition - 1) * 0.5)`), blending the age estimate upward for newer-era assets. The curve is asymptotic and never reaches 1.0, so **age alone cannot condemn a pipe at initialization**.

Break history is seeded proportionally to age and distributed across virtual sub-segments, **capped one break below `SEGMENT_BREAK_THRESHOLD` in every segment**. Seeded breaks exist to drive the LEYP feedback term; a pipe the inventory lists as in service must not start the simulation already failed. The failure rule itself is unchanged, and seeded breaks still count toward it — a segment seeded at the cap fails on its next break.

### Degradation Model

Each pipe is assigned a **lognormal time-to-failure (TTF)**. Condition decays exponentially:

```
condition(t+dt) = condition(t) × exp(-degradation_rate × dt)
```

where `degradation_rate = ln(6.0) / TTF`, so a pipe starting at condition 6 reaches condition 1 at its sampled TTF.

### Hazard & Break Simulation (LEYP-Inspired)

```
h(t) = (β/η) × (t/η)^(β-1) × material_mult × exp(coeff_diameter × diameter)
       × (1 + α × min(n_breaks, LEYP_BREAK_FEEDBACK_CAP))
       × wearout(age / base_life)

wearout(r) = 1                                                  for r ≤ onset
           = 1 + (M − 1) × (1 − exp(−k × (r − onset)))          for r > onset
```

- **β, η**: Material-specific Weibull parameters.
- **α**: LEYP feedback parameter (default 0.15).
- **`wearout(r)`** — the **age-related arm of the bathtub curve**. `r` is the pipe's age as a multiple of its material's `STANDARD_LIFE.base_life`. The multiplier is exactly 1× through average life, then rises sharply and saturates at `WEAROUT_MAX_MULTIPLIER` (`M`, default 10): 2.6× at 110% of average life, 4.5× at 125%, 6.7× at 150%, 9.8× at 300%. `WEAROUT_ONSET_LIFE_RATIO` (default 1.0) sets where it begins and `WEAROUT_STEEPNESS` (`k`, default 2.0) how fast it rises. Bounded by construction, so it cannot diverge; it resets on renewal because a replaced pipe's age restarts at zero; `M = 1` disables it exactly. This term is what gives "end of life" a physical consequence: before it existed, a pipe at its condition floor broke no faster than a healthy pipe of the same age, and the model compensated with a rule that emergency-replaced any pipe the moment it reached the floor. The early-life arm (infant mortality) is deliberately not modelled; see `OPEN_ITEMS.md` DEC-6.
- **Two clocks.** Wear-out runs on `STANDARD_LIFE.base_life`; condition decay runs on each pipe's sampled TTF around `DEGRADATION_PARAMS.ttf_mean`, which is ~25% shorter. Most pipes therefore reach the condition floor before wear-out begins. Condition is the planning signal; age is the mortality clock. The gap is reported as `Floor_Pipes_Below_Onset`.
- **`LEYP_BREAK_FEEDBACK_CAP`**: Ceiling on the break count entering the feedback term. Hazard elevation from break history saturates in practice. **Without a ceiling the term is an unbounded positive feedback loop** — each break raises hazard, which produces more breaks — and it diverges in any run where pipes are not renewed. Renewal masks this: an untreated baseline reached ~10⁷ breaks/mile/year before the cap was added.

Expected breaks per segment per year are `h(t) × segment_length / HAZARD_LENGTH_SCALE`. Each pipe has **4 virtual sub-segments**.

**Failure rule:** a pipe is condemned when **any single segment** reaches `SEGMENT_BREAK_THRESHOLD` breaks (default 3). **This is the only route to emergency replacement.** Reaching the condition floor has no direct consequence: a worn-out pipe stays in service, remains CIP-eligible, and keeps breaking under the wear-out hazard until planned renewal reaches it or a segment fails. (Earlier versions emergency-replaced any pipe the moment it reached the floor, which charged $5,000/ft for simply existing and drove the optimizer to spend without limit.) Break damage also reduces condition by `BREAK_CONDITION_PENALTY` per break, floored at `BREAK_DAMAGE_CONDITION_FLOOR` — that coupling makes a frequently-breaking pipe read as poor condition and become CIP-eligible; it is not a route to failure.

### Calibration

`HAZARD_LENGTH_SCALE` is a pure units constant with no physical claim of its own, which makes it the right knob for the model's absolute break production. It is a **static parameter**; "calibration" refers to how its value was chosen, not to anything that happens at run time.

It is set by the owner at **8,600**, twice the value derived against a **short untreated window** — the inventory as it stands, run with no CIP and no renewal for `CALIBRATION_WINDOW_YEARS` (10), matched to 0.287 breaks/mile/year (4,300). At 8,600 the window rate is **0.128 breaks/mile/year**. A short window measures today's network, which is what a published break-rate statistic describes. The full 100-year do-nothing run is kept only as a **stability check** — it must stay finite and sit above the window rate — because under wear-out most of that run is spent with the whole network far past average life at the saturated multiplier, a state no real utility is in and no literature rate was measured on.

The derived value is tightly coupled to `WEAROUT_MAX_MULTIPLIER`: the inherited backlog is already past onset, so the window rate scales almost proportionally with the ceiling (0.11 / 0.26 / 0.60 at 5× / 10× / 20× with the scale held fixed). Only the combination is pinned by the break-rate target; separating them needs local break records broken down by pipe age.

Anchor: Folkman (2018) national averages weighted by that network's material mix give 0.077 breaks/mile/year across all ages; at 8,600 this network, already 60% past design life, sits about 1.7× above it. Expected breaks scale as 1 ÷ `HAZARD_LENGTH_SCALE`. **Revisit this constant if the inventory, material parameters, or `ALPHA` change.**

### Three-Cost-Stream Model

1. **Investment Cost**: Planned CIP replacement (`$25/inch-ft` × diameter × length)
2. **Emergency Repair Cost**: Point repairs for individual breaks (`$5,000`/break)
3. **Emergency Replacement Cost**: Full replacement of failed pipes (`$5,000`/ft)

Emergency replacement carries a premium over planned work — mobilization, out-of-hours crews, service interruption, road reinstatement, and the loss of any chance to upsize or coordinate with other works. At these rates that is roughly 6.9× for a 6-inch main and 3.5× for a 12-inch one.

**Objective 1** (Investment) = present value of planned CIP spend
**Objective 2** (Risk) = present value of emergency repairs + emergency replacements

Both are discounted at `DISCOUNT_RATE` (default 3% real). Undiscounted totals are reported alongside. Setting the rate to 0.0 recovers undiscounted behavior.

---

## Replacement Logic

1. **Eligibility**: condition ≤ replacement trigger. Pipes at the failure floor **remain eligible** — excluding them hands every age-out pipe to the emergency stream, which is exactly the work a CIP program exists to capture.

2. **Prioritization — risk reduction per dollar**:
   ```
   Annualized_Risk = (Length × CoF × Emergency_Replacement_Cost) / max(0.1, TTF_remaining)
   Priority        = Annualized_Risk / CIP_Replacement_Cost
   ```
   Ranking on annualized risk alone is length-dominant: risk scales with length, so the longest and least affordable pipes sort to the top. Dividing by cost makes it a benefit/cost ratio in which length cancels, leaving consequence, imminence, and cost per foot to drive the order.

3. **Budget execution**: pipes are taken in priority order; one that does not fit is **skipped, not treated as a stopping point**, so cheaper eligible pipes behind it can still use the remaining budget. Pipes costing more than a whole year's budget can never be funded in one year and are reported (`Unfundable`, `Unfundable_Length`) rather than blocking the queue — a multi-year programming question, not a ranking one.

4. **Full state reset**: replaced pipes get condition 6.0, the default replacement material, a negative age marking the replacement year, and fresh virtual segments.

### Annual Loop Order

```
1. PLANNED CIP   decided on condition at the last assessment
2. DEGRADE       apply natural ageing; count pipes at the floor
3. BREAKS        Poisson events under the wear-out hazard, repairs, and
                 emergency replacement of any pipe whose segment failed
```

CIP decides **before** the year's degradation and breaks. Deciding afterwards would let the program intercept a pipe in the very year it fails — perfect foresight plus same-year execution, which no capital program can schedule, and which makes replacing at the last possible moment look optimal. There is no separate emergency step for pipes at the condition floor; emergency replacement happens inside step 3, and only after a segment fails.

---

## NSGA-II Optimization

| Gene | Description | Default Range |
|---|---|---|
| `budget` | Annual CIP replacement budget ($) | $10,000 – $2,000,000 |
| `rehab_trigger` | Condition threshold for replacement eligibility | 1.0 – 3.5 |

The budget ceiling must be able to express a solvent policy. The reference network costs about **$84.4M** to renew once, so a ceiling of $500k/yr over the horizon ($50M) cannot keep up with degradation at any point in the search space.

### Determinism and Replicates

The simulation is stochastic. `run_simulation(seed=...)` seeds the stream before the network is built, so **a given (seed, budget, trigger) reproduces to the dollar**. The optimizer applies **one seed to every evaluation** (common random numbers), so differences between candidates come from the genes rather than simulation noise.

`n_replicates` averages several draws per evaluation. On the reference network, total-cost spread falls from $12.3M at a single draw to $2.9M at five — about 0.75% of the front's range.

> Seeding applies to NumPy's global stream, so concurrent evaluations in one process are not independently reproducible. Run replicates in separate processes if parallelizing.

### Strategy Selection

Three rules each pick one strategy from the front. `selection:` in `optimizer_config.yaml` chooses which one drives the action plan; all three are always marked on the charts and summarised in `selected_strategies.csv`.

- `min_total_cost` (default) — lowest investment + risk: the point where the front's slope is −1, so a dollar invested avoids a dollar of risk. Both objectives are dollars, so this is a real quantity rather than an arbitrary weighting.
- `knee` — closest to the ideal point (least investment, least risk) after min–max normalising both objectives; the best-balanced trade-off.
- `max_marginal_return` — the steepest stretch of the front, where each additional dollar invested avoids the most risk. Single segments of an NSGA-II front are noisy, so the slope is measured over a sliding window of five neighbouring points and the strategy is the window's centre. It is a best-value-per-dollar point, usually at low investment with high remaining risk — not a recommended spend level.

**The plateau** is the set of front strategies whose total is within `plateau_tolerance` (default 5%) of the minimum total. Total cost is flat near the optimum, so the plateau, not the single minimum, is the honest answer to "what budget": strategies inside it are interchangeable to within the tolerance.

**Present value and nominal.** The objectives, and so every rule, the plateau and the axes of `optimization_curve.png`, are present values at `discount_rate` (3% real, end-of-year). Average annual spend is always **nominal**: a strategy's undiscounted 100-year investment ÷ 100 — never the budget gene and never a present value. `nsga2_results.csv` holds the nominal sums beside the present values; they come from re-simulating each front strategy with the optimizer's seed, which reproduces its objectives exactly.

The run prints the three picks, the selected rule, the plateau (point count, budget range, investment range) and the inherited backlog, and warns if the selected budget sits at the gene ceiling, since the optimum may lie above it. `nsga2_results.csv` also carries `Norm_Investment`, `Norm_Risk` and `Ideal_Distance`.

---

## Installation & Usage

```bash
pip install pandas numpy scipy matplotlib pyyaml pymoo
pip install pytest        # to run the test suite
```

```bash
python leyp_optimizer.py                      # full optimization + reports
python leyp_optimizer.py --cost-curve-only    # search, then write only the nominal cost curve
python leyp_optimizer.py --plot-only          # no search: redraw charts + summary from the saved front
python leyp_optimizer.py --sensitivity-only   # discount-rate sweep only (0% / 3% / 5%)
python leyp_runner.py --output-dir results \
    --budget 1000000 --trigger 1.5 \
    --seed 20260830 --replicates 5            # single strategy
pytest                                        # 227 tests
```

### Key Output Files

| File | Description |
|---|---|
| `nsga2_results.csv` | Pareto front: present-value objectives, genes, total cost, normalized position, distance to ideal, the discount rate, and each strategy's nominal investment, risk, total and average annual spend, breaks, emergency-replaced pipes and inherited backlog |
| `optimization_curve.png` | **Report figure.** 100-year investment, risk and total against investment along the front (present value at the run's rate), the plateau shaded, and the three rules' strategies marked with their nominal average annual spend; the selected one flagged in the legend |
| `selected_strategies.csv` | One row per rule's strategy plus one for the plateau: selected flag, basis, budget, trigger, 100-year investment / risk / total, nominal investment, average annual spend, breaks, emergency-replaced pipes, inherited backlog, risk avoided per dollar invested (≈ 1 at the minimum total), and the strategy's exported plan file; the plateau row gives its budget, trigger, investment, total and spend ranges and point count |
| `Action_Plan_Min_Total_Cost.csv`, `Action_Plan_Knee_Balanced.csv`, `Action_Plan_Max_Marginal_Return.csv` | With `export_strategy_plans: true` (default): the year-by-year plan of each strategy marked on the chart, named after its call-out, in the same columns as `Optimal_Action_Plan.csv`. The selected strategy's file is identical to `Optimal_Action_Plan.csv` |
| `discount_sensitivity.csv` | Sensitivity sweep only: the chosen strategy (default knee) at each rate — columns Nominal / 3% / 5%, rows average annual spend, failures (breaks) over 100 years, and strategy type |
| `discount_sensitivity_detail.csv`, `sensitivity/nsga2_results_rate_*.csv` | Sensitivity sweep only: the picks as numbers, and each rate's full front |
| `pareto_cost_curve.png` | The report figure on a nominal (undiscounted) basis, with the rules and plateau applied to nominal costs |
| `pareto_front.png` | The front itself: risk against investment, coloured by total, with the ideal point and the three picks |
| `Optimal_Action_Plan.csv` | Year-by-year schedule for the selected strategy |
| `cost_summary.csv` | Present values, undiscounted totals, and plan health metrics |
| `simulation_diagnostics.csv` | Per-year CIP/emergency/break activity, budget utilization, discount factors, `Pipes_At_Floor`, `Floor_Pipes_Below_Onset` |
| `pareto_cost_curve_data.csv` | `--cost-curve-only` runs only: the front behind `pareto_cost_curve.png` |
| `validation_curve.png` | Three-panel validation |
| `validation_data.csv` | Validation curve data with replicate spread |

**`--plot-only`** redraws `optimization_curve.png`, `pareto_cost_curve.png`, `pareto_front.png` and `selected_strategies.csv` from the saved `nsga2_results.csv`, applying the config's current `selection` and `plateau_tolerance`, without re-optimising. It does not regenerate the action plan, cost summary, diagnostics or validation curve — those need a full run — and says so. A front saved before the nominal and count columns existed is re-simulated once (exact with the configured seed; refused if the re-simulation does not reproduce the saved objectives) and saved back with them.

**`--plot-only`** also re-simulates and writes the three `Action_Plan_*.csv` files when `export_strategy_plans` is on; it still leaves `Optimal_Action_Plan.csv` alone.

### Discount-rate sensitivity

The sweep answers: how does the recommendation change if the analysis discounts the future more, less, or not at all? The discount rate changes the objectives, so it changes the front and the strategy each rule picks — the sweep therefore **re-optimises at each rate**. (Re-pricing one fixed plan would be pointless: its spend, failures and strategy are the same at every rate.)

```yaml
sensitivity:
  enabled: false            # true: run after a full optimisation
  discount_rates: [0.0, 0.03, 0.05]
  strategy: knee            # independent of `selection`
```

`python leyp_optimizer.py --sensitivity-only` runs it on demand. The front already searched at the configured rate is reused for that column — the run's own front in a full run, or the saved `nsga2_results.csv` with `--sensitivity-only` provided every point still reproduces exactly — so only the other rates cost a search (about 20 min each at the default settings). Each rate checkpoints to its own file (`nsga2_checkpoint_rate_5pct.pkl`), so a preempted sweep never resumes one rate from another's checkpoint. `discount_sensitivity.csv` holds one column per rate:

| | Nominal | 3% | 5% |
|---|---|---|---|
| Recommended avg spend (knee pick) | nominal 100-yr investment ÷ 100 | … | … |
| Failures (breaks) over 100 yr at the knee pick | mean over replicates, rounded | … | … |
| Strategy type at the knee pick | e.g. "$178k/yr budget, replace at condition ≤ 1.25" | … | … |

A pick at the budget gene ceiling is marked "at the budget ceiling", since the optimum at that rate may lie above it. The table is also printed as markdown.

**`--cost-curve-only`** runs the same NSGA-II search and writes only `pareto_cost_curve.png` and `pareto_cost_curve_data.csv`. It skips the action plan, validation curve, `nsga2_results.csv` and the other charts, and leaves any of those already in the output directory as they were, so after a parameter change they will describe the earlier run.

On the nominal chart the three rules are applied to nominal costs, so its "Minimum total cost" is the lowest point of the nominal total. The configured rule picks on present values, so its strategy can differ from the same rule's nominal pick; it is then shown with its own hollow ring, labelled e.g. "Knee (balanced), present value — selected", so the legend never calls a point selected when it was not.

`Optimal_Action_Plan.csv` records `Action` (`CIP_Replacement`, `Emergency_Replacement`, `Break_Event`), `Cause` for emergencies (always `break_failure` — the segment rule is the only route), `Life_Ratio` (age ÷ average life at the time of an emergency), `Backlog` marking the first replacement of an inherited-backlog pipe, `Priority` (the ranking value, captured before the pipe is reset), and `Annualized_Risk`.

`cost_summary.csv` reports `CIP_To_Emergency_Ratio`, `Mean_Budget_Utilization`, `Zero_Spend_Years`, `Breaks_Per_Mile_Year`, `Investment_Share_Of_Total`, `Yr1_2_Emergency_Count`, `Max_Annual_Emergency_Count`, and the inherited-backlog figures.

### Python API

`run_simulation()` returns a 2- or 5-tuple. `simulate()` returns the full result dict, including the renewal footprint and per-replicate detail:

```python
from leyp_runner import simulate

result = simulate("inventory.csv", annual_budget=1_000_000, rehab_trigger=1.5,
                  seed=20260830, n_replicates=5)
result["investment_cost"]      # present value
result["nominal_risk_cost"]    # undiscounted
result["cip_pipes"]            # distinct pipes renewed by CIP
result["replicates"]           # per-replicate results

baseline = simulate("inventory.csv", no_intervention=True, seed=20260830)
```

`no_intervention=True` runs a genuine do-nothing baseline. **A zero budget is not one**: emergency replacement still renews the network for free.

---

## Configuration Reference

### `leyp_config.py`

```python
DISCOUNT_RATE = 0.03                          # real, applied to both objectives
CIP_REPLACEMENT_COST_PER_INCH_FT = 25.00
EMERGENCY_REPAIR_COST_PER_BREAK = 5000.00
EMERGENCY_REPLACEMENT_COST_PER_FT = 5000.00
DEFAULT_REPLACEMENT_MATERIAL = "HDPE"

ALPHA = 0.15                    # LEYP break-feedback parameter
LEYP_BREAK_FEEDBACK_CAP = 10    # ceiling on feedback; without it the model diverges
COEFF_DIAMETER = -0.02
N_SEGMENTS_PER_PIPE = 4
SEGMENT_BREAK_THRESHOLD = 3     # breaks in ONE segment that condemn the pipe
HAZARD_LENGTH_SCALE = 8600.0    # owner-set (2x the 10-yr window derivation); see Calibration
CALIBRATION_WINDOW_YEARS = 10

# Wear-out (bathtub curve, age-related arm) — see Hazard & Break Simulation
WEAROUT_ONSET_LIFE_RATIO = 1.0  # begins at this multiple of STANDARD_LIFE.base_life
WEAROUT_MAX_MULTIPLIER = 10.0   # hazard ceiling the arm saturates toward
WEAROUT_STEEPNESS = 2.0         # per unit of life ratio beyond onset
BREAK_CONDITION_PENALTY = 0.3
BREAK_DAMAGE_CONDITION_FLOOR = 1.05
SIMULATION_YEARS = 100

TRIGGERS = {"Rehab": 2.0}
BACKLOG_CONDITION = 2.0         # fixed standard for inherited-backlog reporting
```

### `optimizer_config.yaml`

```yaml
genes:
  budget: {min: 10000, max: 2000000}
  rehab_trigger: {min: 1.0, max: 3.5}

discount_rate: 0.03

simulation:
  seed: 20260830        # common random numbers; null for a non-reproducible run
  n_replicates: 5

validation:
  n_points: 15

selection: min_total_cost   # or: knee, max_marginal_return
plateau_tolerance: 0.05
export_strategy_plans: true

sensitivity:
  enabled: false
  discount_rates: [0.0, 0.03, 0.05]
  strategy: knee

algorithm:
  pop_size: 32
  n_offsprings: 16
  n_gen: 30
  seed: 895
```

---

## Validation

`validation_curve.png` has three panels, all measured against a **reactive-only baseline** — no planned work, but failures still repaired and replaced. That is the real counterfactual a utility faces, and it makes each y-axis attributable to the proactive replacement on the x-axis.

| Panel | x | y |
|---|---|---|
| Count-based | % of pipes proactively replaced | % of breaks avoided |
| Length-based | % of network length proactively replaced | % of emergency-replacement length avoided |
| Cost-effectiveness | % of network length proactively replaced | emergency cost avoided per CIP dollar (PV) |

Budget points are **log-spaced**: coverage is steeply non-linear in budget, and linear spacing leaves the low-coverage half of the curve almost unsampled. Each point carries its cross-replicate standard deviation, drawn as a band. Values are reported exactly as measured — no monotonic smoothing.

### Interpreting the panels

The first two panels compare against a diagonal representing non-prioritized (random) replacement. **On the reference network neither clears it, and that reflects the metric more than the model.**

Renewal here is overwhelmingly age-out driven — at a zero budget, 409 degradation failures against 10 break-driven ones — and age-out produces no breaks. Moving a pipe from the emergency stream to the CIP stream therefore changes *who pays and when* far more than *how often it breaks*: across $0 to $2M, emergency replacements fall 384 → 5 while breaks fall only 89 → 60. The length panel tracks the diagonal closely for a related reason: length not renewed proactively is largely renewed reactively instead, which makes that comparison close to tautological.

**The cost-effectiveness panel is the informative one**, because cost avoidance is what the program actually buys. Its break-even line shows where further proactive spending stops paying for itself.

That ratio is noise-dominated at very small budgets: the two policies' stochastic paths diverge far more than the policies differ, and across five seeds at a $5,000 budget the ratio ranged from −98 to +181. Ratios are therefore computed per replicate against the same-seeded baseline replicate and reported with their spread, and the break-even summary requires a point to clear 1.0 net of its own spread.

### Sensitivity

- `LEYP_BREAK_FEEDBACK_CAP`: the untreated break rate moves only 0.245–0.384 across caps of 5 to 40; divergence returns only if the term is left unbounded.
- `SEGMENT_BREAK_THRESHOLD`: lower values mean more failures and higher emergency costs.
- `EMERGENCY_REPLACEMENT_COST_PER_FT`: sets whether proactive work pays for itself. At $800/ft each CIP dollar bought $0.51 of risk reduction; at $5,000/ft it buys $3.18.
- `DISCOUNT_RATE`: higher rates favor deferral.

---

## Testing

```bash
pytest                    # 227 tests
pytest tests/test_leyp_core.py -v
```

| File | Covers |
|---|---|
| `test_leyp_core.py` | Condition initialization, hazard, LEYP feedback, degradation, segment failure rule |
| `test_water_replacement.py` | Cost, risk-per-dollar ranking, eligibility, budget invariants, unfundable reporting |
| `test_runner.py` | Reproducibility, replicate averaging, diagnostics reconciliation, loop ordering |
| `test_initialization.py` | Seeding cap, LEYP history preservation, inherited-backlog reporting |
| `test_calibration.py` | Feedback saturation, break-rate band, hazard stability, planning realism, discounting |
| `test_optimizer.py` | Front annotation, selection rules, search sizing, config sanity |
| `test_optimizer_report.py` | The three selection rules and the plateau on synthetic fronts, the report figure and strategy summary, `--cost-curve-only`, `--plot-only`, and that `selection` drives the action plan |
| `test_plans_and_sensitivity.py` | Per-strategy plan export, the sensitivity settings and table, front reuse, per-rate checkpoints, and `--sensitivity-only` |
| `test_validation.py` | Measured counts, distinct length axis, reported spread, sweep reach, baselines |

---

## Preemption Handling & Cloud Deployment

Designed for preemptible cloud VMs (GCP spot instances) with automatic checkpoint recovery.

**Three-layer protection:**

1. **`OptimizationCheckpoint`** — NSGA-II state is pickled after every generation to `nsga2_checkpoint.pkl`. On restart, `restore_or_create()` resumes from the last completed generation and prints `Resuming from generation N`. The file is deleted after successful completion.
2. **SIGTERM handler** — spot VMs give a 30-second warning; the handler saves state and exits with code 3 (`EXIT_PREEMPTED`).
3. **Atomic writes** — all outputs go through `safe_write_file()` (temp file → `fsync` → `os.replace`), so preemption mid-write never leaves a corrupted file.

```python
# leyp_config.py
NSGA2_CHECKPOINT_PATH = "nsga2_checkpoint.pkl"
NSGA2_CHECKPOINT_EVERY_N_GEN = 1
```

### Deployment

**systemd** (`/etc/systemd/system/leyp-optimizer.service`):

```ini
[Unit]
Description=LEYP-Water Optimizer
After=network.target

[Service]
Type=simple
User=leyp
WorkingDirectory=/opt/leyp-water
ExecStart=/opt/leyp-water/venv/bin/python leyp_optimizer.py
Restart=on-failure
RestartPreventExitStatus=0 1 2
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

**Startup script:**

```bash
#!/bin/bash
cd /opt/leyp-water
while true; do
    python leyp_optimizer.py
    [ $? -ne 3 ] && break        # only exit code 3 (preempted) restarts
    echo "Preemption detected, restarting..."
    sleep 5
done
```

**Inspect a checkpoint:**

```bash
python3 -c "from checkpoint import inspect_checkpoint; inspect_checkpoint('nsga2_checkpoint.pkl', mode='pickle')"
```

---

## Troubleshooting

| Issue | Solution |
|---|---|
| `FileNotFoundError: Missing config file` | Ensure `optimizer_config.yaml` is in the working directory |
| Optimization finds no useful strategies | Check gene bounds — the budget ceiling may be too low to renew the network within the horizon |
| Results differ between identical runs | Set `simulation.seed`; leaving it null gives a non-reproducible draw |
| Break counts implausibly high | Check `LEYP_BREAK_FEEDBACK_CAP` is set; an unbounded feedback term diverges without renewal |
| Selected budget sits at the gene ceiling | The optimum may lie above it — raise `genes.budget.max` |
| Validation curve below the diagonal | Expected on age-out-dominated networks; read the cost-effectiveness panel instead |

---

## Contributing & Development

- **Python 3.10+** with type hints and Google-style docstrings
- **PEP 8** formatting
- **Unit tests** for physics and investment logic — run `pytest` before committing
- **No magic numbers** — all parameters live in `leyp_config.py`

**Adding a material:** add entries to `MATERIAL_PROPS`, `DEGRADATION_PARAMS`, and `STANDARD_LIFE` in `leyp_config.py`. `test_all_materials_initialize` covers every material in `STANDARD_LIFE` automatically.

**Changing costs:** edit the cost constants in `leyp_config.py`. Re-check the sensitivity notes above — the emergency premium determines whether proactive work is economic at all.

**Changing physics:** re-derive `HAZARD_LENGTH_SCALE` against the 10-year untreated window; `test_calibration_window_rate_is_defensible` pins the target band, `test_long_untreated_run_stays_finite` pins stability, and `test_hazard_scale_is_the_calibrated_value` pins the number so a silent edit fails the suite. Any change to a `WEAROUT_*` constant also requires re-derivation.

---

## References & Citations

1. **LEYP Model**: Cemagref/IRSTEA *Casses* software documentation for water main break prediction
2. **NSGA-II**: K. Deb et al. "A fast and elitist multiobjective genetic algorithm: NSGA-II" (2002)
3. **Break rates**: S. Folkman, "Water Main Break Rates in the USA and Canada: A Comprehensive Study" (Utah State University, 2018)
4. **Water Asset Management**: AWWA Manual M28 "Rehabilitation of Water Mains" (2014)
5. **Reliability Engineering**: Blischke & Murthy "Reliability: Modeling, Prediction, and Optimization" (2000)

## License

MIT License - See LICENSE file for details.

---

*For technical support or feature requests, please open an issue in the project repository.*
