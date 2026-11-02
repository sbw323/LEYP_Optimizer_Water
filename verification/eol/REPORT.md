# End-of-Life Modelling — Verification Report

Companion to `VERIFICATION_PLAN.md`. Implementation of `EOL_MODELLING_PLAN.md` revision 2
with the approved decisions:

| Decision | Choice |
|---|---|
| 1 — wear-out parameters | `WEAROUT_MAX_MULTIPLIER = 10`, `WEAROUT_STEEPNESS = 2`, `WEAROUT_ONSET_LIFE_RATIO = 1.0` (proposed defaults) |
| 2 — which lifespan drives onset | `STANDARD_LIFE.base_life`; tables **not** aligned; the two-clock gap is reported, not removed |
| 3 — infant mortality | Not modelled; recorded as `OPEN_ITEMS.md` DEC-6 |
| 4 — calibration window | 10-year untreated window — **confirmed 2026-11-02**. Derivation tooling deferred to `OPEN_ITEMS.md` ENG-4 |
| 5 — budget ceiling | Restored to $2,000,000 |

All measurements: Louisa inventory, `seed = 20260830`. Run on 2026-11-02.

---

## V0 — Baseline (unchanged code) — PASS

Captured before any edit.

| Budget | PV total $M | |
|---:|---:|---|
| $0 | 213.3 | |
| $200k | 181.7 | |
| $500k | 128.7 | |
| $1M | 83.1 | |
| $2M | 56.2 | |
| $5M | **47.8** | ← minimum at the top of the range: the symptom |

- Untreated 100-yr rate: 0.283 breaks/mile-year.
- At $500k, trigger 1.5: **67 of 73** emergency replacements were `degradation`-triggered — the behaviour being removed.
- Suite: 140 passed, exit 0.

Evidence: `verification/eol/v0/baseline.json`, `v0/cost_summary.csv`, `v0/nsga2_results.csv`, `v0/suite.txt`.

---

## V1 — Floor-triggered replacement removed at both sites — PASS

| Check | Result |
|---|---|
| V1.1 `grep "current_condition <= 1.001" leyp_runner.py` | One match, line 269 — the `Pipes_At_Floor` diagnostic counter (allowed). No match in any replacement path. |
| V1.1 `CAUSE_DEGRADATION` / `CAUSE_BREAK_DAMAGE` anywhere | None. `EMERGENCY_CAUSES == ("break_failure",)`. |
| V1.2 `test_floor_pipe_is_not_replaced_without_a_break` | Green. A pipe at the floor with hazard silenced sits there for 100 years, unreplaced, zero emergency rows. |
| V1.3 Integration, $500k, trigger 1.5, 3 reps | 22 emergency rows; `Cause` values `{'break_failure': 22}`. |
| V1.3 `test_no_emergency_replacement_without_a_segment_failure` | Green. Every emergency row has a same-year `Break_Event` on the same pipe. |
| V1.4 Old test names collected | 0 (`test_unfunded_age_out_falls_as_budget_rises`, `test_cip_captures_age_out_before_emergency` replaced). |

Evidence: `verification/eol/v1/`, `gates_v1-v6.txt`.

---

## V2 — Age-based wear-out multiplier — PASS

| Check | Result |
|---|---|
| V2.1 `pytest -k wearout` (8 tests incl. parametrised over all 10 materials) | Green: unity before onset, monotone in age, bounded, onset at average life, curve matches specification, resets on replacement, inherited backlog starts in wear-out. |
| V2.2 `test_max_multiplier_of_one_disables_wearout` | Green. With `M = 1.0`, `calculate_hazard` equals the pre-change formula to 1e-12 for every material at ages 0/50/100/200. |
| V2.3 Composition with LEYP term and cap | Green (`test_hazard_still_scales_with_leyp_feedback`, `test_leyp_feedback_scales_hazard`, `test_feedback_saturates_at_the_cap`). |
| V2.4 Curve | 0.50→1.00× · 0.90→1.00× · 1.00→1.00× · 1.10→2.63× · 1.25→4.54× · 1.50→6.69× · 2.00→8.78× · 3.00→9.84× · 5.00→10.00× |
| V2.5 `Mean_Life_Ratio_At_Emergency` ($500k) | **1.68** (> 1.0): the pipes that fail are past their average life. `test_emergencies_fall_on_old_pipes` green. |

Evidence: `verification/eol/v2_curve.txt`.

---

## V3 — Calibration re-anchored; model stable — PASS (with a flag)

Derivation (fixed-point on the 10-year window, target 0.287):

```
iter 0: window rate 0.926 -> scale 4,842
iter 1: window rate 0.251 -> scale 4,237
iter 2: window rate 0.291 -> scale 4,292
chosen: HAZARD_LENGTH_SCALE = 4,300
```

