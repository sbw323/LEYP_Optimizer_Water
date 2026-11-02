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

## V5 — The symptom is resolved — PASS (V5.4 pending the full run)

| Check | Result |
|---|---|
| V5.1 Ceiling | `genes.budget.max = 2000000` ✓; `n_replicates = 8`. |
| V5.2 Coarse sweep (5 reps) | $10k 77.7 · $200k 73.3 · $500k 52.4 · $1M 53.5 · **$1.5M 50.2** · $2M 50.3 — minimum strictly interior. `test_budget_optimum_is_interior` green at 8 reps on a sweep extended to $5M. |
| V5.3 Control | `test_wearout_is_load_bearing_for_interior_optimum` green: with `M = 1.0` the minimum returns to a bound. |
| V5.4 Full optimizer | _pending — see below_ |
| V5.5 vs V0 | V0 min-cost budget **$5,000,000** (top of range) → V5 **$1,500,000** (interior). |

**Honesty note on V5.2.** $1.5M (50.2) and $2M (50.3) differ by $0.1M against a replicate
sd of ~$2M. The surface is flat above ~$500k; the gate passes because the *lower* bound is
decisively rejected (77.7 vs 50.2) and the argmin is no longer forced to the ceiling — not
because a single sharp optimum exists. Read the Pareto front, not the point.

### V5.4 — full optimizer run

_To be filled from `verification/eol/v5_optimizer.log` and `Optimization_Results_NSGA2/`._

- Selected budget: __ (__% of ceiling; pass < 90%)
- Trigger: __
- `[warning] ... upper gene bound` present: __
- Front solutions above 90% of ceiling: __ of 32

---

## V6 — Plan health and reproducibility — PASS (V6.1/V6.2 pending the full run)

| Check | Result |
|---|---|
| V6.1 Health metrics on the regenerated plan | _pending_ |
| V6.2 Cost-effectiveness break-even exists | _pending_ |
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
| V5 | V5.1–V5.3, V5.5 PASS; V5.4 _pending_ | Claude (session) | 2026-11-02 |
| V6 | V6.3–V6.5 PASS; V6.1–V6.2 _pending_ | Claude (session) | 2026-11-02 |
| V7 | PASS | Claude (session) | 2026-11-02 |
| V8 | n/a | — | — |

All five decisions are recorded with their chosen values above.
