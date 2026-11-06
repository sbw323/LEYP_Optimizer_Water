# End-of-Life Condition Modelling — Review and Implementation Plan

**Revision 2.** The hazard coupling in §3B is now **age-based** (the wear-out arm of the
bathtub curve), per the approved direction. Revision 1 proposed coupling to condition; its
experiment is retained in §2 as supporting evidence but is no longer the design.

**Approved so far.** Removal of both floor-triggered emergency replacements in
`leyp_runner.py` (steps 3 and 4). A hazard multiplier that rises with age beyond a pipe's
average lifespan, so that very old pipes are driven to failure through the physics rather
than by rule.

**Symptom reported.** The optimizer always drives the annual budget to the gene ceiling.
Raising the ceiling from $2M to $20M moved the selected budget to **$12.1M/yr** for a
21.7-mile, 388-pipe network — not pinned any more, but still "spend whatever it takes so
that no pipe ever reaches end of life."

**Finding.** A pipe reaching its minimum condition is emergency-replaced in that year even
though it has not broken. The behaviour exists in **two** places, and it exists because the
hazard model never reads condition or wear-out: without a forced rule a pipe at end of life
breaks no faster than a healthy pipe of the same age. The rule was a crutch standing in for
a missing mortality model.

---

## 1. Where the behaviour lives

### Site 1 — explicit sweep, `leyp_runner.py`, annual loop step 3

```python
# 3. EMERGENCY REPLACEMENT — pipes the plan did not reach in time
for pipe in network:
    if pipe.current_condition <= 1.001:
        _emergency_replace(pipe, year, CAUSE_DEGRADATION)
```

### Site 2 — the same trigger inside the break step, `leyp_runner.py`, step 4

```python
if not no_intervention and (
    sim_result["failed"] or pipe.current_condition <= 1.001
):
    cause = CAUSE_BREAK_FAILURE if sim_result["failed"] else CAUSE_BREAK_DAMAGE
    _emergency_replace(pipe, year, cause)
```

The `or pipe.current_condition <= 1.001` clause is a second copy of the same rule, invisible
today only because site 1 has already replaced those pipes. **Removing site 1 alone does
nothing** — verified: emergency counts unchanged (228 → 229), with the pipes caught one step
later and mislabelled `break_damage`.

### Why the rule was needed — `leyp_core.py`, `Pipe.calculate_hazard`

```python
h0 = (beta/eta) * (t/eta)**(beta-1)             # Weibull in age
cov_factor = mat_mult * exp(COEFF_DIAMETER * d)  # material, diameter
leyp_factor = 1 + ALPHA * min(n_breaks, CAP)     # break history
return h0 * cov_factor * leyp_factor
```

The Weibull term does rise with age (β > 1 for every material but HDPE), but far too
gently to constitute wear-out. Expected time for a pipe at end of life to trip the segment
rule (3 breaks in one of 4 segments, feedback ignored):

| Material, age | Hazard /yr | Years to trip the segment rule |
|---|---:|---:|
| GI, 80 | 0.242 | ~250 |
| AC, 90 | 0.126 | ~475 |
| DI, 96 | 0.022 | ~2,700 |
| PVC, 96 | 0.007 | ~8,500 |

So "end of life" has no mortality consequence in the hazard model. The forced replacement
was what gave it one.

### Why it pins the budget

Each pipe that reaches the floor without being renewed costs $5,000/ft immediately. The
only way to avoid that is to renew it first, so the optimizer buys enough budget to renew
everything before it ages out — and the cheapest way is always "more". Your latest run shows
the end state: **0 degradation emergencies, 428 CIP, 4 emergency**, at $12.1M/yr.

---

## 2. Evidence from runtime experiments

All runs: Louisa inventory, trigger 1.5, seed 20260830, 3 replicates, PV totals in $M.

### 2.1 Removing both sites, no hazard change

| Budget | Current: total | CIP | Emerg | No floor rule: total | CIP | Emerg | Breaks |
|---:|---:|---:|---:|---:|---:|---:|---:|
| $0 | 213.3 | 0 | 382 | **51.1** | 0 | 56 | 267 |
| $200k | 181.7 | 258 | 132 | 52.7 | 260 | 38 | 205 |
| $500k | 128.7 | 328 | 69 | **44.8** | 336 | 21 | 121 |
| $1M | 83.1 | 374 | 21 | 46.6 | 376 | 8 | 80 |
| $2M | 56.2 | 384 | 4 | 44.8 | 384 | 2 | 54 |
| $5M | 47.8 | 387 | 1 | 47.8 | 387 | 1 | 48 |

