# LEYP-Water — Model Assumptions

Every parameter that affects a computed cost, as set in the code on 2026-11-11, after the
owner set `HAZARD_LENGTH_SCALE` = 8,600 and CIP = $25 per inch-ft (`leyp_config.py`,
`optimizer_config.yaml`). Values marked *derived* were computed by
running the model, not read from config. All dollars are **constant 2026 dollars**: the
model has no inflation, and its 3% is a real discount rate.

---

## 1. Unit costs

| Parameter | Value | Applies to | Feeds |
|---|---:|---|---|
| `CIP_REPLACEMENT_COST_PER_INCH_FT` | **$25 per inch-diameter per ft** | Every planned replacement | Investment objective |
| `EMERGENCY_REPLACEMENT_COST_PER_FT` | **$5,000 per ft**, any diameter | Replacement after a segment fails | Risk objective, and the consequence term in ranking |
| `EMERGENCY_REPAIR_COST_PER_BREAK` | **$5,000 per break** | Point repair on every break (demand maintenance) | Risk objective |
| `DEFAULT_REPLACEMENT_MATERIAL` | HDPE | Every replacement, planned or emergency | Future hazard of renewed pipe |

### Unit costs by diameter in the Louisa inventory

| Diameter | Pipes | Miles | CIP $/ft | Emergency $/ft | Emergency premium |
|---:|---:|---:|---:|---:|---:|
| 1.5 in | 5 | 0.45 | $37.50 | $5,000 | 133× |
| 2 in | 17 | 0.92 | $50 | $5,000 | 100× |
| 3 in | 2 | 0.07 | $75 | $5,000 | 67× |
| **6 in** | **321** | **17.45** | **$150** | **$5,000** | **33×** |
| 8 in | 31 | 1.69 | $200 | $5,000 | 25× |
| 10 in | 11 | 0.83 | $250 | $5,000 | 20× |
| 12 in | 1 | 0.34 | $300 | $5,000 | 17× |

| Network figure (*derived*) | Value |
|---|---:|
| Length-weighted CIP rate | $153 per ft |
| Renew the whole network once by CIP | $17.6M |
| Renew the whole network once by emergency replacement | $574.0M |

**Consequence for ranking.** Priority is risk reduction per dollar:
`CoF × $5,000 ÷ remaining life ÷ ($25 × diameter)`. Length cancels, and because the
emergency rate is flat while CIP cost rises with diameter, **priority is proportional to
1 ÷ diameter**. Small mains are favoured: the pipes the committed knee plan replaces average
$150/ft against the network's $153/ft. The CIP rate scales every priority equally, so it
does not change the ranking.

An emergency premium of 17–133× is far above the 2–5× typical of utility practice (it was
3.5–28× at the earlier $120 per inch-ft), so planned renewal is strongly favoured.

---

## 2. Economic and policy parameters

| Parameter | Value | Notes |
|---|---:|---|
| `discount_rate` / `DISCOUNT_RATE` | **3% real** | Both objectives are present values at this rate |
| Inflation | **not modelled** | See the inflation section of the review discussion: combined consistently (6.09% nominal), it changes year-of-spend dollars but not PV or the chosen plan |
| `SIMULATION_YEARS` | 100 | Planning horizon |
| Annuity factor, 3% / 100 yr (*derived*) | 31.60 | PV ÷ 31.60 = level annual equivalent. Not PV ÷ 100 |
| Budget gene | $10,000 – $2,000,000 per yr | Searched by NSGA-II |
| Rehab-trigger gene | 1.0 – 3.5 | Condition at or below which a pipe is eligible for **replacement** (not rehab) |
| `selection` | **knee** (current config) | `min_total_cost` if unset; or `max_marginal_return`. See section 9 |
| `plateau_tolerance` | 5% | Width of the plateau of near-minimum strategies. See section 9 |
| `simulation.n_replicates` | 8 | Each strategy's score is the mean of 8 stochastic runs |
| `simulation.seed` | 20260830 | Same draws for every candidate (common random numbers) |
| Search size | 32 population, 16 offspring, 30 generations, seed 895 | About 512 evaluations |

---

## 3. Background break rates

### Calibration

| Parameter | Value |
|---|---:|
| `HAZARD_LENGTH_SCALE` | **8,600**, set by the owner: twice the 4,300 derived below |
| Calibration scenario | Louisa inventory as it stands, no CIP, no renewal, `CALIBRATION_WINDOW_YEARS` = **10** |
| Derivation target (gave 4,300) | 0.287 breaks / mile / yr |
| Achieved at 8,600 (8 replicates, *derived*) | **0.128 breaks / mile / yr** |
| Anchor behind the target | National averages weighted by this material mix, 0.077 / mile / yr, scaled up for an ageing network — verify against the source before citing |

Expected breaks per mile-year for a pipe = `hazard × 5,280 ÷ 8,600` = `0.614 × hazard`.

### Today's network, by material (*derived*, 10-year untreated window, 8 replicates)

