# End-of-Life Condition Modelling — Review and Implementation Plan

**Symptom reported.** The optimizer always drives the annual budget to the gene ceiling.
Raising the ceiling from $2M to $20M moved the selected budget to **$12.1M/yr** for a
21.7-mile, 388-pipe network — not pinned any more, but still "spend whatever it takes so
that no pipe ever reaches end of life."

**Hypothesis reported.** A pipe reaching its minimum condition is automatically given an
emergency replacement in that year, even though it has not broken. Emergency replacement
should be a consequence of a break event, not of end-of-life condition modelling.

**Finding.** The hypothesis is correct, the behaviour exists in **two** places in the code
rather than one, and there is a structural reason it was there: the hazard model never
reads condition, so without a forced rule a pipe at condition 1.0 would break no faster
than a healthy pipe of the same age. The rule was a crutch standing in for a missing
coupling. Removing it fixes the budget symptom; coupling hazard to condition is what makes
the fix physically meaningful.

---

## 1. Where the behaviour lives

### Site 1 — explicit sweep, `leyp_runner.py`, annual loop step 3

```python
# 3. EMERGENCY REPLACEMENT — pipes the plan did not reach in time
for pipe in network:
    if pipe.current_condition <= 1.001:
        _emergency_replace(pipe, year, CAUSE_DEGRADATION)
```

Every pipe whose condition has decayed to the floor is charged a $5,000/ft emergency
replacement that year. No break has occurred. The cause is even labelled `degradation`.

### Site 2 — the same trigger inside the break step, `leyp_runner.py`, step 4

```python
if not no_intervention and (
    sim_result["failed"] or pipe.current_condition <= 1.001
):
    cause = CAUSE_BREAK_FAILURE if sim_result["failed"] else CAUSE_BREAK_DAMAGE
    _emergency_replace(pipe, year, cause)
```

The `or pipe.current_condition <= 1.001` clause is a second copy of the same rule. It is
invisible today only because site 1 has already replaced those pipes. **Removing site 1
alone does nothing**: the pipes are caught one step later and mislabelled `break_damage`.
This was verified — with only site 1 removed, emergency counts were unchanged (228 → 229).

### Why the rule was needed — `leyp_core.py`, `Pipe.calculate_hazard`

```python
h0 = (beta/eta) * (t/eta)**(beta-1)             # age only
cov_factor = mat_mult * exp(COEFF_DIAMETER * d)  # material, diameter
leyp_factor = 1 + ALPHA * min(n_breaks, CAP)     # break history
return h0 * cov_factor * leyp_factor             # condition: absent
```

`current_condition` does not appear. A pipe at the floor and a pipe at condition 4.0 of the
same age and material have identical break hazard. Expected time for a floor pipe to trip
the segment rule (3 breaks in one of 4 segments, feedback ignored):

| Material, age | Hazard /yr | Years to trip the segment rule |
|---|---:|---:|
| GI, 80 | 0.242 | ~250 |
| AC, 90 | 0.126 | ~475 |
| DI, 96 | 0.022 | ~2,700 |
| PVC, 96 | 0.007 | ~8,500 |

So "end of life" currently has no physical consequence in the hazard model at all. The
forced replacement was what gave it one.

### Why it pins the budget

Each pipe that reaches the floor without being renewed costs $5,000/ft immediately. The
only way to avoid that cost is to renew it first, so the optimizer buys enough budget to
renew everything before it ages out — and the cheapest way to do that is always "more".
Your latest run shows the end state: **0 degradation emergencies, 428 CIP, 4 emergency**,
at $12.1M/yr.

---

## 2. Evidence from runtime experiments

All runs: Louisa inventory, trigger 1.5, seed 20260830, 3 replicates, PV totals.

### Removing both sites (no hazard change)

| Budget | Current model | | | No floor rule | | | |
|---:|---:|---:|---:|---:|---:|---:|---:|
| | Total $M | CIP | Emerg | Total $M | CIP | Emerg | Breaks |
| $0 | 213.3 | 0 | 382 | **51.1** | 0 | 56 | 267 |
| $200k | 181.7 | 258 | 132 | 52.7 | 260 | 38 | 205 |
| $500k | 128.7 | 328 | 69 | **44.8** | 336 | 21 | 121 |
| $1M | 83.1 | 374 | 21 | 46.6 | 376 | 8 | 80 |
| $2M | 56.2 | 384 | 4 | 44.8 | 384 | 2 | 54 |
| $5M | 47.8 | 387 | 1 | 47.8 | 387 | 1 | 48 |