Every remaining emergency replacement has `Cause == break_failure` and
`PreCondition == 1.00`: floor pipes that eventually broke, which is the intended semantics.

### 2.2 Age-based wear-out multiplier (the approved design)

Hazard × `1 + (Mmax − 1)·(1 − exp(−k·max(0, r − 1)))`, with `r = age / base_life[material]`,
`k = 2`. Calibrated on a **10-year untreated window** to 0.287 breaks/mile-year, then swept
over 100 years.

| Mmax | Scale | 10-yr rate | 100-yr rate | $0 | $200k | $500k | $1M | $2M | $5M | Best | Emerg | CIP |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 601 | 0.316 | 0.801 | 108.2 | 108.5 | 84.5 | 72.4 | 65.2 | **65.0** | $5.0M | 6 | 387 |
| 5 | 2,444 | 0.279 | 0.837 | 75.0 | 74.1 | **54.0** | 56.6 | 54.7 | 55.5 | $0.5M | 23 | 337 |
| 10 | 4,576 | 0.250 | 0.871 | 75.3 | 71.2 | 51.7 | **49.3** | 53.0 | 51.6 | $1.0M | 9 | 377 |
| 20 | 9,921 | 0.238 | 0.781 | 70.0 | 62.8 | **48.3** | 50.2 | 50.6 | 51.4 | $0.5M | 22 | 335 |

Three results drive the design:

1. **Wear-out is load-bearing.** With the floor rule removed but no wear-out (`Mmax = 1`),
   cost falls monotonically to $5M — the symptom returns. Add wear-out and the optimum
   turns interior at every strength tested. Physically: wear-out concentrates risk in old
   pipes that CIP can target by risk-per-dollar; without it risk is spread evenly and
   buying everything always pays.
2. **The surface is flat above the optimum** (~$2–5M across $0.5M–$5M). The budget optimum
   will be noise-sensitive; the Pareto front, not the single selected point, is the result
   to read.
3. **Re-anchoring calibration to a 10-year window changes `HAZARD_LENGTH_SCALE` materially**
   (1,500 → ~600 at Mmax = 1, ~4,600 at Mmax = 10), because today's inventory is far less
   broken than a 100-year do-nothing network. The 100-year untreated run stays finite and
   sits above the 10-year rate, as an ageing network should.

### 2.3 Condition-coupled multiplier (revision 1 design — superseded)

Retained because it independently confirmed that removing the floor rule restores an
interior optimum under any monotone end-of-life hazard coupling. Not the design going
forward: it ties mortality to each pipe's sampled TTF clock rather than to age, and it made
the 100-year do-nothing calibration target indefensible (required scales of 18,000–78,000).

---

## 3. Design

### A. Remove forced replacement at the condition floor

Emergency replacement becomes a consequence of exactly one thing: a segment reaching
`SEGMENT_BREAK_THRESHOLD` breaks (the confirmed failure rule). Reaching the condition floor
has no direct cost. A floor pipe remains in service, remains CIP-eligible, and keeps
breaking under the hazard model until CIP renews it or a segment fails.

Both sites change together. Site 1 is deleted. Site 2 keeps only `sim_result["failed"]`.
`CAUSE_DEGRADATION` and `CAUSE_BREAK_DAMAGE` are removed — the first can no longer occur and
the second was already impossible (`BREAK_DAMAGE_CONDITION_FLOOR = 1.05`). `EMERGENCY_CAUSES`
collapses to `("break_failure",)`. The `Cause` column stays in the action plan.

The CIP ranking already handles floor pipes: `current_ttf` floors at 0.1, so a floor pipe
scores ~366× higher than an otherwise identical pipe at condition 3. No ranking change.

### B. Age-based wear-out multiplier — the bathtub's wear-out arm

Add a multiplier to `Pipe.calculate_hazard` driven by age relative to the material's
average lifespan:

```python
# leyp_config.py
WEAROUT_ONSET_LIFE_RATIO = 1.0      # wear-out begins at this multiple of average life
WEAROUT_MAX_MULTIPLIER   = 10.0     # hazard multiplier the wear-out arm saturates toward
WEAROUT_STEEPNESS        = 2.0      # per unit of life ratio beyond onset

# leyp_core.py, Pipe.calculate_hazard
age  = max(self.initial_age + sim_year_idx, 0.0)
life = STANDARD_LIFE.get(self.material, STANDARD_LIFE["Default"])["base_life"]
excess = max(0.0, age / life - WEAROUT_ONSET_LIFE_RATIO)
wearout = 1.0 + (WEAROUT_MAX_MULTIPLIER - 1.0) * (1.0 - math.exp(-WEAROUT_STEEPNESS * excess))
return h0 * cov_factor * leyp_factor * wearout
```

Properties, each of which becomes a test:

- **Exactly 1× up to the onset**, so useful-life hazard is unchanged and `Mmax = 1`
  reproduces today's hazard bit-for-bit.
- **Monotone non-decreasing in age**, and **bounded** at `WEAROUT_MAX_MULTIPLIER` —
  it cannot diverge, which is the Phase 4 lesson.
- **Resets on replacement.** `initial_age = −year` after renewal, so a new pipe's `age`
  starts at 0 and it re-enters useful life; a new HDPE main (base life 150) never reaches
  onset within the horizon.
- **Fires immediately on the inherited backlog.** AC at 76 (life 60, r = 1.27), GI at 76
  (life 50, r = 1.52), CI at 96 (life 75, r = 1.28) all start past onset; DI at 96 (life 100,
  r = 0.96) is just short of it. That is intended: these are the pipes that should be failing.

Chosen form: a saturating exponential rather than an unbounded power or exponential in age,
so the ceiling is explicit and the three parameters are each interpretable (where it
starts, how high it goes, how fast). `Mmax = 10, k = 2, onset = 1.0` is proposed; §2.2 shows
the interior optimum is robust from 5 to 20. These are modelling judgements for you to
confirm (§7).

**Two clocks — a consequence you should know about.** Condition decays on each pipe's
sampled lognormal TTF, drawn around `DEGRADATION_PARAMS.ttf_mean`; wear-out runs on
`STANDARD_LIFE.base_life`. The config carries both as "average life" and they disagree by
~25% (DI 75 vs 100, AC 45 vs 60, CI 60 vs 75, PVC 90 vs 120). So most pipes reach the
condition floor *before* wear-out onset, and a pipe can sit at the floor with a 1× multiplier.
That is coherent — condition is the planning signal, age is the mortality clock — but it
should be a choice. Options: (a) accept, and make it visible via §D; (b) set onset relative
to `ttf_mean`; (c) align the two tables. Decision in §7; §D makes the gap measurable either
way.

**Infant mortality — optional, off by default.** The bathtub's first arm would add an
elevated multiplier for the first few years after installation, decaying to 1×. It applies
to every CIP renewal and so directly reduces the value of planned work. Proposed as a
separate, independently switchable term (`INFANT_MORTALITY_MULTIPLIER = 1.0` disables it),
not enabled until you decide it should be.

### C. Re-anchor the calibration window

Replace the 100-year do-nothing calibration target with a **short-window untreated rate**:
run the inventory as it stands with no intervention for `CALIBRATION_WINDOW_YEARS` (proposed
10) and match that to the target band. This measures the hazard model on the network as it
exists today — the thing a break-rate statistic describes — rather than on a network in
which most pipes have spent decades past onset at the saturated multiplier.

The 100-year `no_intervention` run becomes a **stability gate only**: finite, and above the
10-year rate. Its absolute level is no longer a target.

Re-derive `HAZARD_LENGTH_SCALE` against the window. Expect a value of roughly 600–10,000
depending on `Mmax` (§2.2), not the current 1,500. The band stays 0.15–0.40 until local
data replaces it (`OPEN_ITEMS.md` DAT-1).

### D. Make floor-dwell visible

With no forced replacement, pipes can sit at the floor. Add to `simulation_diagnostics.csv`
a per-year `Pipes_At_Floor` and `Floor_Pipes_Below_Onset` (at the floor but not yet in
wear-out — the two-clock gap), and to `cost_summary.csv` `Floor_Pipe_Years`,
`Max_Pipes_At_Floor`, and `Mean_Life_Ratio_At_Emergency` (confirms that the pipes which
fail are the old ones). A plan that leaves many pipes at the floor for many years is a plan
with a visible, quantified exposure rather than a hidden one.

---

## 4. Implementation steps

Land A and B **together**. A alone produces a model in which worn-out pipes are nearly
immortal and the symptom returns under the re-anchored calibration (§2.2, `Mmax = 1`);
B alone changes almost nothing while the floor rule still fires.

