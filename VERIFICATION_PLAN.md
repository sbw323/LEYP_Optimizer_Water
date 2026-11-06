# End-of-Life Modelling — Verification Plan

Companion to `EOL_MODELLING_PLAN.md` (revision 2). One gate per required change, plus a
baseline gate before anything is touched and an integration gate at the end. Each gate
states what to run, what passes, and what file is kept as evidence. **Do not proceed past
a failing gate.**

Evidence is written to `verification/eol/` (git-ignored except for the final `REPORT.md`).
Every measurement uses `seed=20260830` so a gate can be re-run and reproduced to the dollar.

| Gate | Verifies | Blocking |
|---|---|---|
| V0 | Baseline captured before any change | yes |
| V1 | Both floor-triggered replacements removed (plan §3A) | yes |
| V2 | Age-based wear-out multiplier behaves as specified (plan §3B) | yes |
| V3 | Calibration re-anchored and the model is stable (plan §3C) | yes |
| V4 | Floor-dwell and the two-clock gap are visible (plan §3D) | yes |
| V5 | The symptom is resolved: interior budget optimum | yes |
| V6 | The plan is still healthy and reproducible | yes |
| V7 | Documentation and backlog reflect the change | yes |
| V8 | Infant-mortality term, if enabled | conditional |

---

## V0 — Baseline, before any change

Purpose: a frozen reference so every later gate is a comparison, not an impression.

**Run**

```bash
mkdir -p verification/eol/v0
python3 - <<'PY'
import json
from leyp_runner import simulate
IN="Louisa_wConduits_Input_CSV.csv"
out={}
for b in (0, 200_000, 500_000, 1_000_000, 2_000_000, 5_000_000):
    r=simulate(IN, annual_budget=b, rehab_trigger=1.5, seed=20260830, n_replicates=3)
    out[b]={k:r[k] for k in ("investment_cost","risk_cost","cip_pipes","emergency_pipes","total_breaks")}
u=simulate(IN, annual_budget=0.0, rehab_trigger=3.5, seed=20260830, n_replicates=3, no_intervention=True)
out["untreated_100yr_rate"]=u["total_breaks"]/(21.74*100)
json.dump(out, open("verification/eol/v0/baseline.json","w"), indent=2)
PY
cp Optimization_Results_NSGA2/cost_summary.csv verification/eol/v0/
cp Optimization_Results_NSGA2/nsga2_results.csv verification/eol/v0/
pytest -q | tail -1
```

**Pass**

- `baseline.json` written; its budget sweep shows total cost falling monotonically to the
  highest budget (this is the symptom, recorded).
- `cost_summary.csv` shows `Emergency_degradation > 0` (the behaviour to be removed,
  recorded).
- Test suite green at the starting commit; record the count.

**Evidence** `verification/eol/v0/baseline.json`, the two CSVs, suite count in `REPORT.md`.

---

## V1 — Floor-triggered replacement removed at both sites

Purpose: emergency replacement happens for exactly one reason — a segment reached the
break threshold.

**V1.1 Static — the triggers are gone from the source**

```bash
grep -n "current_condition <= 1.001" leyp_runner.py
grep -rn "CAUSE_DEGRADATION\|CAUSE_BREAK_DAMAGE" --include=*.py .
```

Pass: the first command returns **no match inside the annual loop** (a match in the
`Pipes_At_Floor` diagnostic from V4 is allowed and must be the only one). The second
returns nothing.

**V1.2 Unit — a worn-out pipe that has not broken is left in service**

```bash
pytest -q tests/test_runner.py::test_floor_pipe_is_not_replaced_without_a_break
```

Pass: green. The test forces a pipe to condition 1.0 with zero breaks and zero budget, runs
one year, and asserts the pipe is still at the floor with no emergency row.

**V1.3 Integration — every emergency row is break-driven**

```bash
pytest -q tests/test_runner.py::test_every_emergency_replacement_follows_a_segment_failure
python3 - <<'PY'
import pandas as pd
from leyp_runner import run_simulation
run_simulation(override_input_path="Louisa_wConduits_Input_CSV.csv", annual_budget=500_000,
    rehab_trigger=1.5, output_dir="verification/eol/v1", generate_report=True,
    seed=20260830, n_replicates=3)
ap=pd.read_csv("verification/eol/v1/Optimal_Action_Plan.csv")
em=ap[ap.Action=="Emergency_Replacement"]
print("emergency rows:", len(em))
print("cause values:", em.Cause.value_counts().to_dict())
assert set(em.Cause)=={"break_failure"} or em.empty
PY
```