Current model: cost falls monotonically to the cap. No floor rule: **interior minimum at
~$500k.** The symptom is resolved by this change alone.

Two things to notice. Every remaining emergency replacement now has
`Cause == break_failure` and `PreCondition == 1.00` — they are floor pipes that eventually
broke, which is the intended semantics. And the cost surface is **flat**: ~$9M across two
orders of magnitude of budget. The budget optimum will be poorly determined and
noise-sensitive (replicate sd at 3 draws is ~$3M).

### Adding a condition-coupled hazard

Hazard multiplied by `exp(ln(M) · (6 − condition)/5)`: 1× at condition 6, M× at the floor.
`HAZARD_LENGTH_SCALE` re-derived each time to hold the untreated rate near 0.287.

| M | Scale | Untreated | $0 | $200k | $500k | $1M | $2M | $5M | Best | Emerg@best |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1,481 | 0.286 | 53.9 | 54.7 | **44.9** | 45.4 | 47.6 | 47.8 | $500k | 21 |
| 5 | 8,704 | 0.226 | 44.0 | 48.2 | **38.6** | 43.5 | 43.1 | 47.4 | $500k | 15 |
| 10 | 18,301 | 0.211 | 39.9 | 46.5 | **36.5** | 39.3 | 42.5 | 46.6 | $500k | 14 |
| 20 | 37,513 | 0.206 | 41.3 | 43.7 | **35.5** | 40.0 | 44.8 | 46.3 | $500k | 13 |
| 40 | 77,771 | 0.195 | 34.8 | 41.7 | **33.3** | 36.4 | 42.8 | 45.5 | $500k | 12 |

The interior optimum survives at every M. The coupling changes *which* pipes break (floor
pipes, as it should) more than it changes the aggregate economics, because the
re-calibration holds total untreated breaks roughly constant.

**The twist.** Look at the Scale column. To hold the 100-year do-nothing rate at 0.287
under coupling, the scale has to rise to 18,000–78,000 — because in a 100-year run with no
renewal, most of the network dwells at the floor for decades and generates most of the
breaks. That state describes no real utility, and no literature break rate was measured on
one. Under condition coupling, **the 100-year untreated run stops being a defensible
calibration target** (it remains a good stability test). Section 3C addresses this.

---

## 3. Design

### A. Remove forced replacement at the condition floor

Emergency replacement becomes a consequence of exactly one thing: a segment reaching
`SEGMENT_BREAK_THRESHOLD` breaks (the confirmed failure rule). Reaching the condition floor
has no direct cost. A floor pipe remains in service, remains CIP-eligible, and keeps
breaking under the hazard model until either CIP renews it or a segment fails.

Both sites are changed together. Site 1 is deleted. Site 2 keeps only `sim_result["failed"]`.
`CAUSE_DEGRADATION` and `CAUSE_BREAK_DAMAGE` are removed — the first can no longer occur,
and the second was already impossible (`BREAK_DAMAGE_CONDITION_FLOOR = 1.05` keeps break
damage above the floor). `EMERGENCY_CAUSES` collapses to `("break_failure",)`. The `Cause`
column is retained in the action plan for log stability.

The CIP ranking already handles floor pipes: `current_ttf` floors at 0.1, so a floor pipe
scores **~366× higher** than an otherwise identical pipe at condition 3. No ranking change.

### B. Couple hazard to condition

Add a condition multiplier to `Pipe.calculate_hazard`, so that "end of life" means
"very likely to break", which is what the words mean:

```python
# leyp_config.py
CONDITION_HAZARD_MULTIPLIER_AT_FLOOR = 10.0   # 1x at condition 6, this at condition 1

# leyp_core.py, calculate_hazard
cond_factor = math.exp(
    math.log(CONDITION_HAZARD_MULTIPLIER_AT_FLOOR) * (6.0 - self.current_condition) / 5.0
)
return h0 * cov_factor * leyp_factor * cond_factor
```

Exponential rather than linear so the end-of-life zone is sharp, which matches how
deterioration behaves; a multiplier of 1.0 disables the coupling exactly. This introduces a
second feedback loop — break damage lowers condition, which raises hazard, which produces
breaks — but it is bounded: condition floors at 1.0, so the multiplier never exceeds M, and
the LEYP loop is already capped.

