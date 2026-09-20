# HANDOFF — ChatGPT speed-index coverage guard (2026-09-20)

## 0. Purpose / branch safety

This is the second ChatGPT handoff after the 2026-09-20 race review.

- Repository: `reiji55/Predict-horse-race`
- Work branch: `chatgpt/fix-speed-coverage-20260920`
- Base commit: `bdf915ce3adc2a111e137ab14427a0a071581c4c`
- Production branch at start: `claude/content-review-full-rw32o2`
- **Production was not modified by this work.**
- Please review this branch before merge.

The user asked to address the highest-priority structural issue found after the 2026-09-20 races, using the same review-first workflow as the prior ChatGPT PR.

---

## 1. Problem found from the 2026-09-20 All Comers review

The All Comers exposed a non-random missing-data bug in factor ① speed.

The current `config/base_times.json` is still very incomplete. In the 2026-09-20 All Comers:

- No base time existed for many relevant turf courses/distances.
- Horse 4 (ヴーレヴー) had recent strong turf form, including a heavy-ground turf win and a G3 turf placing.
- Those turf runs could not be indexed because their venue/surface/distance combinations were absent from `base_times.json`.
- The only runs that happened to be indexable for that horse were poor dirt runs.
- Therefore the old speed factor used those dirt losses and gave the horse a strongly negative speed contribution.
- Horses with no indexable speed runs at all simply had `speed_raw=None`, and `base_score` renormalized the remaining factors.

That creates an asymmetric failure mode:

> horse with no speed data → not penalized by speed  
> horse with only an unrepresentative subset of speed data → can be heavily penalized

This is not just "the model picked the wrong horse"; it is a data-availability artifact.

The fix in this branch is a **fail-safe**, not an attempt to tune the model to the race result.

---

## 2. Design adopted

### A. Same-surface only for prediction-time speed

`logic/speed_index.compute_horse_speed` now accepts `target_surface`.

When `config/speed_index.json.guard.same_surface_only=true` and the prediction race surface is known:

- turf prediction → only turf past runs may enter speed
- dirt prediction → only dirt past runs may enter speed
- obstacle runs remain excluded as before

Rationale:

The speed index is theoretically course-normalized, but with a sparse base-time table a cross-surface fallback can select an arbitrary and unrepresentative subset. Until the base-time table is much more complete, same-surface use is the safer assumption.

### B. Minimum usable runs per horse

New config:

```json
"guard": {
  "same_surface_only": true,
  "min_usable_runs": 2,
  "min_race_coverage": 0.5,
  "neutral_impute_missing": true
}
```

A horse with fewer than 2 indexable same-surface runs is treated as speed-missing for the composite score.

This prevents a single historical run from carrying the full speed-factor weight.

**Important:** `2` is a provisional safety floor, not a fitted hyperparameter.

### C. Race-level coverage gate

After all horses are evaluated, `speed_index.apply_race_speed_guard` computes:

```text
coverage = horses with >= min_usable_runs / total horses
```

If coverage is below 50%:

- factor ① speed is disabled for **every horse in the race**.

This is deliberately symmetric. We do not allow a sparse subset of horses to be judged on speed while the rest are judged on a renormalized aptitude/human model.

Again, `0.5` is a provisional "at least a majority has usable evidence" safety floor, not an ROI-tuned threshold.

### D. Neutral imputation when coverage is sufficient

If race-level coverage is at least the threshold but some horses are still speed-missing:

- missing `speed_raw` is filled with the **mean of observed qualified speed_raw values**.

Why the mean?

`base_score` z-standardizes the factor within the race. Imputing the observed mean makes the missing horse's speed contribution exactly:

```text
z_speed = 0
```

So "unknown speed" becomes genuinely neutral rather than causing the other factors to be reweighted upward.

The horse remains `uncertain=true`; this does not pretend the imputed value is observed evidence.

---

## 3. New diagnostics

Each prediction race now includes:

`speed_quality`

Example shape:

```json
{
  "raw_available_horses": 4,
  "qualified_horses": 1,
  "total_horses": 13,
  "coverage": 0.0769,
  "min_usable_runs": 2,
  "min_race_coverage": 0.5,
  "same_surface_only": true,
  "used": false,
  "imputed_horses": 0,
  "reason": "insufficient_race_coverage"
}
```

The exact W38 diagnostic should be checked from CI; the above numbers are only an example shape.

`logic/snapshots.py` now freezes `speed_quality` together with the pre-race prediction so later postmortems can distinguish:

- speed genuinely used
- speed disabled for sparse coverage
- speed used with neutral imputations

---

## 4. Files changed

Runtime/config:

- `config/speed_index.json`
- `logic/speed_index.py`
- `logic/build_predictions.py`
- `logic/snapshots.py`

Tests/CI:

- `tests/test_speed_guard.py` (new)
- `tests/test_build_predictions.py`
- `.github/workflows/run_pipeline.yml`
- `.github/workflows/chatgpt_speed_guard_ci.yml` (work-branch CI)

Documentation:

- this file

---

## 5. Test cases added

`tests/test_speed_guard.py` fixes three behaviors:

1. A turf prediction cannot derive speed from dirt runs when same-surface mode is enabled.
2. If qualified speed coverage is below the race threshold, speed is disabled for every horse.
3. If coverage is sufficient, missing speed is mean-imputed and its z-score is exactly zero.

`tests/test_build_predictions.py` also verifies:

- `speed_quality` is emitted.
- the sample race uses speed when quality is sufficient.
- empty base times disable speed and reduce confidence to the existing floor.

`run_pipeline.yml` now executes `tests/test_speed_guard.py` before any network calls.

---

## 6. What this branch intentionally does NOT solve

### Base-time completeness

This guard does not populate `config/base_times.json`.

The repository already has:

- `scripts/build_base_times.py`
- `scripts/fill_base_times.py`
- `.github/workflows/run_base_times.yml`

Those should continue to fill the table. As the table becomes complete, the guard should naturally allow speed in more races.

Do **not** lower the quality thresholds merely to make speed "show up" more often.

### Probability calibration

Softmax p is still uncalibrated. This branch does not address Brier/log-loss/reliability calibration.

### Pace / trip / running style

The model still lacks a proper race-shape/pace model. The All Comers result should not be explained away solely by this speed fix.

### Human-score data quality

Third-place counts are still missing in the leadership source, so the human factor can behave like a quinella rate instead of a true place rate.

### Race condition parsing

The known `note="ハンデ"` parsing error remains outside this branch.

---

## 7. Claude review checklist

Please review the implementation, not just the intent.

In particular:

- Is same-surface-only the right temporary guard while base_times is sparse?
- Is `min_usable_runs=2` too permissive or too strict?
- Is `min_race_coverage=0.5` an acceptable provisional safety floor?
- Is mean imputation appropriate, given that it guarantees z=0 neutrality?
- Could imputation inadvertently make character-specific score weights behave unexpectedly?
- Does `n_usable` still correctly feed the existing myomi confidence calculation?
- Is `speed_quality` preserved through snapshots/restoration without schema regressions?
- Does disabling speed race-wide create any hidden assumptions in card selection?
- Should base-score missing-factor handling eventually use neutral imputation for other factors too?
- Should the base-time fill workflow be accelerated for turf courses before the next live weekend?

Most importantly:

> Do not evaluate this change by whether it would have made the 2026-09-20 winner rank first.

The acceptance criterion is structural:

> A horse must not be strongly penalized merely because only an unrepresentative subset of its past runs happens to have base-time coverage.

---

## 8. Merge recommendation

Recommended sequence:

1. Review this branch and this document.
2. Inspect the branch CI, including the offline 2026-W38 diagnostic.
3. If the guard logic is accepted, merge it.
4. Continue filling `base_times.json`; do not treat the guard as a substitute for data.
5. Then proceed to probability calibration.

No production prediction/snapshot/result file is intentionally rewritten by this branch.
