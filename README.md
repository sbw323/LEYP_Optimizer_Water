# LEYP-Water: Water Main Replacement Planning Optimizer

## Overview

**LEYP-Water** is a Monte Carlo simulation and multi-objective optimization framework for generating optimized water main replacement plans. It models pipe degradation physics, simulates break events over a 100-year planning horizon, applies replacement strategies within budget constraints, and uses the NSGA-II genetic algorithm to discover Pareto-optimal trade-offs between investment cost and emergency risk cost.

The system features crash-safe checkpointing for preemptible cloud VMs, ensuring optimization can resume from interruptions without loss of progress. All file outputs use atomic writes to prevent corruption during preemption events.

The package is inspired by the **LEYP (Linear Extension of the Yule Process)** statistical model originally developed by Cemagref/IRSTEA for water main break prediction. This implementation adapts the core concepts — Weibull baseline hazard, material covariates, and the "previous breaks increase future break rate" feedback loop — into a forward-looking simulation engine suitable for water utility capital improvement planning.

---

## What It Does

Given a pipe inventory CSV with attributes (ID, age, condition score, material, diameter, length, and consequence-of-failure), the package:

1. **Preprocesses** the network into analysis-ready segments (optional segmentation for long pipes).
2. **Simulates** 100 years of pipe life, including stochastic degradation, Poisson-distributed break events on virtual sub-segments, and condition-driven failure.
3. **Applies investment logic** each year: budget-constrained replacement prioritized by annualized risk (consequence/time-to-failure ratio).
4. **Optimizes** two decision variables simultaneously via NSGA-II to minimize both total investment spend and total emergency cost across the planning horizon.
5. **Outputs** a Pareto front of strategies, a detailed year-by-year action plan for the best-found solution, and validation curves showing performance metrics.

---

## Package Architecture

The package consists of seven Python modules and one YAML configuration file, organized in a linear pipeline with an optimization wrapper.

### Module Summary

| Module | Role | Key Responsibility |
|---|---|---|
| `leyp_config.py` | Configuration & Constants | All tunable parameters: water material properties, Weibull parameters, degradation physics, cost models, replacement triggers, and checkpoint settings. |
| `leyp_preprocessor.py` | Data Preparation | Reads raw pipe inventory CSV, standardizes column headers, optionally segments long pipes into ~25 ft analysis units, and writes an optimized input file. |
| `leyp_core.py` | Physics Engine | Defines the `Pipe` and `VirtualSegment` classes. Implements Weibull hazard calculation, lognormal time-to-failure sampling, exponential condition degradation, and Poisson break simulation on four virtual sub-segments per pipe. |
| `water_replacement.py` | Replacement Decision Engine | The `ReplacementManager` class implements budget-constrained, risk-based replacement prioritization. Uses annualized risk to rank pipes for full replacement within annual budget limits. |
| `leyp_runner.py` | Simulation Executor | Orchestrates a single 100-year simulation run: loads data → initializes `Pipe` objects → loops (degrade → planned replacements → simulate breaks/emergencies) → accumulates investment and risk costs → returns summary or detailed action log. |
| `leyp_optimizer.py` | NSGA-II Optimization Wrapper | Defines a `pymoo` `ElementwiseProblem` with 2 genes (budget, replacement trigger), 2 objectives (investment cost, risk cost), and 0 constraints. Runs evolutionary optimization with checkpoint/resume support and saves Pareto results plus optimal action plan. |
| `water_validation.py` | Performance Analysis | Generates validation curves showing "% breaks avoided vs. % pipes replaced" to demonstrate the effectiveness of different budget levels and validate optimization results. |
| `optimizer_config.yaml` | Optimization Settings | Gene bounds, algorithm hyperparameters (population size, generations, seed), file paths, and economic parameters. |

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

The system models common water main materials with appropriate physics parameters:

