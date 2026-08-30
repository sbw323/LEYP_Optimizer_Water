# LEYP-Water — Software Package Review Plan

**Scope.** Improve the credibility of `Optimization_Results_NSGA2/Optimal_Action_Plan.csv`
and `Optimization_Results_NSGA2/validation_curve.png` against three reported symptoms:

| ID | Reported symptom |
|---|---|
| **A** | Over-predicts pipe breaks and emergency replacements in years 1–2 |
| **B** | Under-emphasizes CIP Replacement across the entire plan |
| **C** | Too many break events throughout the lifecycle |

**Method.** The `README.md` is used as the specification of record. Every claim it makes
about physics, economics, replacement logic and validation was checked against the
implementation and against the committed outputs. Findings below are measured, not
inferred; the reproduction command for each is given in Phase 0.

---

## 1. Baseline measurements (current committed outputs)

Network: 388 pipes, 114,792 ft (21.7 mi), mean diameter 6.0 in, ages 26/46/76/96.
Best strategy in `nsga2_results.csv`: Budget = $399,047/yr, Rehab_Trigger = 2.80.

| Metric | Value | Expectation |
|---|---|---|
| CIP replacements, 100 yr | **40** | should dominate a well-funded plan |
| Emergency replacements, 100 yr | **404** | should be the rare escalation |
| Break events, 100 yr | 328 (341 breaks) | — |
| Emergency replacements in year 1 | **42** | vs. 100-yr mean of 4.1 |
| Investment cost | $6.03 M | — |
| Risk cost | $109.40 M | **94.8% of total spend** |
| Budget actually deployed | **22.5%** of authorized | ~100% |
| Validation curve reach | x ≤ 11.6%, y ≤ 5.9% | README asks to inspect the first **50%** |

The plan is inverted: emergency work outnumbers planned work 10:1 and consumes 95% of
lifecycle cost. That is the signature of all three symptoms, and it has four independent
mechanical causes, not one.

---

## 2. Findings

### 2.1 Symptom A — year 1–2 emergency/break spike

**A1. Seeded break history condemns 40 pipes before the simulation starts.**
`Pipe._seed_breaks` (`leyp_core.py`) draws up to 6 historical breaks and scatters them
across 4 virtual segments. `Pipe.simulate_year` then evaluates `seg.has_failed(3)`
*unconditionally every year*, including the first. Measured at t=0: **40 of 388 pipes
already have a segment holding ≥3 breaks**, so they are declared failed on their first
simulated year no matter what policy is chosen. This is essentially the entire year-1
spike of 42.

**A2. README and code disagree on the failure rule.**
README: *"If 3+ segments accumulate breaks, the pipe is declared failed."*
Code: the pipe fails when **any single segment** reaches 3 breaks. The implemented rule
is far more aggressive than the documented one and is the direct trigger for A1.
Decide which is intended — this is a specification question, not a bug fix.

**A3. Inherited backlog is billed as emergency work.**
There is no burn-in or backlog-triage period. Pipes the utility inherited already past
their service life are charged $800/ft emergency replacement in year 1, before the CIP
program has been allowed to run even once (see B3 for the ordering cause).

### 2.2 Symptom B — CIP under-emphasized

**B1. The budget loop aborts the year on the first unaffordable pipe.** *(largest single cause)*
`ReplacementManager.run_year` (`water_replacement.py`) sorts eligible pipes by risk, then:

```python
if total_spend + cost <= self.budget:
    ...
else:
    break          # <-- abandons the year; should skip and continue
```

Measured over 100 years at the best strategy:
- **65 of 100 years spend $0**, because the single top-ranked pipe alone exceeds the
  whole annual budget;
- **14,863 affordable pipe-years are skipped** behind it;
- only **22.5% of the authorized budget is ever deployed.**

62 of 388 pipes individually cost more than the entire annual budget (largest: $3.85 M).