Pass: `cause values` is `{'break_failure': N}` and nothing else; assertion holds.

**V1.4 Regression — the rewritten tests exist and the old ones are gone**

```bash
pytest -q --collect-only -q 2>/dev/null | grep -c "age_out\|degradation_emerg"
```

Pass: `0`. The four tests listed in plan §5 "Rewrite" no longer exist under their old names.

**Evidence** `verification/eol/v1/` outputs; grep results pasted into `REPORT.md`.

---

## V2 — Age-based wear-out multiplier

Purpose: the multiplier is the bathtub's wear-out arm — unity through average life, rising
sharply after it, bounded, resetting on renewal — and nothing else about the hazard changed.

**V2.1 Unit — shape**

```bash
pytest -q tests/test_leyp_core.py -k "wearout"
```

Pass: all green. Specifically:

| Test | Asserts |
|---|---|
| `test_wearout_is_unity_before_onset` | multiplier == 1.0 for age ≤ onset × base_life, every material |
| `test_wearout_is_monotone_in_age` | non-decreasing on a dense age grid, every material |
| `test_wearout_is_bounded` | ≤ `WEAROUT_MAX_MULTIPLIER` always; ≥ 0.99 × it at r = 5 |
| `test_wearout_onset_is_at_average_life` | 1.0 at r = 1.00, > 1.0 at r = 1.01 |
| `test_wearout_resets_on_replacement` | 1.0 immediately after `execute_replacement`, and for `base_life[HDPE]` years after |
| `test_inherited_backlog_starts_in_wearout` | AC/GI/CI at inventory ages > 1.0 at t = 0; DI at 96 == 1.0 |

**V2.2 Unit — the term is removable without trace**

```bash
pytest -q tests/test_leyp_core.py::test_max_multiplier_of_one_disables_wearout
```

Pass: green. With `WEAROUT_MAX_MULTIPLIER = 1.0`, `calculate_hazard` equals the pre-change
formula to machine precision for every material at ages 0–200.

**V2.3 Unit — composition with existing terms**

```bash
pytest -q tests/test_leyp_core.py::test_hazard_still_scales_with_leyp_feedback \
          tests/test_leyp_core.py::test_leyp_feedback_scales_hazard \
          tests/test_leyp_core.py::test_feedback_saturates_at_the_cap
```

Pass: green. Wear-out multiplies the LEYP term; the LEYP cap still holds.

**V2.4 Curve — print and keep the multiplier table**

```bash
python3 - <<'PY'
import math
from leyp_config import WEAROUT_MAX_MULTIPLIER as M, WEAROUT_STEEPNESS as k, WEAROUT_ONSET_LIFE_RATIO as o
rows=[(r, 1+(M-1)*(1-math.exp(-k*max(0,r-o)))) for r in (0.5,0.9,1.0,1.1,1.25,1.5,2.0,3.0,5.0)]
open("verification/eol/v2_curve.txt","w").write("\n".join(f"{r:.2f} {m:.3f}" for r,m in rows))
for r,m in rows: print(f"  age/life {r:>4.2f} -> {m:5.2f}x")
PY
```

Pass: reads 1.00× at and below 1.0; strictly increasing after; last row within 2% of `M`.
With the proposed defaults: 2.63× at 1.10, 4.54× at 1.25, 6.69× at 1.50, 9.84× at 3.0.

**V2.5 Integration — failures land on old pipes**

```bash
pytest -q tests/test_runner.py::test_emergencies_fall_on_old_pipes
python3 -c "
import pandas as pd; s=pd.read_csv('verification/eol/v1/cost_summary.csv').iloc[0]
print('Mean_Life_Ratio_At_Emergency =', round(s['Mean_Life_Ratio_At_Emergency'],2))"
```

Pass: green, and the printed mean life ratio is **> 1.0** — the pipes that reach emergency
replacement are, on average, past their average life.

**Evidence** `verification/eol/v2_curve.txt`; test output in `REPORT.md`.

---

## V3 — Calibration re-anchored; model stable

Purpose: the hazard model is calibrated on today's network, not on a century of neglect,
and does not diverge when left alone.

**V3.1 Window rate in band**

```bash
pytest -q tests/test_calibration.py::test_calibration_window_rate_is_defensible
python3 - <<'PY'
from leyp_runner import simulate
from leyp_config import CALIBRATION_WINDOW_YEARS as W, HAZARD_LENGTH_SCALE
r=simulate("Louisa_wConduits_Input_CSV.csv", annual_budget=0.0, rehab_trigger=3.5,
           seed=20260830, n_replicates=5, no_intervention=True, horizon_years=W)
rate=r["total_breaks"]/(21.74*W)
print(f"window={W}yr  scale={HAZARD_LENGTH_SCALE}  rate={rate:.3f} breaks/mi/yr")
assert 0.15 <= rate <= 0.40
PY
```

