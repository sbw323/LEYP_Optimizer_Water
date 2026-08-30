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
```

- **β, η**: Material-specific Weibull parameters.
- **α**: LEYP feedback parameter (default 0.15).
- **`LEYP_BREAK_FEEDBACK_CAP`**: Ceiling on the break count entering the feedback term. Hazard elevation from break history saturates in practice. **Without a ceiling the term is an unbounded positive feedback loop** — each break raises hazard, which produces more breaks — and it diverges in any run where pipes are not renewed. Renewal masks this: an untreated baseline reached ~10⁷ breaks/mile/year before the cap was added.

Expected breaks per segment per year are `h(t) × segment_length / HAZARD_LENGTH_SCALE`. Each pipe has **4 virtual sub-segments**.

**Failure rule:** a pipe is condemned when **any single segment** reaches `SEGMENT_BREAK_THRESHOLD` breaks (default 3). Break damage also reduces condition by `BREAK_CONDITION_PENALTY` per break, but is floored at `BREAK_DAMAGE_CONDITION_FLOOR` — that coupling exists to make a frequently-breaking pipe read as poor condition and become CIP-eligible, not to act as a third independent route to failure alongside age-out and the segment rule.

### Calibration

`HAZARD_LENGTH_SCALE` is a pure units constant with no physical claim of its own, which makes it the right knob for the model's absolute break production. It is calibrated so a **do-nothing run** (no CIP, no emergency renewal) of the reference network produces **0.287 breaks/mile/year**.

Anchor: Folkman (2018) national averages weighted by that network's material mix give 0.077 breaks/mile/year across all ages; an untreated network already 60% past design life and ageing a further century should sit several times above that. **Re-derive this constant if the inventory, material parameters, or `ALPHA` change.**

### Three-Cost-Stream Model

1. **Investment Cost**: Planned CIP replacement (`$120/inch-ft` × diameter × length)
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
1. PLANNED CIP        decided on condition at the last assessment
2. DEGRADE            apply natural ageing
3. EMERGENCY REPLACE  pipes the plan did not reach in time
4. BREAKS             Poisson events, repairs, break-driven failures
```

CIP decides **before** the year's degradation is applied. Ordering degradation first would let the program intercept a pipe in the very year it crossed the failure floor — perfect foresight plus same-year execution, which no capital program can schedule, and which makes replacing at the last possible moment look optimal.

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

One strategy is chosen from the front for the action plan, via `selection:`:

- `min_total_cost` (default) — lowest investment + risk. Both objectives are dollars, so this is a real quantity rather than an arbitrary weighting.
- `knee` — closest to the ideal point once both objectives are normalized; the best-balanced trade-off.

The run prints what the other rule would have chosen, and warns if the selected budget sits at the gene ceiling, since the optimum may lie above it. `nsga2_results.csv` carries `Norm_Investment`, `Norm_Risk`, and `Ideal_Distance` so the trade-off stays visible.

---

## Installation & Usage

```bash
pip install pandas numpy scipy matplotlib pyyaml pymoo
pip install pytest        # to run the test suite
```

```bash
python leyp_optimizer.py                      # full optimization + reports
python leyp_runner.py --output-dir results \
    --budget 1000000 --trigger 1.5 \
    --seed 20260830 --replicates 5            # single strategy
pytest                                        # 140 tests
```

### Key Output Files

| File | Description |
|---|---|
| `nsga2_results.csv` | Pareto front: objectives, genes, total cost, normalized position, distance to ideal |
| `Optimal_Action_Plan.csv` | Year-by-year schedule for the selected strategy |
| `cost_summary.csv` | Present values, undiscounted totals, and plan health metrics |
| `simulation_diagnostics.csv` | Per-year CIP/emergency/break activity, budget utilization, discount factors |
| `optimization_curve.png` | Investment vs. risk cost with the Pareto front |
| `validation_curve.png` | Three-panel validation |
| `validation_data.csv` | Validation curve data with replicate spread |

`Optimal_Action_Plan.csv` records `Action` (`CIP_Replacement`, `Emergency_Replacement`, `Break_Event`), `Cause` for emergencies (`degradation`, `break_failure`, `break_damage`), `Backlog` marking the first replacement of an inherited-backlog pipe, `Priority` (the ranking value, captured before the pipe is reset), and `Annualized_Risk`.

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
CIP_REPLACEMENT_COST_PER_INCH_FT = 120.00
EMERGENCY_REPAIR_COST_PER_BREAK = 5000.00
EMERGENCY_REPLACEMENT_COST_PER_FT = 5000.00
DEFAULT_REPLACEMENT_MATERIAL = "HDPE"

ALPHA = 0.15                    # LEYP break-feedback parameter
LEYP_BREAK_FEEDBACK_CAP = 10    # ceiling on feedback; without it the model diverges
COEFF_DIAMETER = -0.02
N_SEGMENTS_PER_PIPE = 4
SEGMENT_BREAK_THRESHOLD = 3     # breaks in ONE segment that condemn the pipe
HAZARD_LENGTH_SCALE = 1500.0    # calibrated; see Calibration
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

selection: min_total_cost   # or: knee

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
pytest                    # 140 tests
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

**Changing physics:** re-derive `HAZARD_LENGTH_SCALE` against the untreated break rate; `test_untreated_break_rate_is_defensible` pins the target band.

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