**B2. The risk ranking is not cost-normalized, so it front-loads unaffordable pipes.**
`Annualized_Risk = Length × CoF × 800 / TTF` has no cost term. Risk scales with length,
and so does cost — so the ranking reliably places the least affordable pipes first,
which is precisely what B1 then chokes on. A benefit/cost (risk-reduction per dollar)
ranking is the standard formulation and removes the interaction.

**B3. Loop ordering hands age-out replacements to the emergency stream.**
`leyp_runner.run_simulation` runs *degrade → emergency-replace → CIP → breaks*. A pipe
that crosses 1.001 during the degrade step is emergency-replaced immediately, and the
CIP eligibility filter excludes `current_condition <= 1.001`. Measured: **291 of 408
emergency replacements (71%) involved zero breaks** — they were pure age-out, exactly
the work a CIP program exists to capture.

**B4. Proactive replacement is not economically preferable to failure.**
| Diameter | CIP $/ft (`$120/inch-ft`) | Emergency $/ft (flat) |
|---|---|---|
| 6 in | 720 | 800 |
| 8 in | **960** | 800 |
| 12 in | **1,440** | 800 |

11% of pipes cost *more* to replace proactively than to let fail. Network-wide: CIP
$84.4 M vs emergency $91.8 M — an 8% margin. And `_emergency_replace` grants an
**identical full reset** (condition 6.0, new HDPE, fresh segments) with **no budget cap
and no delay**. Deferring is close to free, so the optimizer is behaving correctly given
the cost model; the cost model is what is wrong. Industry practice puts emergency work
at 2–5× planned. Nothing in the package discounts future dollars either, so there is no
time-value counterweight.

**B5. The budget gene bound cannot express a solvent policy.**
`optimizer_config.yaml` caps budget at $500k/yr → $50 M over 100 years, against $84.4 M
to replace the network once. No point in the search space keeps up with degradation.
(README documents a $2 M max; the YAML says $500k — reconcile.)

### 2.3 Symptom C — break volume

**C1. Three uncalibrated failure paths compound.** A pipe can fail via (i) exponential
condition decay, (ii) `damage = 0.3 × breaks` subtracted directly from condition, and
(iii) the per-segment break threshold. None is calibrated against an observed break rate;
they interact multiplicatively through the LEYP `(1 + α·n_breaks)` feedback term.

**C2. Hazard units are unvalidated.** `calculate_hazard` returns a per-pipe annual Weibull
hazard, which `VirtualSegment.simulate_breaks` then rescales by `length / 300`
(`HAZARD_LENGTH_SCALE`). The composite has never been checked against a
breaks/mile/year target. The aggregate happens to land near 0.16 breaks/mi/yr, but only
because 404 emergency replacements keep resetting the network to new HDPE — the
underlying per-pipe rate is untested.

**C3. Break events outnumber nothing.** 328 break events vs 404 emergency replacements
is backwards: failure is being driven by condition decay and seeded history, not by
break accumulation. Any recalibration must be judged on this ratio, not on break count alone.

### 2.4 Cross-cutting — the optimizer's answer is mostly noise

**D1. Single stochastic replicate per evaluation, no per-evaluation seeding.**
`run_simulation` never seeds NumPy; each call consumes the global stream, and pipe
initialization (`_seed_breaks`, lognormal TTF) is re-randomized every time. Measured with
identical genes over 6 evaluations:

| | mean | sd | CV |
|---|---|---|---|
| Investment | $9.50 M | $0.78 M | 8.2% |
| Total cost | — | **$2.22 M** | — |

The entire reported Pareto front spans **$3.84 M** in total cost. **Run-to-run noise is
the same order as the signal the front is meant to represent** — the front is not
reliably a front. README's *"Deterministic: Same random seed produces identical results"*
is false as written. Note also that "Monte Carlo" in the README implies replication;
each evaluation is a single draw.

**D2. Search budget is far too small.** `pop_size: 5`, `n_gen: 15`, `n_offsprings: 3`
≈ 50 evaluations for a noisy 2-gene problem. README itself flags this as "small for testing".

### 2.5 `validation_curve.png`