| Material | Pipes | Miles | Length-weighted age | × average life | Breaks / mi / yr | Share of length | Share of breaks |
|---|---:|---:|---:|---:|---:|---:|---:|
| GI | 19 | 1.11 | 77 yr | **1.55** | **1.210** | 5% | **48%** |
| AC | 84 | 4.57 | 76 yr | **1.27** | **0.274** | 21% | **45%** |
| CI | 37 | 1.04 | 92 yr | 1.23 | 0.096 | 5% | 4% |
| DI | 209 | 12.00 | 43 yr | 0.43 | 0.004 | 55% | 2% |
| PVC | 36 | 2.87 | 60 yr | 0.50 | 0.009 | 13% | 1% |
| Default | 3 | 0.16 | 80 yr | 0.94 | 0.079 | 1% | 0% |
| **Network** | **388** | **21.74** | | | **0.128** | 100% | 100% |

GI and AC — 26% of the length, all past average life and therefore in wear-out — produce
93% of the breaks.

### Rate implied by the hazard, by material and age (*derived*)

6-inch main, no prior breaks. Columns are age as a multiple of the material's average life.

| Material | Average life | 0.25× | 0.5× | 0.75× | 1.0× | 1.25× | 1.5× | 2.0× |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| GI | 50 yr | 0.010 | 0.023 | 0.037 | 0.053 | 0.314 | 0.575 | 1.067 |
| AC | 60 yr | 0.007 | 0.014 | 0.020 | 0.027 | 0.155 | 0.273 | 0.478 |
| CI | 75 yr | 0.006 | 0.010 | 0.013 | 0.017 | 0.092 | 0.157 | 0.260 |
| DI | 100 yr | 0.005 | 0.007 | 0.008 | 0.010 | 0.049 | 0.078 | 0.119 |
| PVC | 120 yr | 0.003 | 0.004 | 0.004 | 0.004 | 0.018 | 0.027 | 0.036 |
| HDPE (replacement) | 150 yr | 0.003 | 0.003 | 0.003 | 0.003 | 0.012 | 0.018 | 0.024 |
| Default | 85 yr | 0.005 | 0.007 | 0.008 | 0.008 | 0.040 | 0.063 | 0.090 |

Breaks per mile per year. The jump between 1.0× and 1.25× is the wear-out term switching on.
Prior breaks multiply these rates by up to 2.5× (section 5).

---

## 4. Material parameters

| Material | Weibull β | Weibull η (yr) | Hazard multiplier | Average life, `base_life` (yr) | Time-to-failure mean ± sd (yr) |
|---|---:|---:|---:|---:|---:|
| GI | 2.2 | 45 | 1.75 | 50 | 40 ± 15 |
| AC | 2.0 | 60 | 1.50 | 60 | 45 ± 15 |
| CI | 1.8 | 75 | 1.30 | 75 | 60 ± 25 |
| PCCP | 1.6 | 70 | 1.20 | 75 | 55 ± 20 |
| Steel | 1.4 | 80 | 1.10 | 80 | 65 ± 25 |
| DI | 1.5 | 90 | 1.00 | 100 | 75 ± 20 |
| Default | 1.3 | 85 | 1.00 | 85 | 70 ± 20 |
| CU | 1.2 | 85 | 0.90 | 85 | 70 ± 22 |
| PVC | 1.1 | 110 | 0.70 | 120 | 90 ± 18 |
| HDPE | 1.0 | 120 | 0.60 | 150 | 100 ± 15 |

Two clocks, by decision: **average life** drives initial condition and wear-out onset;
**time-to-failure** (sampled lognormal per pipe) drives condition decay. The second runs
about 25% faster. PCCP, Steel and CU are configured but absent from the Louisa inventory.

---

## 5. Hazard modifiers

```
hazard = (β/η)(age/η)^(β−1) × material multiplier × exp(−0.02 × diameter)
         × (1 + 0.15 × min(prior breaks, 10)) × wear-out(age ÷ average life)
```

| Parameter | Value | Effect |
|---|---:|---|
| `COEFF_DIAMETER` | −0.02 per inch | ×0.970 at 1.5 in · ×0.887 at 6 in · ×0.787 at 12 in |
| `ALPHA` | 0.15 | +15% hazard per prior break |
| `LEYP_BREAK_FEEDBACK_CAP` | 10 breaks | Break-history multiplier tops out at **2.5×** |
| `WEAROUT_ONSET_LIFE_RATIO` | 1.0 | Wear-out starts at average life |
| `WEAROUT_MAX_MULTIPLIER` | 10 | Ceiling of the wear-out multiplier |
| `WEAROUT_STEEPNESS` | 2.0 | How fast it rises past onset |

| Age ÷ average life | ≤ 1.0 | 1.1 | 1.25 | 1.5 | 2.0 | 3.0 |
|---|---:|---:|---:|---:|---:|---:|
| Wear-out multiplier | 1.00× | 2.63× | 4.54× | 6.69× | 8.78× | 9.84× |

---

## 6. Condition, failure and repair rules