| Material | Base Life (years) | Weibull β | Weibull η | Notes |
|---|---|---|---|---|
| HDPE | 150 | 1.0 | 120 | Most durable plastic |
| PVC | 120 | 1.1 | 110 | Standard plastic pipe |
| DIP (Ductile Iron) | 100 | 1.5 | 90 | Modern iron standard |
| Steel | 80 | 1.4 | 80 | Carbon steel |
| Cast Iron (CI) | 75 | 1.8 | 75 | Legacy iron pipe |
| PCCP | 75 | 1.6 | 70 | Prestressed concrete |
| Copper (CU) | 85 | 1.2 | 85 | Service connections |
| Asbestos Cement (AC) | 60 | 2.0 | 60 | Legacy material |

### Initialization Physics

For existing pipes, initial condition is interpolated based on age:

```
life_fraction = clamp(age / standard_life[material], 0, 1)
initial_condition = 6.0 - (5.0 * life_fraction)
```

Break history is seeded proportionally to age, distributed uniformly across virtual sub-segments.

### Degradation Model

Each pipe is assigned a **lognormal time-to-failure (TTF)** sampled from material-specific distributions. The condition decays exponentially:

```
condition(t+dt) = condition(t) × exp(-degradation_rate × dt)
```

where `degradation_rate = ln(6.0) / TTF`. This ensures a pipe starting at condition 6 reaches condition 1 at its sampled TTF.

### Hazard & Break Simulation (LEYP-Inspired)

The annual hazard rate for each pipe follows a **Weibull baseline** with covariate adjustment:

```
h(t) = (β/η) × (t/η)^(β-1) × material_multiplier × exp(coeff_diameter × diameter) × (1 + α × n_breaks)
```

Key parameters:
- **β (shape)** and **η (scale)**: Material-specific Weibull parameters controlling how quickly hazard rises with age.
- **α**: The LEYP feedback parameter — each prior break multiplicatively increases future hazard (default 0.15).
- **n_breaks**: Cumulative break count from both seeded history and simulated events.

Each pipe is divided into **4 virtual sub-segments**. Breaks are drawn from a Poisson process at each segment. If 3+ segments accumulate breaks, the pipe is declared failed (condition forced to 1.0).

### Three-Cost-Stream Model

The optimization considers three distinct cost categories for water main management:

1. **Investment Cost**: Planned CIP replacement spending ($120/inch-ft based on pipe diameter)
2. **Emergency Repair Cost**: Point repairs for individual breaks ($5,000/break)
3. **Emergency Replacement Cost**: Full replacement of failed pipes ($800/ft)

**Objective 1** (Investment) = Sum of planned CIP replacement costs
**Objective 2** (Risk) = Sum of emergency repair costs + emergency replacement costs

This replaces traditional single-action sewer models with water-specific cost structures and material considerations.

---

## Preemption Handling & Cloud Deployment

**LEYP-Water** is designed for **Google Cloud Platform (GCP) spot VMs** with automatic checkpoint recovery from preemption events.

### Checkpoint Architecture

The system provides **three-layer checkpoint protection**:

1. **OptimizationCheckpoint**: NSGA-II algorithm state is pickled after every generation to `nsga2_checkpoint.pkl` (configurable via `NSGA2_CHECKPOINT_PATH` in `leyp_config.py`). On restart, evolution resumes from the last completed generation.

2. **SIGTERM Handler**: GCP spot VMs receive a 30-second warning via SIGTERM. The handler saves checkpoint state and exits with code 3 (`EXIT_PREEMPTED`).

3. **Atomic File Writes**: All output files (CSV, action plans, results) use atomic write operations (`safe_write_file`) to prevent corruption during mid-write preemption.

### Preemption Protocol

When a spot VM is preempted:

1. **SIGTERM received** → checkpoint handler triggers
2. **State saved** to `nsga2_checkpoint.pkl` 
3. **Process exits** with code 3
4. **Outer runner detects** exit code 3 and restarts the process
5. **Optimization resumes** from the last completed generation
6. **Checkpoint cleaned up** after successful completion