| Check | Result |
|---|---|
| V3.1 Window rate (8 reps) | **0.285** breaks/mile-year — in band 0.15–0.40. `test_calibration_window_rate_is_defensible` green. |
| V3.2 100-yr untreated | **0.926** — finite (< 5.0) and above the window. `test_long_untreated_run_stays_finite` green. |
| V3.3 Pin | `test_hazard_scale_is_the_calibrated_value == 4300.0` green; comment in `leyp_config.py` records window, target, measured rates, `Mmax`, date. |
| V3.4 Sensitivity (scale fixed at 4,300, 3 reps) | `Mmax = 5 → 0.112`, `10 → 0.262`, `20 → 0.603` |

**Flag (V3.4).** The window rate scales almost proportionally with `WEAROUT_MAX_MULTIPLIER`,
because the inherited backlog is already past onset and dominates today's breaks. The
break-rate target therefore pins only the *combination* of scale and ceiling, not each
separately. Recorded as `OPEN_ITEMS.md` MOD-4; separating them needs local break records
by pipe age (DAT-1).

Evidence: `verification/eol/v3_derivation.txt`.

---

## V4 — Floor-dwell and the two-clock gap visible — PASS

| Figure ($500k, trigger 1.5, 3 reps) | Value |
|---|---:|
| `Floor_Pipe_Years` | 2,054 |
| `Max_Pipes_At_Floor` | 33 |
| Share of floor pipe-years below wear-out onset | **16%** |

Columns present and reconciling (`test_floor_dwell_is_reported` green). The 16% is the
input to Decision 2: one floor pipe-year in six is a pipe the condition clock calls
worn-out but the age clock does not yet. Accepted as-is and recorded as DEC-7.

---

## V5 — The symptom is resolved — V5.1–V5.3, V5.5 PASS · **V5.4 FAIL on its literal criterion** (see analysis)

| Check | Result |
|---|---|
| V5.1 Ceiling | `genes.budget.max = 2000000` ✓; `n_replicates = 8`. |
| V5.2 Coarse sweep (5 reps) | $10k 77.7 · $200k 73.3 · $500k 52.4 · $1M 53.5 · **$1.5M 50.2** · $2M 50.3 — minimum strictly interior. `test_budget_optimum_is_interior` green at 8 reps on a sweep extended to $5M. |
| V5.3 Control | `test_wearout_is_load_bearing_for_interior_optimum` green: with `M = 1.0` the minimum returns to a bound. |
| V5.4 Full optimizer | Selected budget $1,820,560 = **91%** of ceiling → fails the < 90% criterion. No ceiling warning; 4 of 32 front solutions above 90%. See analysis below. |
| V5.5 vs V0 | V0 min-cost budget **$5,000,000** (top of range) → V5 **$1,500,000** (interior). |

**Honesty note on V5.2.** $1.5M (50.2) and $2M (50.3) differ by $0.1M against a replicate
sd of ~$2M. The surface is flat above ~$500k; the gate passes because the *lower* bound is
decisively rejected (77.7 vs 50.2) and the argmin is no longer forced to the ceiling — not
because a single sharp optimum exists. Read the Pareto front, not the point.

### V5.4 — full optimizer run — FAIL on criterion, with analysis

512 evaluations, 8 replicates each, $2M ceiling.

| Item | Result | Criterion |
|---|---|---|
| Selected budget (`min_total_cost`) | **$1,820,560 = 91% of ceiling**, trigger 1.27, PV total $44.6M | < 90% — **not met** |
| `[warning] … upper gene bound` in log | absent | absent ✓ |
| Front solutions above 90% of ceiling | 4 of 32 | < 8 ✓ |
| Budget range on front | $10,263 – $1,974,173; trigger 1.17 – 2.56 | — |
| `knee` would pick | $591,095, trigger 1.50, PV total $49.3M | — |

**Why it failed, measured rather than argued.** Two follow-up sweeps at 16 replicates × 3
independent seeds (`v5.4_plateau.txt`, `v5.4_above_ceiling.txt`):

1. *Is the near-ceiling pick genuinely cheaper than the knee pick?* Yes: paired gap
   −$3.2M, sd of gap $1.5M (~2σ). The optimizer is not being fooled by noise.
2. *Does the surface keep falling past the ceiling?* No. Minimum of means is at **$2.5M**
   at both triggers, then flat-to-rising to $5M:

| Budget | Trigger 1.27 | Trigger 1.50 |
|---:|---:|---:|
| $1.5M | 51.0 | 51.0 |
| $2.0M | 50.4 | 51.2 |
| $2.5M | **50.2** | **50.4** |
| $3.0M | 50.7 | 51.6 |
| $4.0M | 51.5 | 51.2 |
| $5.0M | 51.2 | 51.2 |

Every difference in that table is inside the replicate noise (sd $1–3M). The surface is a
very flat bowl whose bottom spans roughly **$1.5M–$5M**, centred near $2–2.5M. The $2M
ceiling happens to sit on the bottom of it.