**E1. Replacement counts are fabricated from a fictional average pipe.**
`_estimate_replaced_count_from_cip_cost` divides total CIP cost by an assumed
"8 in × 300 ft" pipe ($288,000). The actual mean cost of pipes the model replaced is
**$150,804** — a 1.9× overstatement that halves the x-axis. The true count is already
sitting in the action log; it does not need estimating.

**E2. The length-based subplot is not length-based.**
`pct_avoided_by_length = pct_avoided_by_number` verbatim. Subplot 2's y-axis is a
duplicate, so the "length-based" panel conveys no additional information.

**E3. Monotonicity is imposed, not observed.** `_make_monotonic_increasing` overwrites any
decrease with the previous value, concealing exactly the D1 noise a reader would use to
judge the curve. The flat tail (5 identical points at 5.917%) is this filter, not a plateau.

**E4. The sweep cannot reach the region the README says to inspect.** Budget is swept only
to $500k (59% of one network replacement) at a fixed trigger of 3.5, so x tops out at
11.6% and y at 5.9%. The README's acceptance test — *"above the diagonal for the first
50% of replacement activity"* — is unevaluable on this plot.

**E5. The zero-budget baseline is not a do-nothing baseline.** Emergency replacement still
renews pipes for free at budget = 0, so `baseline_breaks` understates true no-intervention
breaks and the "% breaks avoided" denominator is wrong.

### 2.6 Housekeeping / README drift

- `Priority` in the action log is computed **after** the pipe is reset to condition 6.0 and
  re-materialed, so the column reports post-replacement risk, not the ranking value used.
- `initially_dead` in `leyp_runner` is always empty — the initialization curve
  `1.0 + 5.0·exp(-1.5·lf)` asymptotes above 1.0 and never reaches the ≤1.001 test. Dead code.
- `avg_failure_cost_per_ft` (YAML) and `GLOBAL_COST_PER_FT` (config) are never read.
- README documents condition init as `6.0 - 5.0 × life_fraction` (linear); the code uses
  exponential decay. README documents budget max $2M; YAML says $500k.
- README references `tests/`, "80+ unit tests", `pytest tests/ -v`, and
  `config.checkpoint` — **none exist in the repository.** There is no test suite at all.

---

## 3. Phased review plan

Ordering matters: Phase 0 must land first or no later change is measurable, and the
economics (Phase 3) must be settled before any recalibration, or the model will be tuned
to a cost structure that is itself wrong.

### Phase 0 — Make results measurable *(prerequisite)*
1. Add a `seed` parameter to `run_simulation`; seed NumPy per evaluation so identical
   genes give identical objectives.
2. Add an `n_replicates` option; return mean (and spread) across replicates so the
   objective is an expectation, not a draw.
3. Add a diagnostics writer: emergency replacements by root cause and year, budget
   utilization per year, breaks per mile-year, CIP:emergency ratio.
4. Stand up a minimal `tests/` suite (the README already promises one) covering hazard,
   condition init, cost, and the budget loop.

**Gate:** repeated runs at fixed genes reproduce to the dollar.

### Phase 1 — Initialization and burn-in *(Symptom A)*
5. Decide and align the failure rule (A2): *N segments with breaks* vs *N breaks in one
   segment*. Fix code or README so they agree.
6. Prevent pre-condemnation at t=0 (A1) — seed break history without pre-tripping the
   failure test, or evaluate the failure test only against breaks accrued in-simulation.
7. Decide the treatment of inherited backlog (A3): explicit year-0 triage, a burn-in
   period, or a distinct "inherited backlog" cost stream reported separately from
   simulated emergencies.

**Gate:** year 1–2 emergency replacements are within ~2× the 100-year annual mean.

### Phase 2 — Replacement engine *(Symptom B, largest wins)*
8. **Change the budget `break` to a skip-and-continue** (B1), with a guard for pipes that
   can never be afforded in a single year (multi-year programming or an explicit
   "unfundable" report).
9. Re-formulate prioritization as risk-reduction per dollar (B2).
10. Reorder the annual loop so the CIP manager sees pipes *before* they are lost to the
    emergency stream, and widen eligibility so condition ≤ 1.001 pipes remain CIP
    candidates (B3).

