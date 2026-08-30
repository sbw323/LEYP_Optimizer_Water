# LEYP-Water — Open Items

Everything still outstanding after the seven-phase review, including findings that
surfaced during the work rather than in the original plan. Each item carries the
measurement behind it, so nothing here rests on assertion.

| | |
|---|---|
| Branch | `claude/leyp-optimizer-water-review-t8cr56` |
| Pull request | [#2](https://github.com/sbw323/LEYP_Optimizer_Water/pull/2) |
| Tests | 140 passing |
| Items | 16 |

| Category | Count |
|---|---:|
| Latent defect | 1 |
| Your decision | 5 |
| Data quality | 4 |
| Model behaviour | 3 |
| Engineering | 3 |

---

## Latent defect

Currently masked by configuration. It will produce wrong answers silently the moment
that configuration changes.

### SEG-1 — Enabling segmentation silently destroys the risk ranking and asset identity

**Blocking · small**

`leyp_preprocessor.py` builds each segment row from Material, Diameter, Age, Condition
and Length — it never carries `CoF_Value` through. Every pipe would fall back to the
default consequence of 1.0, flattening the term the whole replacement ranking divides
by. It also rewrites identifiers, so no row in the action plan could be traced back to
a real asset.

This is dormant only because `skip_segmentation: True` is set. Nothing warns you if
that flips.

```
preprocess_network(skip_segmentation=False) on the Louisa inventory:
  rows      388  ->  4,607 segments
  columns   PipeID, Material, Diameter, Age, Condition, Length
  CoF_Value ABSENT
  PipeID    "P787"  ->  "Class_0_Seg_1"
```

**Done when** `CoF_Value` survives segmentation, segment IDs retain the parent PipeID,
and a test runs the segmented path end to end.

---

## Decisions only you can make

Each of these is a modelling or policy judgement, not a defect. Every one is left at a
defensible placeholder rather than chosen on your behalf.

### DEC-1 — The optimum is pinned against the budget ceiling

**Decision · small**

The selected strategy sits within 0.1% of the $2M gene ceiling, so the search never
found an interior optimum in budget — the true best may lie above it. The run emits a
warning rather than hiding this.

```
selected budget   $1,998,192
gene ceiling      $2,000,000
run output        [warning] budget sits at the upper gene bound
```

**Done when** either the ceiling is raised until the optimum turns interior, or $2M is
confirmed as a real funding constraint and the warning is expected.

### DEC-2 — Discount rate and emergency premium are placeholders, not Louisa's numbers

**Decision · small**

The 3% real discount rate is a conventional default. The $5,000/ft emergency rate was
chosen to test whether the mechanism responds — it is not sourced from Louisa's actual
emergency job costs. Both move the answer substantially.

```
DISCOUNT_RATE                      0.03   (conventional default)
EMERGENCY_REPLACEMENT_COST_PER_FT  5000   (chosen to test the mechanism)
CIP_REPLACEMENT_COST_PER_INCH_FT    120   (~$720/ft at 6 in)
effect of the premium: $0.51 -> $3.18 of risk avoided per CIP dollar
```

**Done when** both are replaced with figures from Louisa's own cost records and the
optimizer is re-run.

### DEC-3 — Emergency replacement still delivers an asset identical to planned work

**Decision · medium**

A pipe replaced in an emergency comes back as the same new HDPE main, on the same
clock, as one replaced under the capital programme. Rushed work typically cannot
upsize, coordinate with other works, or achieve the same bedding and compaction. This
was item 12 of the review plan and was never actioned.

Until it is, the model understates the case for planned work in asset terms — the
premium is captured only in dollars.

**Done when** a decision is recorded either way: emergency work gets a degraded reset
(shorter effective life, no upsizing), or identical reset is accepted and documented as
deliberate.

### DEC-4 — Retire the validation curve's diagonal criterion

**Decision · small**

The README's original acceptance test — the curve should sit above the diagonal over
the first 50% of replacement activity — does not pass, and the evidence says the
criterion is what is wrong. Renewal here is overwhelmingly age-out driven, and age-out
produces no breaks, so moving a pipe from the emergency stream to the capital programme
changes who pays and when far more than how often it breaks.

The rebuilt curve carries a cost-effectiveness panel that measures the real value
proposition. The old criterion is documented but not formally retired.

```
across $0 -> $2M annual budget:
  emergency replacements   384  ->   5
  breaks                    89  ->  60
at zero budget, failures by cause:
  degradation  409      break-driven  10
```

**Done when** the diagonal test is either formally dropped in favour of the
cost-effectiveness panel, or a corrected break-exposure-weighted diagonal is specified
to replace it.

### DEC-5 — Unfundable pipes need a policy only if you adopt a lower budget

**Conditional · medium**

Pipes costing more than an entire year's budget can never be funded in one year. The
engine reports them rather than letting them block the queue, but there is no
multi-year programming to actually deliver them.

At the selected $2M budget this is not currently binding. It becomes material fast at
lower budgets, so it matters if DEC-1 resolves downward.

```
unfundable pipes per year
  at $2,000,000 budget    0.1 mean, 1 max, 10 of 100 years affected
  at   $399,047 budget   30.2 mean, 40 max
```

**Done when** either the adopted budget keeps unfundable near zero, or a
reserve-accrual or phased-construction rule is added for large mains.

---

## Data quality

The largest available gain in credibility. The model is now internally sound; its
remaining weakness is what it is fed.

### DAT-1 — Calibrate break rates against Louisa's own history, not national averages

**High value · medium**

`HAZARD_LENGTH_SCALE` is currently anchored on Folkman's 2018 national rates weighted
by this network's material mix, scaled up for its age. That is defensible, but it is a
literature anchor standing in for local evidence. Actual break records would replace
the single weakest assumption in the model.

```
current anchor   0.077 breaks/mi/yr  (Folkman 2018, weighted, all ages)
calibrated to    0.287 breaks/mi/yr  (untreated, this network, 100 yr)
knob             HAZARD_LENGTH_SCALE = 1500.0
```

**Done when** the constant is re-derived against observed breaks per mile-year from
Louisa's maintenance records, and `test_untreated_break_rate_is_defensible` is updated
to the local band.

### DAT-2 — The Condition column is an era flag, not a condition assessment

**High value · medium**

The inventory's `Condition` field takes only two values, acting as an indicator of
installation era rather than observed condition. The model therefore derives condition
almost entirely from age and material, with the field applying a small upward nudge for
newer assets.

Real inspection or leak-survey data would materially change which pipes the plan
selects, and in what order.

```
Condition value counts:  {1: 315,  2: 73}
used as:  boost = max(0, (Condition - 1) * 0.5)
```

**Done when** genuine condition grades are supplied on the 1–6 scale, or the field is
renamed to what it actually is so no one mistakes it for an assessment.

### DAT-3 — Consequence of failure is a three-point integer scale

**Improvement · small**

`CoF_Value` takes only the values 1, 2 and 3. Since consequence enters the replacement
priority directly, this coarseness limits how finely the ranking can discriminate
between assets — a hospital feeder and a slightly-above-average street main can score
identically.

```
CoF_Value counts:  {2: 173,  1: 170,  3: 45}
```

**Done when** consequence reflects real criticality — customers affected, critical
facilities, traffic disruption, pressure-zone role.

### DAT-4 — Three inventory files in the repository are referenced by no code

**Housekeeping · small**

Only `Louisa_wConduits_Input_CSV.csv` is read. The others sit alongside it with no
indication of which is authoritative or how they relate, which is how the wrong file
eventually gets used.

```
Louisa_LEYP_Conditioner.xlsx                         0 references
Louisa_wConduits_Input_CSV.xlsx                      0 references
Louisa_wConduits_Risk_Table(wPipes_Nominal_Flow).csv 0 references
```

**Done when** the authoritative source is documented and the rest are removed or moved
to a clearly-labelled source directory.

---

## Model behaviour to understand

Not defects. Each is behaviour that would mislead someone reading the output without
knowing about it.

### MOD-1 — The best replacement trigger is conditional on budget

**Caveat · small**

There is no single trigger to recommend. A tighter trigger only pays once the budget
can fund everything that becomes eligible; below that it starves the programme and work
escapes into the emergency stream.

```
best trigger at $1,000,000 budget   1.5
best trigger at $1,998,192 budget   1.12
(1.12 verified interior: beats 1.05 at $72.1M and 1.30 at $59.4M)
```

**Done when** any published recommendation states the budget it assumes, rather than
quoting a trigger alone.

### MOD-2 — The break-repair cost stream is immaterial to the objective

**Check · small**

Risk cost is almost entirely emergency replacement; point repairs contribute under half
a percent. Either the per-break cost understates what a repair really costs Louisa, or
the stream genuinely does not matter — and it is worth knowing which, because the model
currently optimises as though breaks themselves are nearly free.

```
at the selected plan:
  repair (breaks)         $     325,000    0.46% of risk
  emergency replacement   $ 70,080,914   99.54% of risk
  EMERGENCY_REPAIR_COST_PER_BREAK = $5,000
```

**Done when** the per-break cost is checked against real repair job costs, including
crew time, traffic control and water loss.

### MOD-3 — No sensitivity harness for the parameters that move the answer

**Tooling · medium**

Several constants materially change the result, and each was checked by an ad-hoc
script during the review rather than by anything reusable. There is no way to re-run
that analysis after a parameter changes.

```
parameters with demonstrated leverage:
  EMERGENCY_REPLACEMENT_COST_PER_FT   flips whether CIP pays for itself
  DISCOUNT_RATE                       shifts the balance toward deferral
  ALPHA / LEYP_BREAK_FEEDBACK_CAP     0.245-0.384 breaks/mi/yr across caps 5-40
  SEGMENT_BREAK_THRESHOLD             sets failure frequency
```

**Done when** a single command sweeps these and writes a tornado or one-at-a-time
sensitivity table into the results directory.

---

## Engineering

### ENG-1 — The test suite gates nothing

**Recommended · small**

There are 140 tests and no continuous integration, so nothing runs them on push.
Several of them exist specifically to catch silent regressions — the calibrated hazard
constant, the untreated break-rate band, reproducibility — and those only protect
anything if they actually run.

```
.github/workflows   does not exist
pytest              140 passing, locally only
```

**Done when** a GitHub Actions workflow runs `pytest` on every push and pull request.

### ENG-2 — Seeding uses the global stream, which rules out in-process parallelism

**Improvement · medium**

Reproducibility is achieved by seeding NumPy's global random state, so concurrent
evaluations in one process would interfere. The optimizer runs serially as a result —
about eleven minutes for a full search.

This is documented rather than hidden, and is only worth fixing if run time starts to
constrain how often you iterate.

```
512 evaluations x 5 replicates x ~0.25s  ~=  11 min serial
```

**Done when** either a `Generator` instance is threaded through the physics, or
evaluations are parallelised across processes.

### ENG-3 — Four unused symbols remain from earlier designs

**Housekeeping · small**

Each is defined and never called, or set and never read. They are harmless today but
read as live API, which is how someone eventually builds on something that was
abandoned.

```
Pipe.predict_ttf              defined, never called
Pipe.reset_breaks             defined, never called
map_condition_to_n_start      imported into leyp_core, never called
Pipe.initial_n_breaks         assigned, never read
```

**Done when** each is removed, or given a caller and a test if it is meant to stay.

---

## Settled — please don't re-open these

The following were raised during the review and closed by decision rather than by code,
so they will look like gaps if you come back to them cold.

- **Failure rule.** A pipe is condemned when any single segment reaches three breaks.
  This is your decision; the code always did this and the README's "3+ segments"
  wording was corrected to match.
- **Default annual budget.** `ANNUAL_BUDGET = 50000` stays as is. It cannot fund a
  solvent programme, so single runs at the default will look poor — the optimizer picks
  its own budget and is unaffected.
- **Budget utilisation below 90%.** Now around 42% with a handful of zero-spend years.
  That is correct at the selected strategy: the binding constraint is the trigger, not
  the money.
- **Year 1–2 emergency ratio reading above 2×.** An artefact of a collapsed denominator,
  not a regression. Absolute counts are the metric to read, and the maximum in any
  single year is now one.