**Reading.** The symptom was a *runaway*: forced $5,000/ft at the condition floor made
cost fall steeply to any ceiling (baseline: 213 → 48 across $0–$5M; cap raised to $20M →
$12.1M selected). That mechanism is gone — V5.3 proves the wear-out term is what removed
it, and the gradient at the ceiling is now break-even within noise. What remains is a
coincidence: the gate assumed the optimum would land well below the ceiling, and it lands
roughly *at* it. Raising the ceiling would move the pick to about $2.5M and stop, not
chase.

**Not done to make it pass:** the criterion was not relaxed, the ceiling was not raised in
the committed config, and the selection rule was not switched to `knee` (which would pass
trivially at $591k but is $3.2M dearer and is a decision about which plan to publish, not a
verification matter).

**Options for closing V5.4** (Decision 5 revisited):
- *Accept as-is* with this evidence: the model says this network's optimal programme is
  ~$2M/yr and the ceiling is a realistic funding cap that coincides with it.
- *Raise `genes.budget.max` to $3M* and re-run: expected selection ~$2.0–2.5M, interior,
  which would satisfy the criterion as written at near-identical cost.
- *Switch `selection` to `knee`*: publishes the $591k plan; cheaper to fund, $3.2M dearer
  in PV, interior by construction.

---

## V6 — Plan health and reproducibility — PASS

| Check | Result |
|---|---|
| V6.1 Health metrics on the regenerated plan | All five PASS: CIP **488** vs emergency **7**; investment share **0.70**; yr 1–2 emergencies **1**; max in any year **3**; mean life ratio at emergency **1.42**. Floor pipe-years 182 (max 4 at once; 56 below onset). Breaks 46 (0.021/mile-year). PV total $44.8M (inv $31.2M, risk $13.6M); nominal $125.0M; budget utilisation 0.49. |
| V6.2 Cost-effectiveness break-even exists | PASS: proactive spend clears break-even up to **80.8%** of network length; max ratio 30.15. Curve reach unchanged (99.2% pipes, 90.2% length); diagonal panels still below the line, as expected and documented. |
| V6.3 Reproducibility | Green (`same_seed`, `replicates`, `detailed_return` tests). |
| V6.4 Noise vs flat surface (8 reps) | replicate sd **$2.17M** vs sweep range **$27.5M** → **8%** (pass < 25%). |
| V6.5 Full suite | **173 passed, exit 0** (V0: 140). Net +33: 8 wear-out, 4 floor-semantics, 2 symptom, 2 calibration, rewrites of 4 pinned tests. |

---

## V7 — Documentation and backlog — PASS

| Check | Result |
|---|---|
| Stale wording in README (`3+ segments`, `CAUSE_DEGRADATION`, old `Cause` list, "pipes the plan did not reach") | None. |
| "only route to emergency replacement" in README | Present. |
| New concepts documented (`WEAROUT_*`, `CALIBRATION_WINDOW_YEARS`, bathtub, `Pipes_At_Floor`, two clocks, `Floor_Pipes_Below_Onset`) | 11 hits (need ≥ 6). |
| `OPEN_ITEMS.md` | DEC-1 superseded note; MOD-2 updated; DEC-6 (infant mortality, deferred), DEC-7 (two clocks, accepted), MOD-4 (calibration/ceiling degeneracy) added. Tallies updated to 19 items. |

---

## V8 — Infant mortality — N/A

Not enabled (Decision 3).

---

## Sign-off

| Gate | Result | Recorded by | Date |
|---|---|---|---|
| V0 | PASS | Claude (session) | 2026-11-02 |
| V1 | PASS | Claude (session) | 2026-11-02 |
| V2 | PASS | Claude (session) | 2026-11-02 |
| V3 | PASS — flag on V3.4 | Claude (session) | 2026-11-02 |
| V4 | PASS | Claude (session) | 2026-11-02 |
| V5 | V5.1–V5.3, V5.5 PASS; **V5.4 FAIL on criterion** — optimum coincides with the ceiling; see analysis and options | Claude (session) | 2026-11-02 |
| V6 | PASS | Claude (session) | 2026-11-02 |
| V7 | PASS | Claude (session) | 2026-11-02 |
| V8 | n/a | — | — |

All five decisions are recorded with their chosen values above.

**Overall.** Every gate passes except the literal threshold in V5.4. The behaviour the
plan set out to remove — emergency replacement forced at the condition floor — is gone at
both sites and cannot recur without a segment failure (V1). Wear-out gives end of life a
physical consequence and is proven load-bearing for the interior optimum (V2, V5.3). The
runaway to the budget ceiling is resolved as a mechanism; what V5.4 catches is that this
network's optimal programme sits at roughly the ceiling that was restored. Closing V5.4 is
a Decision-5 choice, set out in the three options above, not further engineering.

Evidence files: `v5_optimizer.log`, `gates_v5.4-v6.2.txt`, `v5.4_plateau.txt`,
`v5.4_above_ceiling.txt`, and the regenerated `Optimization_Results_NSGA2/`.