**M = 10 is proposed as the default** (a worn-out main breaks an order of magnitude more
often than a new one of the same age), with the sweep above showing results are not
sensitive to the exact value between 5 and 40. This is a modelling judgement for you to
confirm; see §7.

### C. Re-anchor the calibration window

Replace the 100-year do-nothing calibration target with a **short-window untreated rate**:
run the inventory as it stands with no intervention for `CALIBRATION_WINDOW_YEARS`
(proposed 10) and match that to the target band. This measures the hazard model on the
network as it exists today — the thing a break-rate statistic actually describes — rather
than on a network where 60% of pipes have sat at the floor for half a century.

Keep the 100-year `no_intervention` run as a **stability gate only**: it must stay finite
(the Phase 4 divergence test), but its absolute level is no longer a target.

Then re-derive `HAZARD_LENGTH_SCALE` against the window. The band remains 0.15–0.40
breaks/mile-year until local data replaces it (`OPEN_ITEMS.md` DAT-1).

### D. Make floor-dwell visible

With no forced replacement, pipes can sit at the floor. That must be observable, not hidden.
Add to `simulation_diagnostics.csv` a per-year `Pipes_At_Floor` count, and to
`cost_summary.csv` `Floor_Pipe_Years` (sum over the horizon) and `Max_Pipes_At_Floor`.
A plan that leaves many pipes at the floor for many years is a plan with a visible,
quantified risk exposure rather than a hidden one.

---

## 4. Implementation steps

Land A and B **together** in one change. A alone produces an intermediate model where worn-out
pipes are nearly immortal; B alone changes almost nothing while the floor rule still fires.

| # | File | Change |
|---|---|---|
| 1 | `leyp_runner.py` | Delete step 3's floor sweep. In step 4, condition the emergency call on `sim_result["failed"]` only. Renumber the loop comments. |
| 2 | `leyp_runner.py` | Remove `CAUSE_DEGRADATION`, `CAUSE_BREAK_DAMAGE`; `EMERGENCY_CAUSES = ("break_failure",)`. Drop the per-cause diagnostic columns that can no longer be non-zero; keep `Emergency_break_failure` or rename to `Emergency_Count`. |
| 3 | `leyp_runner.py` | Add `Pipes_At_Floor` per year (count of `current_condition <= 1.001` after step 2), `Floor_Pipe_Years` and `Max_Pipes_At_Floor` to the summary. |
| 4 | `leyp_config.py` | Add `CONDITION_HAZARD_MULTIPLIER_AT_FLOOR = 10.0` and `CALIBRATION_WINDOW_YEARS = 10`, each with a comment explaining why it exists. |
| 5 | `leyp_core.py` | Add the condition factor to `calculate_hazard` as in §3B. |
| 6 | `leyp_runner.py` | `simulate()` gains `horizon_years: int | None` (default `SIMULATION_YEARS`) so the calibration window can be run without touching the global. |
| 7 | `leyp_config.py` | Re-derive `HAZARD_LENGTH_SCALE` against the 10-year untreated window and record the derivation in the comment. Expect a value in the low thousands, not 18,000+. |
| 8 | `optimizer_config.yaml` | Restore `genes.budget.max` to `2000000`. The $20M ceiling was a workaround for the symptom this change removes. Consider `n_replicates: 8` given the flat cost surface. |
| 9 | `README.md` | Annual loop order; failure rule section ("the only route to emergency replacement is the segment rule"); hazard formula with the condition factor; calibration section with the window; new diagnostics columns. |
| 10 | `OPEN_ITEMS.md` | DEC-1 is superseded (root cause found); MOD-2 will shift (breaks become the sole failure driver, so the repair stream and the break rate carry the risk objective). |

---

## 5. Tests

### Rewrite (they pin the behaviour being removed)

| Test | Why it breaks | Replacement |
|---|---|---|
| `test_calibration.py::test_unfunded_age_out_falls_as_budget_rises` | Reads `Emergency_degradation`, which no longer exists | `test_emergency_replacements_fall_as_budget_rises` on `Emergency_Count` |
| `test_calibration.py::test_cip_decides_on_last_assessment_condition` | Expects an aged-out pipe to be emergency-replaced | Drive the pipe to a segment failure instead; assert CIP did not intercept it in the same year |
| `test_runner.py::test_cip_captures_age_out_before_emergency` | Asserts no `degradation` emergencies — now vacuous | `test_no_emergency_replacement_without_a_segment_failure` |
| `test_runner.py` diagnostics tests | Iterate `EMERGENCY_CAUSES` | Follow the collapsed tuple |