| # | File | Change |
|---|---|---|
| 1 | `leyp_runner.py` | Delete step 3's floor sweep. In step 4 condition the emergency call on `sim_result["failed"]` only. Renumber loop comments. |
| 2 | `leyp_runner.py` | Remove `CAUSE_DEGRADATION`, `CAUSE_BREAK_DAMAGE`; `EMERGENCY_CAUSES = ("break_failure",)`. Drop per-cause diagnostic columns that can no longer be non-zero. |
| 3 | `leyp_runner.py` | Add `Pipes_At_Floor`, `Floor_Pipes_Below_Onset` per year; `Floor_Pipe_Years`, `Max_Pipes_At_Floor`, `Mean_Life_Ratio_At_Emergency` to the summary. Record each emergency's life ratio in the action row (`Life_Ratio`). |
| 4 | `leyp_config.py` | Add `WEAROUT_ONSET_LIFE_RATIO`, `WEAROUT_MAX_MULTIPLIER`, `WEAROUT_STEEPNESS`, `INFANT_MORTALITY_MULTIPLIER = 1.0`, `INFANT_MORTALITY_YEARS`, `CALIBRATION_WINDOW_YEARS = 10`, each with a comment stating why it exists. |
| 5 | `leyp_core.py` | Add `Pipe.wearout_multiplier(sim_year_idx)` and (optional) `infant_multiplier(sim_year_idx)` as separate methods, both applied in `calculate_hazard`. Separate methods so each is unit-testable on its own. |
| 6 | `leyp_runner.py` | `simulate()` gains `horizon_years: int | None` (default `SIMULATION_YEARS`) so the calibration window runs without touching the global. |
| 7 | `leyp_config.py` | Re-derive `HAZARD_LENGTH_SCALE` against the 10-year untreated window; record the derivation and the `Mmax` it assumes in the comment. |
| 8 | `optimizer_config.yaml` | Restore `genes.budget.max` to `2000000`. Raise `simulation.n_replicates` to 8 given the flat surface. |
| 9 | `water_validation.py` | No change required; the reactive-only baseline remains correct. |
| 10 | `README.md` | Annual loop order; failure rule ("the only route to emergency replacement is the segment rule"); hazard formula with the wear-out term and the bathtub framing; calibration window; two-clock note; new columns. |
| 11 | `OPEN_ITEMS.md` | DEC-1 superseded (done). MOD-2 will shift: breaks are now the sole failure route. |

---

## 5. Tests

### Rewrite (they pin the behaviour being removed)

| Test | Why it breaks | Replacement |
|---|---|---|
| `test_calibration.py::test_unfunded_age_out_falls_as_budget_rises` | Reads `Emergency_degradation`, gone | `test_emergency_replacements_fall_as_budget_rises` on `Emergency_Count` |
| `test_calibration.py::test_cip_decides_on_last_assessment_condition` | Expects an aged-out pipe to be emergency-replaced | Drive the pipe to a segment failure; assert CIP did not intercept it that year |
| `test_calibration.py::test_untreated_break_rate_is_defensible` | Asserts the 100-yr rate is in band; it will now sit near 0.8 | Split: `test_calibration_window_rate_is_defensible` (10-yr, in band) and `test_long_untreated_run_stays_finite` (100-yr < 5.0, and > the 10-yr rate) |
| `test_calibration.py::test_hazard_scale_is_the_calibrated_value` | Pins 1500.0 | Pin the new derived value |
| `test_runner.py::test_cip_captures_age_out_before_emergency` | Asserts no `degradation` emergencies — now vacuous | `test_no_emergency_replacement_without_a_segment_failure` |
| `test_runner.py` diagnostics tests | Iterate `EMERGENCY_CAUSES` | Follow the collapsed tuple |

### Add — wear-out multiplier (`test_leyp_core.py`)

- `test_wearout_is_unity_before_onset` — multiplier == 1.0 for every age up to onset × life,
  every material.
- `test_wearout_is_monotone_in_age` — non-decreasing across a dense age grid, every material.
- `test_wearout_is_bounded` — never exceeds `WEAROUT_MAX_MULTIPLIER`; approaches it at
  r = 5.
- `test_wearout_onset_is_at_average_life` — multiplier at r = 1.0 is 1.0 and at r = 1.01 is
  > 1.0, for the configured onset of 1.0.
