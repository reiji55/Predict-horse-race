# HANDOFF — Top3 / Place partner model (2026-09-21)

## 0. Status / safety

- Repository: `reiji55/Predict-horse-race`
- Review branch: `chatgpt/top3-place-model-20260921`
- Base production commit: `5cd8b668be240d4c6f96814dd5c8c3c20a2b4944`
- Production/default branch: `claude/content-review-full-rw32o2`
- **This model change is NOT merged to production.**
- Review all commits and the full PR diff before merge.

Verified branch CI:
- GitHub Actions run: `35588224689`
- Python: **153 passed, 13 skipped**
- Top3-focused subset: **22 passed**
- Node adapter tests: success

---

## 1. Why this change exists

The 2026-09-21 Kobe Shimbun Hai exposed a model distinction that the current architecture cannot express cleanly.

Pre-race manual analysis correctly separated:

- horse 1 ロブチェン: highest outright win ability / obvious anchor
- horse 4 アルトラムス: materially underpriced win contender

Those two finished 1st and 2nd.

However horse 8 シートゥサミット, an extreme longshot, finished 3rd.
The important structural point is not that "we should have picked horse 8 after seeing the result."

The relevant pre-race property was:

> a horse can be relatively weak as a win candidate but unusually repeatable as a top-3 / place candidate under the target distance/surface.

The existing pipeline is fundamentally win-centric:

`base score -> softmax win p -> Harville -> Wide/Quinella/Trio`

Therefore Wide and Trio partner selection inherits the same ordering used for winning.

This branch separates:

- **Win Score / win p**: unchanged, still controls the axis and Quinella side.
- **Top3 Score**: new non-probabilistic ranking used only to select partners for Wide and Trio.

---

## 2. Important non-goal: Top3 Score is NOT a probability

This is deliberate.

The current win p is already known to be uncalibrated.
Creating a second uncalibrated number and calling it "P(top3)" would repeat the same mathematical mistake.

Therefore `Top3 Score`:

- is a 0-1-ish heuristic score,
- does NOT sum to anything,
- is NOT passed into market EV,
- is NOT passed into Harville,
- is NOT used by the myomi meter,
- is used only for **ranking partner candidates**.

Existing win p continues to calculate current hit_pct / approximate payouts / market card EV.

Calibration of true P(top3) is a future statistical task.

---

## 3. New module: logic/top3_score.py

For each usable historical run on the same surface:

```text
top3 = 1 if finish <= 3 else 0
perf = (heads - finish + 0.5) / heads

run_value =
    w_top3 * top3
  + w_perf * perf
```

Default:

```json
"w_top3": 0.7,
"w_perf": 0.3
```

Condition weight:

```text
proximity =
  1
  + b_dist * dist_close
  + b_venue * same_venue
  + b_going * same_going

final_run_weight = recency_weight * proximity
```

Defaults:

```json
"b_dist": 0.8,
"b_venue": 0.2,
"b_going": 0.15,
"dist_tol": 600,
"recency_weights": [1.0, 0.9, 0.8, 0.7, 0.6]
```

Surface mismatch is excluded entirely.

The profile preserves audit evidence:

- `top3_raw`
- `top3_n_usable`
- `top3_same_dist_runs`
- `top3_same_dist_hits`

This allows later review of whether a high Top3 rank had real same-distance evidence or was based on weaker nearby-distance evidence.

---

## 4. Win model remains unchanged

`logic/build_predictions.py` still computes:

- speed_raw
- aptitude_raw
- human_raw
- base_score
- score
- win p
- q
- myomi

exactly through the existing path.

Top3 is calculated alongside these fields but is NOT included in `base_score.composite_scores`.

This separation is intentional:

> do not improve Wide by silently changing the win prediction.

The earlier speed-coverage guard remains active and independent.

---

## 5. Partner roles by character

New config:

```json
"place_partner_mode":
  kei   -> "insurance"
  tetsu -> "balanced"
  gen   -> "edge"
  otori -> "edge"
```

### Kei: insurance

Uses:

- Top3 suitability: 80%
- market popularity / shorter odds: 20%

Purpose:

A Wide partner should be a repeatable place candidate and can deliberately be conservative.

This preserves the role of bets such as a low-odds anchor Wide as bankroll insurance.

### Tetsu: balanced

Uses:

- Top3 suitability: 100%
- market component: 0%

Purpose:

Select the strongest place candidates without deliberately chasing or avoiding price.

### Gen / Otori: edge

Uses:

- Top3 suitability: 65%
- market unpopularity / longer odds: 35%

Purpose:

When two horses have similar place suitability, prefer the less popular / higher-priced one.

Important:

This is NOT an expected-value formula.
It only changes ordering.

A 100x horse with poor Top3 suitability must not outrank a strong place candidate just because it is 100x.

Tests explicitly fix this behavior.

---

## 6. Why raw Top3 score is used instead of Top3 rank in the blend

An early implementation used rank-normalized Top3 position.

That makes #1 vs #2 look like a huge difference even if their raw scores are nearly identical.

The branch was corrected before handoff.

Top3 raw scores are min-max normalized within the candidate pool:

```text
top3_norm = (raw - min) / (max - min)
```

The market component remains rank-normalized.

This allows the intended behavior:

- candidate A: Top3 0.85, odds 3x
- candidate B: Top3 0.80, odds 30x

Under `edge`, candidate B can move above A because place suitability is close and market price is dramatically different.

But a horse with Top3 0.40 does not jump to the top solely because it is 100x.

---

## 7. Which ticket types change

This is intentionally asymmetric.

### Quinella / 馬連

**No Top3 partner substitution.**

It continues to use the existing Win-side character ordering.