Pass: rate within **0.15–0.40**; test green. Record the scale.

**V3.2 Long run finite and above the window**

```bash
pytest -q tests/test_calibration.py::test_long_untreated_run_stays_finite
```

Pass: green. 100-year untreated rate **< 5.0** and **≥ the window rate**. (An ageing
network left alone should break more, not less; a value far below the window would mean
the wear-out term is not firing.)

**V3.3 Scale pinned with its derivation**

```bash
pytest -q tests/test_calibration.py::test_hazard_scale_is_the_calibrated_value
grep -n "HAZARD_LENGTH_SCALE" leyp_config.py
```

Pass: test green at the new value; the comment above the constant names the window length,
the target rate, the `WEAROUT_MAX_MULTIPLIER` it was derived under, and the date. Expected
order of magnitude: ~600 at Mmax = 1, ~4,600 at Mmax = 10 (plan §2.2).

**V3.4 Sensitivity recorded**

```bash
python3 - <<'PY'
import importlib, leyp_config, leyp_core, leyp_runner
from leyp_runner import simulate
base=leyp_config.WEAROUT_MAX_MULTIPLIER
for M in (5,10,20):
    leyp_config.WEAROUT_MAX_MULTIPLIER=M; importlib.reload(leyp_core); importlib.reload(leyp_runner)
    r=leyp_runner.simulate("Louisa_wConduits_Input_CSV.csv", annual_budget=0.0, rehab_trigger=3.5,
        seed=20260830, n_replicates=3, no_intervention=True, horizon_years=leyp_config.CALIBRATION_WINDOW_YEARS)
    print(f"Mmax={M:>2}: window rate {r['total_breaks']/(21.74*leyp_config.CALIBRATION_WINDOW_YEARS):.3f}")
leyp_config.WEAROUT_MAX_MULTIPLIER=base; importlib.reload(leyp_core); importlib.reload(leyp_runner)
PY
```

Pass: informational — the three rates are recorded in `REPORT.md`. If the window rate moves
out of band at the neighbouring values of `Mmax`, say so; the calibration is then tightly
coupled to a modelling choice and the report should flag it.

**Evidence** printed figures and test output in `REPORT.md`.

---

## V4 — Floor-dwell and the two-clock gap are visible

Purpose: pipes that sit at the floor are counted, not hidden; the gap between the
condition clock and the age clock is measurable.

**Run**

```bash
pytest -q tests/test_runner.py::test_floor_dwell_is_reported
python3 - <<'PY'
import pandas as pd
d=pd.read_csv("verification/eol/v1/simulation_diagnostics.csv")
s=pd.read_csv("verification/eol/v1/cost_summary.csv").iloc[0]
for c in ("Pipes_At_Floor","Floor_Pipes_Below_Onset"): assert c in d.columns, c
for c in ("Floor_Pipe_Years","Max_Pipes_At_Floor","Mean_Life_Ratio_At_Emergency"): assert c in s.index, c
assert d["Pipes_At_Floor"].sum()==s["Floor_Pipe_Years"]
assert d["Pipes_At_Floor"].max()==s["Max_Pipes_At_Floor"]
print(f"floor pipe-years {s['Floor_Pipe_Years']:.0f}  max at floor {s['Max_Pipes_At_Floor']:.0f}")
print(f"share of floor pipe-years below wear-out onset: "
      f"{100*d['Floor_Pipes_Below_Onset'].sum()/max(1,d['Pipes_At_Floor'].sum()):.0f}%")
PY
```

**Pass**

- Columns present; per-year values reconcile with the summary.
- The two printed figures are recorded. There is no pass threshold on the numbers
  themselves — they are the exposure the plan makes visible — but the **share of floor
  pipe-years below onset** is the input to decision §7.2 in the plan. If it is large,
  the condition clock and the age clock disagree materially and that decision needs to be
  taken before sign-off.

**Evidence** figures in `REPORT.md`.

---

## V5 — The symptom is resolved

Purpose: with a realistic ceiling restored, the optimizer no longer drives the budget to
its bound, and the reason is the wear-out term.

**V5.1 Ceiling restored**

```bash
python3 -c "import yaml; c=yaml.safe_load(open('optimizer_config.yaml')); print(c['genes']['budget']['max']); assert c['genes']['budget']['max']==2000000"
```

Pass: `2000000`.