**Gate:** budget utilization > 90%; zero-spend years eliminated.

### Phase 3 — Cost model *(Symptom B, decides the optimum)*
11. Re-derive the CIP vs emergency cost relationship (B4) so emergency work carries a
    realistic premium (2–5×), including social/disruption cost, and make emergency
    replacement diameter-aware for comparability.
12. Consider whether emergency replacement should deliver a *worse* reset than CIP
    (rushed installation, no upsizing) rather than an identical one.
13. Add NPV discounting to both objectives.
14. Raise the budget gene bound (B5) to a range that can express a solvent policy;
    reconcile with the README.

**Gate:** a positive-CIP strategy dominates the deferral strategy on total discounted cost.

### Phase 4 — Hazard calibration *(Symptom C)*
15. Calibrate `HAZARD_LENGTH_SCALE`, `ALPHA`, and `SEGMENT_BREAK_THRESHOLD` against a
    target breaks/mile/year for the untreated network (C1, C2).
16. Review the three compounding failure paths and the `0.3 × breaks` condition penalty;
    reduce to a defensible set.

**Gate:** untreated-network break rate lands in a defensible band (e.g. 0.1–0.5
breaks/mi/yr for this age/material mix), and break events exceed emergency replacements.

### Phase 5 — Optimizer validity
17. Raise `pop_size` / `n_gen` to a level appropriate for a noisy objective (D2).
18. Re-check the noise-to-signal ratio from Phase 0 after Phases 2–4; the front is only
    meaningful once replicate spread is well below front spread.
19. Reconsider selecting the reported plan by `Total_Cost.idxmin()` — with equal weighting
    and no discounting this collapses the multi-objective result to one arbitrary point.

**Gate:** replicate sd is a small fraction of Pareto spread.

### Phase 6 — Validation curve rebuild
20. Count actual CIP actions from the action log instead of the fictional average pipe (E1).
21. Make the length panel genuinely length-based (E2).
22. Remove the imposed monotonicity; plot the real curve with replicate spread (E3).
23. Extend the budget sweep so the x-axis spans 0–100% of the network, enabling the
    README's own "above the diagonal for the first 50%" acceptance test (E4).
24. Define a true do-nothing baseline that disables emergency renewal (E5).

**Gate:** the curve spans the full x-range and sits above the diagonal over the first
50%, as the README specifies.

### Phase 7 — README reconciliation
25. Correct the failure rule, condition-init formula, budget bounds, determinism claim,
    and the references to a non-existent test suite and `config.checkpoint` module.
26. Remove the three duplicated preemption/checkpointing sections (the README documents
    the same GCP deployment material four times).
27. Prune the dead configuration keys.

---

## 4. Acceptance criteria for the reworked outputs

`Optimal_Action_Plan.csv`
- Year 1–2 emergency replacements within ~2× the 100-year annual mean.
- CIP replacements **outnumber** emergency replacements over the horizon.
- Investment cost is a substantial share of total lifecycle cost (not ~5%).
- Annual CIP spend approaches the authorized budget in most years.
- Break events outnumber emergency replacements.
- `Priority` reflects the value actually used for ranking.

`validation_curve.png`
- x-axis spans 0–100% of the network.
- Curve sits above the diagonal over the first 50% of replacement activity.
- Length panel differs from the count panel.
- Curve reflects observed values, with replicate spread shown, not imposed monotonicity.

---

## 5. Suggested sequencing

Phases 0 → 2 → 3 deliver most of the correction: the budget `break` (B1) alone is
suppressing 77.5% of planned investment, and the cost model (B4) is why the optimizer
prefers to defer. Phase 1 removes the year-1 artifact. Phase 4 addresses break volume on
top of a then-correct policy engine. Phases 5–7 make the result trustworthy and the
documentation honest.

Recommend confirming the **Phase 1.5 (A2 failure rule)** and **Phase 3.11 (emergency
cost premium)** decisions before implementation begins — both are modeling policy calls
that determine what "correct" means for everything downstream.