### Add

- `test_floor_pipe_is_not_replaced_without_a_break` — force a pipe to the floor with zero
  breaks and zero budget; after a year it is still at the floor, unreplaced, no emergency row.
- `test_every_emergency_replacement_follows_a_segment_failure` — on the real inventory, 100%
  of emergency rows have `Cause == break_failure`.
- `test_hazard_rises_as_condition_falls` — same pipe, condition 6 → 1, hazard increases.
- `test_hazard_multiplier_is_bounded_at_floor` — hazard at condition 1.0 equals hazard at
  condition 6.0 × M, and never exceeds it.
- `test_multiplier_of_one_disables_coupling` — M = 1.0 reproduces the uncoupled hazard.
- `test_calibration_window_rate_is_defensible` — 10-year untreated rate within 0.15–0.40.
- `test_long_untreated_run_stays_finite` — 100-year untreated rate < 5.0 (keeps the Phase 4
  divergence guard; replaces the absolute-level assertion).
- `test_floor_dwell_is_reported` — diagnostics carry `Pipes_At_Floor`; summary reconciles.
- `test_budget_optimum_is_interior` — on a coarse sweep, the minimum-cost budget is strictly
  inside the gene range, not at either bound.

---

## 6. Verification gates

| Gate | Criterion |
|---|---|
| G1 — semantics | Every emergency replacement in the action plan has `Cause == break_failure`. Zero pipes are replaced in the year they reach the floor without a segment failure. |
| G2 — physics | Hazard is monotone decreasing in condition; multiplier bounded at M; M = 1 reproduces today's hazard exactly. |
| G3 — calibration | 10-year untreated rate within band; 100-year untreated rate finite. |
| G4 — the symptom | With the ceiling restored to $2M, the optimizer's selected budget is below 90% of the ceiling, and a budget sweep shows a strict interior minimum. |
| G5 — plan health | CIP still outnumbers emergency; investment share of total between 20% and 80%; cost-effectiveness panel shows a break-even point rather than a flat line. |
| G6 — visibility | `Floor_Pipe_Years` reported; `Max_Pipes_At_Floor` reported. |
| G7 — suite | All tests green, with the four rewritten tests and nine new ones. |

---

## 7. What to expect, and what you need to decide

**The answer will change a lot.** Expect the selected budget to fall from $12.1M/yr to
somewhere in the $0.5–1M range, investment share to fall, and emergency replacements to
number in the teens over the horizon. That is the model saying something true it could not
say before: once worn-out pipes are no longer billed $5,000/ft for existing, deferral is
genuinely cheaper than it looked, and the capital programme's value is in avoiding the
breaks that worn-out pipes actually produce.

**The budget optimum will be poorly determined.** The cost surface is flat (~$9M across a
100× budget range). Either raise `n_replicates` for the optimizer run, or accept that the
Pareto front, not the single selected point, is the result to read.

**Pipes will sit at the floor.** This is realistic — utilities do operate worn-out mains —
but it must be visible, which is what §3D is for. If the floor-dwell numbers look
unacceptable, the lever is M (§3B), not a return to forced replacement.

Decisions needed before implementation:

1. **M**, the hazard multiplier at the floor. Proposed 10. Results are insensitive between
   5 and 40; the sweep above gives you the trade-off.
2. **Calibration window.** Proposed 10 years untreated. The alternative is a reactive-only
   baseline (zero budget, failures replaced), which is closer to a poorly-funded real utility
   but mixes policy into the calibration.
3. **Restore the $2M ceiling.** Recommended once the change lands; the $20M was a workaround.

---

## 8. Relationship to `OPEN_ITEMS.md`

- **DEC-1** (optimum pinned at the ceiling) — superseded. Root cause identified here; the
  resolution is this plan, not a higher ceiling.
- **MOD-2** (repair stream immaterial) — will shift. With breaks the only route to failure,
  `EMERGENCY_REPAIR_COST_PER_BREAK` and the break rate carry the risk objective; re-read
  that item after this lands.
- **DEC-3** (emergency delivers an identical asset) — unchanged, and more consequential now
  that emergency replacement is rarer and genuinely failure-driven.
- **DAT-1** (calibrate to local history) — the calibration window in §3C is where local
  break data would plug in.