### Configuration Parameters

```python
# leyp_config.py
NSGA2_CHECKPOINT_PATH = "nsga2_checkpoint.pkl"       # Checkpoint file path
NSGA2_CHECKPOINT_EVERY_N_GEN = 1                     # Save frequency (generations)
```

### GCP Deployment Options

**Option 1: systemd Service (Recommended)**
```ini
[Unit]
Description=LEYP-Water Optimizer
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/leyp-water/leyp_optimizer.py
Restart=on-failure
RestartPreventExitStatus=0 1 2
# Exit code 3 = preempted, triggers restart
# Exit codes 0,1,2 = success/error, no restart
```

**Option 2: Startup Script Loop**
```bash
#!/bin/bash
while true; do
    python3 /opt/leyp-water/leyp_optimizer.py
    exit_code=$?
    if [ $exit_code -ne 3 ]; then
        # Exit codes 0,1,2 = finished/error, exit loop
        echo "Optimizer completed with exit code $exit_code"
        break
    fi
    echo "Preemption detected (exit code 3), restarting..."
    sleep 5
done
```

**Startup Script Installation (GCP VM):**
```bash
# /var/lib/cloud/scripts/per-boot/leyp-optimizer.sh
gcloud compute instances add-metadata $(hostname) \
    --metadata startup-script='#!/bin/bash
cd /opt/leyp-water
while true; do
    python3 leyp_optimizer.py 2>&1 | tee -a optimizer.log
    if [ ${PIPESTATUS[0]} -ne 3 ]; then break; fi
    echo "Restarting after preemption..." | tee -a optimizer.log
done'
```

### Verification Commands

**Check checkpoint status:**
```bash
# Verify checkpoint file during optimization
python3 -c "
from config.checkpoint import inspect_checkpoint
inspect_checkpoint('nsga2_checkpoint.pkl', mode='pickle')
"

# Verify atomic output writes
grep -r 'safe_write_file' leyp_optimizer.py leyp_runner.py
# Should show all CSV/JSON output using atomic writes
```

---

### Replacement Logic

The `ReplacementManager` implements a single-action strategy:

1. **Eligibility Filter**: Pipes with condition ≤ replacement trigger (default 2.0) and not already failed (condition > 1.001).

2. **Risk-Based Prioritization**: Pipes ranked by annualized risk:
   ```
   Annualized_Risk = (Length × CoF × Emergency_Replacement_Cost) / max(0.1, TTF_remaining)
   ```

3. **Budget-Constrained Execution**: Highest-risk pipes replaced until annual budget exhausted.

4. **Full State Reset**: Replaced pipes get condition 6.0, new material (default HDPE), negative age (indicating replacement year), and fresh virtual segments.

### NSGA-II Optimization

The optimizer searches a 2-dimensional decision space:

| Gene | Description | Default Range |
|---|---|---|
| `budget` | Annual CIP replacement budget ($) | $10,000 – $2,000,000 |
| `rehab_trigger` | Condition threshold for replacement eligibility | 1.0 – 3.5 |

The algorithm discovers Pareto-optimal trade-offs between:
- **Investment Cost**: Total planned replacement spending over 100 years
- **Risk Cost**: Total emergency repairs and replacements over 100 years

---

## Preemption Handling & Checkpointing

The system is designed for deployment on preemptible cloud VMs (GCP spot instances) with automatic checkpoint/resume capability:

### Signal Handling
- **SIGTERM Detection**: GCP sends SIGTERM 30 seconds before VM termination
- **Graceful Exit**: Signal handler saves current state and exits with code 3
- **Automatic Restart**: Outer runner (systemd, startup script) detects exit code 3 and restarts process