Reason:

1st/2nd exact participation is closer to win strength; this branch is specifically about place-oriented tickets.

### Wide / ワイド

Axis remains the existing Win-side axis.

Partners are replaced from the character's Top3 partner pool.

### Trio / 3連複

Axis remains the existing Win-side axis.

The other positions are replaced from the Top3 partner pool.

This creates the desired structure:

> strong / underpriced Win axis
> ×
> horses with repeatable Top3 suitability

---

## 8. Template mechanics were preserved

Existing bankroll templates and 100-yen unit rules were not redesigned.

Example Kei template remains conceptually:

- Wide axis-partner1 200
- Wide axis-partner2 100
- Wide partner1-partner2 100
- Quinella axis-winPartner1 100

But Wide partner1/partner2 now come from `insurance` Top3 ordering.

The Quinella partner remains from existing Win ordering.

Gen's 200-yen Wide becomes an `edge` Wide because its partner comes from the edge Top3 pool.

Therefore the user observation from 2026-09-21 is addressed without creating a new portfolio optimizer yet:

- Kei is allowed to hold a low-return insurance Wide.
- Gen uses its Wide budget on a higher-price place candidate.

---

## 9. No portfolio-level de-duplication yet

This branch does NOT solve total 1500-yen / multi-character portfolio overlap.

For example, it does not yet say:

> if Kei already bought a low-odds Wide, no other character may spend on the same pair.

That is a separate portfolio optimization problem and should be evaluated after this partner model produces prospective results.

Do not add cross-character restrictions during review unless there is a clear mathematical reason.

---

## 10. Audit data / provenance

`marks[]` now stores:

- `top3_score`
- `top3_rank`
- `top3_same_dist_runs`
- `top3_same_dist_hits`

Each card stores:

- `place_partner_mode`
- `model_version: "top3-partner-v1"`

`results/build_results.py::settle_card` now preserves:

- objective
- place_partner_mode
- model_version
- probability_model

This is essential.

Without this, future ROI cannot be segmented by the partner model that generated the historical card.

---

## 11. Tests added

New:

`tests/test_top3_score.py`

It verifies:

1. repeat same-distance place form outranks a flashy but inconsistent shorter-distance profile;
2. other-surface runs are excluded;
3. insurance and edge modes choose different partners;
4. edge does not promote an extreme longshot whose Top3 suitability is poor;
5. Wide and Trio use Top3 partners;
6. Quinella stays on Win-side ordering;
7. missing Top3 data falls back to the old template order.

Updated:

`tests/test_build_results.py`

Verifies Top3/model provenance survives settlement.

Updated:

`.github/workflows/run_pipeline.yml`

Runs `tests/test_top3_score.py` before live scraping.

Branch-only CI:

`.github/workflows/chatgpt_top3_model_ci.yml`

Verified run `35588224689`:

- 153 passed
- 13 skipped
- 22 focused passed
- Node adapter tests success

---

## 12. Files changed in this model branch

Core:

- `config/cards.json`
- `logic/top3_score.py` (new)
- `logic/cards.py`
- `logic/build_predictions.py`
- `results/build_results.py`

Tests / CI:

- `tests/test_top3_score.py` (new)
- `tests/test_build_results.py`
- `.github/workflows/run_pipeline.yml`
- `.github/workflows/chatgpt_top3_model_ci.yml`

Documentation:

- this file

The branch is based on production commit `5cd8b668...`, which already includes the special 2026-09-21 result / 2026-09-22 Nakayama display update. Those base changes are not part of the model PR diff.

---

## 13. What Claude should review carefully

Please review every changed file and full PR diff.

Specific questions:

1. Is the Top3 run-value formula reasonable as a ranking heuristic?
2. Should `w_top3=0.7 / w_perf=0.3` remain provisional?
3. Is `dist_tol=600` too broad for distance suitability?
4. Should same-distance evidence receive an explicit discrete bonus beyond continuous `dist_close`?
5. Is same-surface-only correct for Top3?
6. Is insurance = 80% Top3 + 20% short-price sensible?
7. Is edge = 65% Top3 + 35% long-price too aggressive?
8. Does min-max normalization introduce instability in very small fields?
9. Is replacing only Wide/Trio partners while preserving the Win axis internally coherent?
10. Are any character-specific `sel_*` mutations leaking between card generations?
11. Does market EV evaluation remain internally coherent after partner substitution?
12. Are hit_pct / Harville numbers misleading because the chosen partners came from a separate Top3 model but probability still comes from win p?
13. Should `hit_pct` eventually be explicitly labeled "win-p Harville estimate" until place calibration exists?
14. Does result provenance preserve enough information for prospective comparison?
15. Are there any UI/schema assumptions broken by extra mark/card fields?

---

## 14. Important statistical warning

Do NOT approve this because it appears likely to have captured horse 8 in the already-finished Kobe Shimbun Hai.

That race motivated the feature distinction, so it is training / idea-generation data.

The new model must be evaluated prospectively on future races.

Acceptance criterion:

> before seeing the outcome, does the model identify long-priced horses with genuinely strong repeated place evidence, and do Wide/Trio results improve out-of-sample?

Do not tune these weights against the 2026-09-21 finish.

---

## 15. Remaining major work after this review

Still unresolved:

1. calibrated win probabilities
2. true calibrated P(top3)
3. pace / running-style / trip model
4. full base-time coverage
5. human-score third-place data quality
6. long-run walk-forward testing
7. portfolio-level stake optimization / overlap control
8. CLV tracking

Suggested order after this branch:

1. review/merge or revise Top3 partner model
2. collect prospective results
3. probability calibration
4. only then use true probability × actual Wide/Trio odds for stake optimization