| Parameter | Value | Effect |
|---|---:|---|
| Condition scale | 1 (failed) – 6 (new) | |
| Initial condition | `1 + 5·exp(−1.5 × age ÷ average life)`, raised by the CSV era flag | Asymptotic: age alone cannot reach 1.0 |
| Seeded break history | Up to 6 × min(1, age ÷ average life), random, **capped at 2 per segment** | Drives the break-history term; cannot condemn a pipe at year 0 |
| `N_SEGMENTS_PER_PIPE` | 4 | Each pipe is four virtual segments |
| `SEGMENT_BREAK_THRESHOLD` | **3** | Any **one** segment reaching 3 breaks → emergency replacement. The only route to emergency work |
| `BREAK_CONDITION_PENALTY` | −0.3 condition per break | Makes frequently breaking pipes CIP-eligible sooner |
| `BREAK_DAMAGE_CONDITION_FLOOR` | 1.05 | Break damage alone cannot reach the failure floor |
| Condition floor | 1.0 | A pipe at the floor stays in service and is counted, not replaced |
| `BACKLOG_CONDITION` | 2.0 | Reporting only: defines "inherited backlog" in the summary |

---

## 7. Which output each parameter drives

| Drives | Parameters |
|---|---|
| **Investment cost** (PV) | CIP $/inch-ft · pipe diameter and length · budget gene · rehab-trigger gene · discount rate |
| **Risk cost** (PV) | Emergency $/ft · repair $/break · every hazard parameter (sections 3–5) · segment threshold · discount rate |
| **Which pipes are replaced, not what they cost** | CoF · remaining life (condition, time-to-failure) · diameter (through the 1 ÷ diameter priority) |
| **Run-to-run spread, not the expected value** | Replicate count · seeds |

CoF enters only the ranking. It never multiplies a dollar figure: a CoF-3 pipe costs the
same to replace or repair as a CoF-1 pipe.

### Present in the config but inert

| Parameter | Status |
|---|---|
| `SIMULATION_START_YEAR` (2026) | Not read anywhere |
| `STANDARD_LIFE` `min_life` / `max_life` | Not read; only `base_life` is used |
| `ANNUAL_BUDGET` ($50,000) | Default for single runs of `leyp_runner.py` only; the optimizer always supplies its own budget |
| `TRIGGERS["Rehab"]` (2.0) | Default for single runs only; the optimizer always supplies its own trigger |
| `map_condition_to_n_start` | Imported, never called |

---

## 8. Limits to keep in view when citing these

- **Only the network total break rate is calibrated.** The split between materials comes
  from the parameters in sections 4–5, not from data, and the scale (8,600) is nearly
  interchangeable with the wear-out ceiling (`OPEN_ITEMS.md` MOD-4). Louisa's own break
  records by material and age would pin both (DAT-1).
- **The emergency rate is flat across diameters**, so the emergency premium runs from 17×
  (12 in) to 133× (1.5 in). That is what drives the 1 ÷ diameter priority.
- **The discount rate and both emergency costs are placeholders**, not Louisa's figures
  (`OPEN_ITEMS.md` DEC-2). Repairs are under 1% of risk cost at $5,000 a break (MOD-2).
- **No rehab or lining option exists**; every planned intervention is a full replacement
  (`docs/CIP_PROCESS_FLOW.md`).

---

## 9. Optimizer outputs — selection rules, plateau and reporting

Output work only: none of this changes the objectives, the physics or the search. The same
seeds give the same front, and the same rule picks the same strategy.

| Rule (`selection`) | Picks | Reading |
|---|---|---|
| `min_total_cost` | Lowest investment + risk | Where the front's slope is −1: a dollar invested avoids a dollar of risk |
| `knee` | Closest to the ideal point after min–max normalising both objectives | Best-balanced trade-off |
| `max_marginal_return` | Steepest stretch of the front: most risk avoided per extra dollar, slope over a sliding 5-point window, assigned to the window's centre | Best value per dollar; usually low investment, high remaining risk. Not a spend level |

| Term | Definition |
|---|---|
| Plateau | Front strategies with total within `plateau_tolerance` (5%) of the minimum total. Interchangeable to within the tolerance; shaded on the charts, summarised as a row of `selected_strategies.csv` |
| Basis of the objectives | Present value at `discount_rate` (3% real, end-of-year); the chart axes and `selected_strategies.csv` state the rate the run used. A run with rate 0 is labelled "nominal" |
| Average annual spend | Nominal 100-year investment ÷ 100. Always nominal; never the budget gene, never a present value |
| Nominal sums | Each front strategy is re-simulated with the optimizer's seed and replicate count, which reproduces its present values exactly, and the undiscounted investment and risk are kept beside them |
| Risk avoided per dollar invested | The windowed slope of the front at a strategy; ≈ 1 at the minimum total, largest at `max_marginal_return` |
| Inherited backlog | Pipes at condition ≤ `BACKLOG_CONDITION` in year 1 and their length. A property of the inventory, the same for every strategy; reported, not optimised |

`selection` decides which strategy drives `Optimal_Action_Plan.csv` and which is flagged
"— selected"; all three are always marked and summarised. `--plot-only` re-applies the
current `selection` and `plateau_tolerance` to the saved front without re-optimising, and
does not rewrite the action plan.