### NSGA-II State Persistence
- **Generation-Level Checkpointing**: Algorithm state pickled after every generation to `nsga2_checkpoint.pkl`
- **Resume Detection**: On startup, if checkpoint file exists, optimization resumes from last completed generation
- **Cleanup**: Checkpoint file automatically deleted after successful completion

### Atomic File Operations
- **Safe Writes**: All output files written via `safe_write_file()` (temp file + atomic rename)
- **No Corruption**: VM preemption during file write never produces corrupted output
- **CSV Protection**: Results, action plans, and validation data are crash-safe

### Configuration Parameters
```python
# In leyp_config.py
NSGA2_CHECKPOINT_PATH = "nsga2_checkpoint.pkl"
NSGA2_CHECKPOINT_EVERY_N_GEN = 1
```

### GCP Deployment Example

**Systemd Unit** (`/etc/systemd/system/leyp-optimizer.service`):
```ini
[Unit]
Description=LEYP Water Main Optimizer
After=network.target

[Service]
Type=simple
User=leyp
WorkingDirectory=/opt/leyp-water
ExecStart=/opt/leyp-water/venv/bin/python leyp_optimizer.py
Restart=on-failure
RestartPreventExitStatus=0 1 2
# Exit code 3 (preempted) triggers restart, others don't

[Install]
WantedBy=multi-user.target
```

**Startup Script Alternative**:
```bash
#!/bin/bash
while true; do
    python leyp_optimizer.py
    exit_code=$?
    if [ $exit_code -ne 3 ]; then
        break  # Success or permanent failure, don't restart
    fi
    echo "Preemption detected (exit code 3), restarting..."
    sleep 5
done
```

---

## Installation & Usage

### Prerequisites

```bash
pip install pandas numpy scipy matplotlib pyyaml pymoo
```

### Basic Usage

1. **Prepare Input Data**: CSV with columns matching `COLUMN_MAP` in `leyp_config.py`
2. **Configure Optimization**: Edit `optimizer_config.yaml` for gene bounds and algorithm settings
3. **Run Optimization**: 
   ```bash
   python leyp_optimizer.py
   ```
4. **Review Results**: Check `Optimization_Results_NSGA2/` directory for Pareto front, action plan, and validation curves

### Key Output Files

| File | Description |
|---|---|
| `nsga2_results.csv` | Pareto front with Investment_Cost, Risk_Cost, Total_Cost, Budget, Rehab_Trigger |
| `Optimal_Action_Plan.csv` | Year-by-year replacement schedule for best strategy |
| `optimization_curve.png` | Investment vs. Risk cost curves with Pareto front |
| `validation_curve.png` | "% breaks avoided vs. % pipes replaced" performance plot |
| `validation_data.csv` | Raw data for validation curve analysis |

---

## Configuration Reference

### Key Parameters in `leyp_config.py`

**Cost Model**:
```python
CIP_REPLACEMENT_COST_PER_INCH_FT = 120.00    # Planned replacement cost
EMERGENCY_REPAIR_COST_PER_BREAK = 5000.00    # Point repair cost  
EMERGENCY_REPLACEMENT_COST_PER_FT = 800.00   # Emergency replacement cost
DEFAULT_REPLACEMENT_MATERIAL = "HDPE"        # New pipe material
```

**Physics Constants**:
```python
ALPHA = 0.15                    # LEYP break-feedback parameter
COEFF_DIAMETER = -0.02          # Diameter effect on hazard
N_SEGMENTS_PER_PIPE = 4         # Virtual sub-segments per pipe
SEGMENT_BREAK_THRESHOLD = 3     # Segments with breaks before pipe fails
SIMULATION_YEARS = 100          # Planning horizon
```

**Trigger Thresholds**:
```python
TRIGGERS = {"Rehab": 2.0}       # Default replacement eligibility threshold
```

### Gene Bounds in `optimizer_config.yaml`