**V5.2 Interior optimum on a coarse sweep**

```bash
pytest -q tests/test_optimizer.py::test_budget_optimum_is_interior
python3 - <<'PY'
import json
from leyp_runner import simulate
IN="Louisa_wConduits_Input_CSV.csv"; out={}
for b in (10_000, 200_000, 500_000, 1_000_000, 1_500_000, 2_000_000):
    r=simulate(IN, annual_budget=b, rehab_trigger=1.5, seed=20260830, n_replicates=5)
    out[b]=(r["investment_cost"]+r["risk_cost"])/1e6
json.dump(out, open("verification/eol/v5_sweep.json","w"), indent=2)
bs=sorted(out); best=min(out,key=out.get)
for b in bs: print(f"  ${b:>9,}: ${out[b]:6.1f}M" + ("  <- min" if b==best else ""))
assert best not in (bs[0], bs[-1]), "optimum is at a bound"
PY
```

Pass: test green; the minimum is **strictly inside** the swept range, not at $10k or $2M.

**V5.3 Wear-out is what makes it interior (control)**

```bash
pytest -q tests/test_optimizer.py::test_wearout_is_load_bearing_for_interior_optimum
```

Pass: green. With `WEAROUT_MAX_MULTIPLIER = 1.0` the same sweep has its minimum at the upper
bound. This pins §2.2's finding so the term cannot later be removed "because it seems not
to matter".

**V5.4 Full optimizer run**

```bash
python3 leyp_optimizer.py 2>&1 | tee verification/eol/v5_optimizer.log
grep -E "Best Strategy|Parameters:|knee would|warning" verification/eol/v5_optimizer.log
python3 -c "
import pandas as pd; d=pd.read_csv('Optimization_Results_NSGA2/nsga2_results.csv')
cap=2_000_000; sel=d.iloc[0].Budget
print(f'selected budget \${sel:,.0f} = {100*sel/cap:.0f}% of ceiling')
print(f'front solutions above 90% of ceiling: {(d.Budget>0.9*cap).sum()} of {len(d)}')
assert sel < 0.9*cap"
```

Pass: selected budget **< 90% of the ceiling**; no `[warning] ... upper gene bound` line in
the log; fewer than a quarter of front solutions above 90% of the ceiling.

**V5.5 Compare with V0**

```bash
python3 - <<'PY'
import json
b0=json.load(open("verification/eol/v0/baseline.json")); s5=json.load(open("verification/eol/v5_sweep.json"))
print("V0 (baseline) min-cost budget:", min((k for k in b0 if k.isdigit()), key=lambda k: b0[k]["investment_cost"]+b0[k]["risk_cost"]))
print("V5 (after)    min-cost budget:", min(s5, key=s5.get))
PY
```

Pass: V0's minimum is at its highest swept budget; V5's is interior. Record both.

**Evidence** `verification/eol/v5_sweep.json`, `v5_optimizer.log`, regenerated
`Optimization_Results_NSGA2/`.

---

## V6 — Plan health and reproducibility

Purpose: fixing the symptom did not break what the earlier phases fixed.

**V6.1 Health metrics**

```bash
python3 - <<'PY'
import pandas as pd
s=pd.read_csv("Optimization_Results_NSGA2/cost_summary.csv").iloc[0]
checks={
 "CIP outnumbers emergency":        s["CIP_Count"] > s["Emergency_Count"],
 "investment share 20-80%":          0.20 <= s["Investment_Share_Of_Total"] <= 0.80,
 "no year-1-2 spike (<=3 abs)":      s["Yr1_2_Emergency_Count"] <= 3,
 "max annual emergencies <= 5":      s["Max_Annual_Emergency_Count"] <= 5,
 "emergencies on old pipes (>1.0)":  s["Mean_Life_Ratio_At_Emergency"] > 1.0,
}
for k,v in checks.items(): print(("PASS " if v else "FAIL ")+k)
assert all(checks.values())
PY
```

Pass: all five lines `PASS`.

**V6.2 Cost-effectiveness panel shows a break-even**

```bash
python3 -c "
import pandas as pd; from water_validation import summarize_validation
c=pd.read_csv('Optimization_Results_NSGA2/validation_data.csv'); s=summarize_validation(c)
print('breakeven up to % length:', round(s['breakeven_up_to_pct_length'],1)); print('max CE:', round(s['max_cost_effectiveness'],2))
assert 0 < s['breakeven_up_to_pct_length'] < 100"
```

Pass: break-even point exists and is **strictly between 0 and 100%** of network length —
the panel is a curve with a crossing, not a flat line.