- `test_wearout_resets_on_replacement` — after `execute_replacement`, multiplier is 1.0 and
  stays 1.0 for `base_life[HDPE]` years.
- `test_max_multiplier_of_one_disables_wearout` — with `WEAROUT_MAX_MULTIPLIER = 1.0`,
  `calculate_hazard` equals the pre-change formula exactly, every material and age.
- `test_inherited_backlog_starts_in_wearout` — AC/GI/CI at their inventory ages have
  multiplier > 1 at t = 0; DI at 96 does not.
- `test_hazard_still_scales_with_leyp_feedback` — the wear-out term composes
  multiplicatively with the existing feedback term.

### Add — floor semantics (`test_runner.py`)

- `test_floor_pipe_is_not_replaced_without_a_break` — force a pipe to the floor with zero
  breaks and zero budget; after one year it is still at the floor, unreplaced, no emergency
  row.
- `test_every_emergency_replacement_follows_a_segment_failure` — on the real inventory,
  100% of emergency rows have `Cause == break_failure`, and the pipe's segments show a
  segment at the threshold at the time of the row.
- `test_floor_dwell_is_reported` — `Pipes_At_Floor`, `Floor_Pipes_Below_Onset` present and
  reconcile with the summary.
- `test_emergencies_fall_on_old_pipes` — `Mean_Life_Ratio_At_Emergency` > 1.0 on the real
  inventory at the reference budget.

### Add — calibration and symptom (`test_calibration.py`, `test_optimizer.py`)

- `test_calibration_window_rate_is_defensible` — 10-yr untreated rate within 0.15–0.40.
- `test_long_untreated_run_stays_finite` — 100-yr untreated rate < 5.0 and ≥ the 10-yr rate.
- `test_budget_optimum_is_interior` — coarse sweep at the default `Mmax`: minimum-cost
  budget strictly inside the gene range.
- `test_wearout_is_load_bearing_for_interior_optimum` — with `Mmax = 1` the same sweep
  has its minimum at the upper bound (documents §2.2 so the term cannot be silently
  removed later).

---

## 6. Verification

See **`VERIFICATION_PLAN.md`** — gates V0 through V8, each with the commands to run, the
pass criterion, and the evidence file it produces.

---

## 7. What to expect, and what you need to decide

**The answer will change a lot.** Expect the selected budget to fall from $12.1M/yr to
roughly $0.5–1M, investment share to fall, and emergency replacements to number in the
teens over the horizon — now genuinely failure-driven and concentrated on pipes past their
average life.

**The budget optimum will be poorly determined.** The cost surface is flat above the
optimum. Raise `n_replicates`, and read the Pareto front rather than the single point.

**Pipes will sit at the floor.** Realistic, but it must be visible (§D). If floor-dwell is
unacceptable, the lever is `WEAROUT_MAX_MULTIPLIER` / `WEAROUT_STEEPNESS`, not a return to
forced replacement.

Decisions needed before implementation:

1. **Wear-out parameters.** Proposed `Mmax = 10, k = 2, onset = 1.0`. Results are robust
   from 5 to 20 (§2.2).
2. **Which "average lifespan" drives onset.** `STANDARD_LIFE.base_life` (proposed, matches
   the initialisation curve) or `DEGRADATION_PARAMS.ttf_mean` (matches the condition clock).
   Or align the two tables. See the two-clock note in §3B.
3. **Infant mortality.** Off by default. Enabling it lowers the value of every renewal;
   if enabled, gate V8 applies.
4. **Calibration window.** Proposed 10 years untreated. Alternative: a reactive-only
   baseline, closer to a poorly-funded real utility but mixing policy into calibration.
5. **Restore the $2M ceiling.** Recommended once the change lands; $20M was a workaround.

---

## 8. Relationship to `OPEN_ITEMS.md`

- **DEC-1** — superseded; marked.
- **MOD-2** (repair stream immaterial) — will shift. With breaks the only failure route,
  `EMERGENCY_REPAIR_COST_PER_BREAK` and the break rate carry the risk objective.
- **DEC-3** (emergency delivers an identical asset) — unchanged and more consequential:
  emergency replacement is now rarer and genuinely failure-driven.
- **DAT-1** (calibrate to local history) — the 10-year window is where local data plugs in.
- **New** — the two-clock inconsistency between `STANDARD_LIFE` and `DEGRADATION_PARAMS`
  belongs on the list regardless of which option in §7.2 is chosen.