```yaml
genes:
  budget: 
    min: 10000      # Minimum annual budget
    max: 2000000    # Maximum annual budget
  rehab_trigger: 
    min: 1.0        # Replace only failed pipes
    max: 3.5        # Replace pipes in moderate condition

algorithm:
  pop_size: 5       # Population size (small for testing)
  n_offsprings: 3   # Offspring per generation  
  n_gen: 15         # Number of generations
  seed: 1027895609238  # Random seed for reproducibility
```

---

## Validation & Quality Assurance

### Validation Curve Analysis

The system generates validation curves showing the relationship between investment level and system performance:

- **X-axis**: Percentage of pipes replaced over 100 years
- **Y-axis**: Percentage of potential breaks avoided
- **Diagonal**: Random replacement (no prioritization)
- **Above Diagonal**: Risk-based prioritization effectiveness

A well-functioning optimization should show curves **above the diagonal** for the first 50% of replacement activity, indicating that risk-based prioritization is identifying the highest-impact pipes first.

### Sensitivity Analysis

Key parameters for sensitivity testing:
- `SEGMENT_BREAK_THRESHOLD`: Lower values = more pipe failures = higher emergency costs
- Emergency cost ratios: Higher emergency costs = higher optimal budgets
- Material parameters: Different degradation rates affect replacement timing

### Integration Testing

Run complete pipeline validation:
```bash
python leyp_optimizer.py && pytest tests/ -v
```

Expected outcomes:
- Pareto front showing Investment ↑, Risk ↓, Total Cost U-shaped
- Validation curve above diagonal for first 50% of replacements  
- All 80+ unit tests passing
- No checkpoint files remaining after successful completion

---

## Technical Architecture Details

### Simulation Flow

```
CSV Input → leyp_preprocessor → leyp_core (Pipe objects) → water_replacement (ReplacementManager) 
    ↓
leyp_runner (100-year Monte Carlo) → leyp_optimizer (NSGA-II wrapper) → Results Output
```

### State Management

- **No Shared State**: Each optimization evaluation creates fresh Pipe objects
- **Independent Runs**: Evaluations can be parallelized without side effects  
- **Deterministic**: Same random seed produces identical results
- **Atomic Updates**: All pipe state changes are immediate and consistent

### Performance Characteristics

- **Memory Usage**: ~1MB per 1000 pipes (Pipe objects + segments)
- **Runtime**: ~100ms per evaluation on modern hardware (depends on network size)
- **Scaling**: Linear in pipe count, quadratic in planning horizon
- **Checkpoint Overhead**: <1% performance impact for generation-level saves

---

## Troubleshooting

### Common Issues

**Issue**: "FileNotFoundError: Missing config file"  
**Solution**: Ensure `optimizer_config.yaml` exists in working directory

**Issue**: Optimization stops with "population has no feasible solution"  
**Solution**: Check gene bounds - budget may be too low for any replacements

**Issue**: Validation curve below diagonal  
**Solution**: Risk calculation may be incorrect - verify CoF values and cost parameters

**Issue**: Checkpoint not resuming after interruption  
**Solution**: Verify `nsga2_checkpoint.pkl` exists and `NSGA2_CHECKPOINT_PATH` configuration

### Debug Mode

Enable verbose output by modifying `optimizer_config.yaml`:
```yaml
algorithm:
  verbose: true   # Shows generation-by-generation progress
```

Add logging to `leyp_runner.py`:
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

---

## Preemption Handling for Cloud Computing

LEYP-Water is designed for deployment on preemptible cloud instances (GCP spot VMs, AWS spot instances) with comprehensive crash-safe checkpointing:

### SIGTERM Handler
- Catches the 30-second termination warning from cloud providers
- Saves optimization state to checkpoint files
- Exits with code 3 (`EXIT_PREEMPTED`) to signal restart requirement

