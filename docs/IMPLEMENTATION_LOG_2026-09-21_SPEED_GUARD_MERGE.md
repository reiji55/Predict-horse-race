# IMPLEMENTATION LOG — Speed guard merged (2026-09-21)

## Status

- Production/default branch: `claude/content-review-full-rw32o2`
- Source PR: #2 `Speed guard: prevent sparse base-time coverage from distorting rankings`
- Merge commit: `ce1afd0fef36ef5cb646982086db148d971de716`
- Source work branch: `chatgpt/fix-speed-coverage-20260920`
- Detailed design/review handoff:
  `docs/HANDOFF_CHATGPT_2026-09-20_SPEED_GUARD.md`

This file is a post-merge index for Claude or another reviewer. The detailed reasoning remains in the handoff above.

## What is now live

### 1. Same-surface speed evidence

Prediction-time speed uses past runs from the same surface as today's race when
`config/speed_index.json.guard.same_surface_only=true`.

Purpose: while `base_times.json` is incomplete, do not let a turf horse be strongly judged by only the dirt runs that happen to have base-time coverage.

### 2. Minimum usable speed history

`guard.min_usable_runs=2`.

A horse with fewer than two usable same-surface speed runs is treated as speed-missing for the composite score.

This threshold is a provisional safety floor, not an optimized betting parameter.

### 3. Race-level speed coverage gate

`guard.min_race_coverage=0.5`.

If fewer than half of the runners have enough usable speed history, factor ① speed is disabled for the entire race.

Purpose: avoid asymmetric scoring where some horses get a strong speed penalty while others have speed omitted and their remaining factors renormalized.

### 4. Neutral imputation when coverage is sufficient

`guard.neutral_impute_missing=true`.

If race coverage is sufficient but some horses still lack speed, their `speed_raw` is filled with the mean of the qualified observed values.

Because the factor is z-standardized race-wide, this means an imputed horse receives `z_speed=0`: neutral rather than rewarded or penalized.

The horse remains `uncertain=true`.

### 5. Diagnostics

Each prediction now exposes `speed_quality`, including:

- raw_available_horses
- qualified_horses
- total_horses
- coverage
- min_usable_runs
- min_race_coverage
- same_surface_only
- used
- imputed_horses
- reason

`logic/snapshots.py` freezes this diagnostic with the pre-race prediction.

## Regression that motivated this

2026-09-20 All Comers:

- Production model gave horse 4 ヴーレヴー score 63.8 and left it unmarked.
- Relevant recent turf runs could not be speed-indexed because those turf course/distance base times were absent.
- Poor dirt runs happened to be indexable and were therefore the only speed evidence.
- Horses with zero usable speed evidence were not penalized equivalently.

Offline rebuild with this guard:

- only 1/13 runners qualified for speed
- race coverage = 0.0769
- speed was disabled race-wide
- ヴーレヴー moved to score 71.9 / mark ✕
- horses 8 and 1 did not simply jump to the top, so the change was not designed to fit the observed 1-2-3 finish

## Verification before merge

Verified work-branch CI run: `35511988028`

- Python: 147 passed, 13 skipped
- Node adapter tests: success
- W38 offline diagnostic: success

Additional work-branch CI runs after documentation changes also passed.

## Files changed by PR #2

Runtime/config:

- `config/speed_index.json`
- `logic/speed_index.py`
- `logic/build_predictions.py`
- `logic/snapshots.py`

Tests/workflows:

- `tests/test_speed_guard.py`
- `tests/test_build_predictions.py`
- `.github/workflows/run_pipeline.yml`
- `.github/workflows/chatgpt_speed_guard_ci.yml`

Documentation:

- `docs/HANDOFF_CHATGPT_2026-09-20_SPEED_GUARD.md`

## Still intentionally unresolved

This merge does NOT claim to solve:

1. probability calibration of softmax p
2. pace / trip / race-shape modeling
3. incomplete base-times coverage itself
4. human-score third-place-data quality
5. known race-condition parsing errors
6. long-run walk-forward validation
7. optimal bankroll / ticket allocation

## Review request for Claude

Please review the merged implementation at and after merge commit
`ce1afd0fef36ef5cb646982086db148d971de716`.

Do not judge the guard by whether it would have perfectly predicted the 2026-09-20 or 2026-09-21 result.
The structural acceptance criterion is:

> incomplete base-time coverage must not cause a horse to be strongly penalized merely because only an unrepresentative subset of its past runs was indexable.

Please also assess whether the provisional values `min_usable_runs=2` and
`min_race_coverage=0.5` should remain until enough out-of-sample data is available.