**V6.3 Reproducibility**

```bash
pytest -q tests/test_runner.py -k "reproduc or same_seed or replicates"
```

Pass: green. Identical genes and seed reproduce to the dollar after the change.

**V6.4 Noise against the flat surface**

```bash
python3 - <<'PY'
import json, yaml, numpy as np
from leyp_runner import simulate
n = yaml.safe_load(open("optimizer_config.yaml"))["simulation"]["n_replicates"]
IN = "Louisa_wConduits_Input_CSV.csv"
totals = []
for k in range(6):
    r = simulate(IN, annual_budget=1_000_000, rehab_trigger=1.5, seed=1000 + k * 97, n_replicates=n)
    totals.append(r["investment_cost"] + r["risk_cost"])
sd = np.std(totals) / 1e6
sweep = json.load(open("verification/eol/v5_sweep.json"))
rng = max(sweep.values()) - min(sweep.values())
print(f"n_replicates={n}: replicate sd ${sd:.2f}M vs sweep range ${rng:.1f}M -> {100*sd/rng:.0f}%")
assert sd / rng < 0.25
PY
```

Pass: replicate standard deviation **< 25% of the sweep's total-cost range**. If it fails,
raise `simulation.n_replicates` and re-run V5.4; record the value that passes.

**V6.5 Full suite**

```bash
pytest -q | tail -1
```

Pass: green; count ≥ V0's count plus the net new tests from plan §5.

**Evidence** outputs pasted into `REPORT.md`.

---

## V7 — Documentation and backlog

**Run**

```bash
grep -n "3+ segments\|CAUSE_DEGRADATION\|age-out\|only route to emergency" README.md
grep -n "WEAROUT_\|CALIBRATION_WINDOW_YEARS\|bathtub\|Pipes_At_Floor\|two clock\|two-clock" README.md | wc -l
grep -n "Superseded by\|MOD-2" OPEN_ITEMS.md | head
```

**Pass**

- First grep: no stale wording; the phrase *only route to emergency replacement* (or
  equivalent) appears in the failure-rule section.
- Second grep: **≥ 6** — the wear-out constants, calibration window, bathtub framing,
  floor-dwell columns and the two-clock note are all documented.
- `OPEN_ITEMS.md`: DEC-1 carries the supersession note; MOD-2's text is updated to say
  breaks are now the sole failure route; a new item records the two-clock inconsistency
  if decision §7.2 chose to accept it rather than align the tables.

**Evidence** grep output in `REPORT.md`.

---

## V8 — Infant-mortality term (only if enabled)

Skip entirely if `INFANT_MORTALITY_MULTIPLIER == 1.0`.

**Run**

```bash
pytest -q tests/test_leyp_core.py -k "infant"
python3 - <<'PY'
import pandas as pd
ap=pd.read_csv("Optimization_Results_NSGA2/Optimal_Action_Plan.csv")
from leyp_config import INFANT_MORTALITY_YEARS as Y
# breaks on pipes renewed within the last Y years vs the following Y years
cip=ap[ap.Action=="CIP_Replacement"][["PipeID","Year"]].rename(columns={"Year":"Renewed"})
br=ap[ap.Action=="Break_Event"].merge(cip, on="PipeID"); br=br[br.Year>br.Renewed]
age=br.Year-br.Renewed
early=(age<=Y).sum(); later=((age>Y)&(age<=2*Y)).sum()
print(f"breaks within {Y} yrs of renewal: {early}; in the following {Y} yrs: {later}")
assert early > later
PY
```

**Pass**

- Unit tests: multiplier > 1 at age 0, decays monotonically to 1.0 by `INFANT_MORTALITY_YEARS`,
  equals 1.0 exactly when the constant is 1.0.
- Integration: more breaks in the first `Y` years after renewal than in the next `Y`.
- Re-run V5.2 and V6.1: the interior optimum and health checks still hold with the term on.
  Record the change in investment share — this term lowers the value of renewal by design,
  and the report must say by how much.

---

## Sign-off

`verification/eol/REPORT.md` is the deliverable: one section per gate with the command
output pasted in, the pass/fail call, and — for V3.4, V4 and V6.4 — the recorded figures
and any decision they triggered. Sign-off requires V0–V7 passed (V8 if applicable) and the
five decisions in plan §7 recorded with their chosen values.

| Gate | Result | Recorded by | Date |
|---|---|---|---|
| V0 | | | |
| V1 | | | |
| V2 | | | |
| V3 | | | |
| V4 | | | |
| V5 | | | |
| V6 | | | |
| V7 | | | |
| V8 | n/a unless enabled | | |