### NSGA-II Algorithm Checkpointing
- Pickles complete algorithm state after every generation (configurable via `NSGA2_CHECKPOINT_EVERY_N_GEN`)
- Checkpoint file: `nsga2_checkpoint.pkl` (configurable via `NSGA2_CHECKPOINT_PATH`)
- On restart: `restore_or_create()` loads the pickle and resumes evolution from the last completed generation
- Console displays: `"Resuming from generation N"` where N > 0
- Cleanup: checkpoint pickle is automatically deleted after successful completion

### Atomic File Writes
All output files use `safe_write_file()` which:
- Writes to temporary file first
- Performs `fsync()` to ensure data is on disk
- Atomically renames to final destination via `os.replace()`
- Ensures no corrupted half-written files during preemption

### Restart Protocol
1. Outer runner (systemd, supervisor, or startup script) monitors exit codes
2. Exit code 3 triggers automatic restart
3. Optimization resumes from last checkpoint automatically
4. Final output files are identical to non-interrupted runs

### GCP Spot VM Deployment

#### Recommended: Systemd Unit
```ini
[Unit]
Description=LEYP Water Optimizer
After=network.target

[Service]
Type=simple
User=leyp
WorkingDirectory=/home/leyp/leyp-water
ExecStart=/home/leyp/miniforge3/envs/leyp/bin/python leyp_optimizer.py
Restart=on-failure
RestartPreventExitStatus=0 1 2
# Only restart on exit code 3 (preemption) or 4 (integrity failure)
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

#### Alternative: GCP Startup Script
```bash
#!/bin/bash
cd /opt/leyp-water
while true; do
    python leyp_optimizer.py
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 0 ] || [ $EXIT_CODE -eq 1 ] || [ $EXIT_CODE -eq 2 ]; then
        echo "Optimization completed with exit code $EXIT_CODE"
        break
    elif [ $EXIT_CODE -eq 3 ]; then
        echo "Preempted (exit code 3) - restarting..."
        sleep 5
    else
        echo "Unexpected exit code $EXIT_CODE - stopping"
        break
    fi
done
```

### Configuration Parameters
```python
# In leyp_config.py
NSGA2_CHECKPOINT_PATH = "nsga2_checkpoint.pkl"
NSGA2_CHECKPOINT_EVERY_N_GEN = 1
```

---

## Contributing & Development

### Code Standards

- **Python 3.10+** with type hints and Google-style docstrings
- **PEP 8** formatting with meaningful variable names
- **Unit tests** for all physics calculations and investment logic
- **No magic numbers** — all parameters in `leyp_config.py`

### Adding New Materials

1. Add material properties to `MATERIAL_PROPS`, `DEGRADATION_PARAMS`, and `STANDARD_LIFE` in `leyp_config.py`
2. Update `DEFAULT_REPLACEMENT_MATERIAL` if desired
3. Add test case to `tests/test_leyp_core.py::test_all_materials_work`

### Extending Cost Models

1. Modify cost calculation in `water_replacement.py::ReplacementManager.calculate_cost()`
2. Update economic parameters in `leyp_config.py`
3. Add corresponding tests to `tests/test_water_replacement.py`

### Custom Optimization Objectives

1. Extend `leyp_optimizer.py::Water_LEYP_Problem` with additional objectives
2. Modify `leyp_runner.py` return values to include new metrics
3. Update results processing and visualization code

---

## References & Citations

1. **LEYP Model**: Cemagref/IRSTEA *Casses* software documentation for water main break prediction
2. **NSGA-II**: K. Deb et al. "A fast and elitist multiobjective genetic algorithm: NSGA-II" (2002)
3. **Water Asset Management**: AWWA Manual M28 "Rehabilitation of Water Mains" (2014)
4. **Reliability Engineering**: Blischke & Murthy "Reliability: Modeling, Prediction, and Optimization" (2000)

## License

MIT License - See LICENSE file for details.

---

*For technical support or feature requests, please open an issue in the project repository.*