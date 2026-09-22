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

# Otori is no longer a fixed-character Top3 mode.
# It is Chappy's high-conviction state; see section 17.
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

### Gen: edge

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


---

# 16. Champion / Challenger shadow operation (added 2026-09-22)

The user explicitly adopted a Champion/Challenger workflow so future model changes can be rolled back based on same-race evidence rather than memory or a few recent outcomes.

This PR now includes that infrastructure.

## Registry

`config/models.json` is the operational source of truth.

Current intended state:

```text
Champion   = win-v1-speed-guard
Challenger = top3-partner-v1
```

Champion has `use_top3_partner=false`.
Challenger has `use_top3_partner=true`.

Therefore merging this PR **does not immediately replace the production prediction method**.

The UI-facing `data/predictions.json` remains the Champion.

## Same-run generation

`logic.build_predictions.main()` captures one `run_now` and one raw input, then creates:

Champion:
- `data/predictions.json`
- `data/snapshots/{race_id}.json`

Challenger:
- `data/challengers/top3-partner-v1/predictions.json`
- `data/challengers/top3-partner-v1/snapshots/{race_id}.json`

Both are frozen against the same `run_now`, which avoids a comparison where one model accidentally sees later odds.

Challenger predictions are not shown in the UI.

## Reproducibility fields

Every generated prediction/race records:

- `model_id`
- `model_role`
- `git_commit`
- `config_hash`

Each card records:
- `model_version`
- `model_role`
- objective
- place_partner_mode
- probability_model

`config_hash` is SHA-256 over:
- cards.json
- chappy.json
- myomi.json
- speed_index.json
- models.json

The hash is intentionally short-displayed (first 16 hex chars) but deterministic.

## Shadow settlement

After the normal results job builds `data/results.json`, it runs:

`python -m results.build_shadow_results`

This settles every enabled Challenger against the exact same `data/race_results.json`.

Outputs:

- `data/challengers/{model_id}/results.json`
- `data/model_comparison.json`

## Apples-to-apples comparison

`results/model_compare.py` compares only the **intersection of race_ids** available to Champion and each Challenger.

Example:

```text
Champion history   100 races
Challenger history  20 races
comparison          20 common races
```

The prior 80 Champion races do not enter the head-to-head.

`manual_chat` special records are excluded.

Comparison includes:
- races
- cards
- hits
- spend
- payout
- balance
- ROI
- card hit rate
- by-character stats
- per-race balance delta

## Promotion and rollback

See:
- `docs/MODEL_HISTORY.md`
- `docs/MODEL_OPERATIONS.md`

Promotion is intended to be config-first:
1. evaluate common-race evidence
2. change `config/models.json`
3. move challenger spec to Champion
4. keep old Champion available as Challenger/Archived
5. update MODEL_HISTORY
6. PR + tests + review

Rollback can therefore often be a small config restoration.
If implementation code itself is wrong, use the recorded Git commit and Git revert.

## Additional files added for this infrastructure

- `config/models.json`
- `logic/model_registry.py`
- `results/build_shadow_results.py`
- `results/model_compare.py`
- `tests/test_model_registry.py`
- `tests/test_model_compare.py`
- `docs/MODEL_HISTORY.md`
- `docs/MODEL_OPERATIONS.md`

Also changed:
- `logic/build_predictions.py`
- `logic/cards.py`
- `logic/snapshots.py`
- `results/build_results.py`
- `.github/workflows/run_pipeline.yml`
- `.github/workflows/run_results.yml`
- `tests/test_build_predictions.py`

## Verified latest CI

Run `35635019385`:
- Python: **159 passed, 13 skipped**
- focused Top3/build/results tests: **23 passed**
- Node adapter tests: success

An earlier red run was caused only by the existing top-level schema test not yet allowing the new `model` metadata field; the test contract was updated and the latest run is green.

## Extra Claude review checklist for shadow operation

16. Does using one captured `run_now` genuinely prevent timing advantage between Champion and Challenger?
17. Can Champion/Challenger snapshot directories ever overwrite each other?
18. Is `config_hash` sufficient to distinguish relevant runtime settings?
19. Should Git SHA fallback be stronger than `unknown` outside GitHub Actions?
20. Does common-race intersection prevent survivorship / history-window bias adequately?
21. Should comparison also require matching odds/frozen timestamps explicitly?
22. Could a Challenger build failure silently reduce the intersection and make results look better?
23. Should missing Challenger races be reported as a coverage metric / failure count?
24. Is config-only promotion safe given both model paths remain in the same codebase?
25. Should old Champion remain shadow-running for a mandatory probation period after promotion?

Please review these operational risks before approving merge.


---

## 16. Champion / Challenger shadow-evaluation architecture

This branch now also contains the infrastructure requested after the initial Top3 implementation.

### Registry

`config/models.json`

Current roles:

- Champion: `win-v1-speed-guard`
  - `use_top3_partner=false`
  - this remains the only model written to `data/predictions.json` and shown in the UI.
- Challenger: `top3-partner-v1`
  - `use_top3_partner=true`
  - hidden from the UI and run only for prospective comparison.

**Bug fixes are not registered as Challengers.**
A bug/data-quality fix is shared by Champion and Challenger and therefore changes their common baseline, not the experimental variant.

### Same-run generation

`logic/build_predictions.py::main` loads raw/config/base-times once and uses the same `run_now` / `generated_at`.

It then builds:

Champion:
- `data/predictions.json`
- `data/snapshots/{race_id}.json`

Challenger:
- `data/challengers/top3-partner-v1/predictions.json`
- `data/challengers/top3-partner-v1/snapshots/{race_id}.json`

This is intentional: the comparison should differ by model variant, not by odds timestamp or raw-data refresh.

### Reproducibility metadata

Each race/snapshot records:

- `model_id`
- `model_role`
- `git_commit`
- `config_hash`

`logic/model_registry.py` hashes the prediction-affecting configs, including `models.json`.

### Same-result settlement

`results/build_shadow_results.py` takes the already-fetched official `data/race_results.json`.

It grades each enabled Challenger using **its own pre-race snapshots**, then writes:

- `data/challengers/{model_id}/results.json`

No second result fetch is needed and no post-race prediction is regenerated.

`.github/workflows/run_results.yml` runs this after the normal Champion `results.json` build.

### Apples-to-apples comparison

`results/model_compare.py` produces:

- `data/model_comparison.json`

For each Challenger, it takes the **intersection of race_id values** present in Champion and Challenger results.

Therefore:
- old Champion-only races from before Challenger launch are excluded;
- a race missing a valid pre-race snapshot on either side is excluded;
- `evaluation_scope=manual_chat` is excluded;
- both sides are evaluated on identical official race results.

It records:
- common race IDs / count
- spent / payout / balance / ROI
- card hit rate
- by-character results
- race-by-race balance delta

### Promotion / rollback

Human-readable history and rollback instructions live in:

`docs/MODEL_HISTORY.md`

Promotion should normally be a registry-role change, not deletion of the old model:

- promote `top3-partner-v1` to Champion;
- retain `win-v1-speed-guard` as a shadow Challenger for a period.

If the new Champion later degrades, swap the roles back.

Only revert code commits when the implementation itself is wrong; ordinary model preference changes should be handled through the registry.

### Do not prematurely promote

No fixed minimum sample size has been hard-coded yet.

That is deliberate. With rare large payouts, a single race can dominate ROI.
Collect paired prospective races first, then define promotion criteria **before** inspecting the deciding sample.

---

## 17. Additional files introduced for Champion / Challenger

- `config/models.json`
- `logic/model_registry.py`
- `results/model_compare.py`
- `results/build_shadow_results.py`
- `tests/test_model_registry.py`
- `tests/test_model_compare.py`
- `docs/MODEL_HISTORY.md`
- `.github/workflows/run_results.yml` (shadow settlement step)
- `logic/snapshots.py` (model identity frozen)
- `results/build_results.py` (model identity preserved)
- `tests/test_build_predictions.py` (model metadata contract)

Claude review must include these files as part of PR #3, not only the original Top3 scoring files.


---

# 17. Chappy 1000-yen integration layer + new Otori semantics (added 2026-09-22)

This section **supersedes any earlier wording in this document that described Otori as an independent fixed character.**

## Why this was added

On 2026-09-22, before the JRA Anniversary Stakes, ChatGPT manually applied the new model's ideas but did not simply execute the fixed Top3 formula.

The reasoning separated:

- Win anchor
- Top3 value anchor
- ability-side support
- higher-price edge candidates
- insurance stake
- edge-wide stake
- trio expansion made possible by a 1000-yen budget

The pre-race manual card centered on horse 14 as the Top3/value signal; the actual finish was 14 -> 3 -> 12 and several proposed combinations hit.

This is a **strong initial product/model-design signal**, not validation:
that race directly motivated this implementation and must not be counted as prospective evidence for Chappy/Top3 effectiveness.

## Chappy is not a fourth fixed-weight model

Chappy is an integration layer implemented in:

- `logic/chappy.py`
- `config/chappy.json`

It combines:

- Win score
- Top3 score
- same-course / same-distance repeatability
- recent same-surface form
- market unpopularity
- current model-vs-market value signal
- data quality

The default weights are not claimed to be statistically optimal.

### Dynamic condition boost

A horse gets a per-horse condition-weight boost only when:

- same course + same distance evidence has at least 3 runs, and
- Top3 rate in that exact condition is at least 75%.

When this fires, condition weight rises and win/recent weights are reduced.

This is intended to represent the kind of signal that made the 2026-09-22 horse 14 interesting:
repeated, highly specific condition success can deserve more attention than a fixed global weight would allow.

Claude must review whether this thresholding is too brittle or too easy to overfit.

## Role assignment

Chappy picks four distinct roles:

- `win_anchor`
- `support`
- `top3_edge`
- `long_edge`

Each role has its own signal blend.

The resulting default 1000-yen portfolio contains:

- one 200-yen core Wide
- insurance Wide
- additional Edge Wide(s)
- one Quinella
- several Trio combinations

All stakes remain 100-yen executable units.

The intent is explicitly different from merely doubling a 500-yen card:
the extra 500 yen buys **coverage of additional asymmetric outcomes**, not only more stake on the safest pair.

## Manual ChatGPT override

Automatic Chappy output can be overridden by a pre-race file:

`data/chappy_manual/{race_id}.json`

Specification:

`docs/CHAPPY_MANUAL_OVERRIDE.md`

This exists because some context-sensitive reasoning is difficult to reduce immediately to a fixed formula.

Rules:

- override must be created before post time;
- it must total exactly 1000 yen;
- it is frozen like every other prediction;
- `source=manual_chat` is preserved in results;
- retroactive post-result overrides are prohibited.

The runtime directory contains a README warning against post-hoc insertion.

## Otori is now Chappy's high-conviction state

There is only one integration-layer slot per race:

- normal state: `char = chappy`
- high-conviction state: `char = otori`

**There must never be both Chappy and Otori cards in the same race.**

The old independent 500-yen Otori generation path is removed from `build_predictions.py`.

Current Otori gate requires all of:

1. minimum displayed myomi
2. minimum Chappy conviction
3. minimum data quality
4. minimum **uncalibrated** hit-rate proxy
5. complete market odds for every ticket on the card
6. market-EV sign/veto condition (expected ROI must not be below 1.0)

The EV magnitude is still not trusted before probability calibration.

If the gate passes, the automatic Chappy portfolio becomes slightly more concentrated by shifting 100 yen from insurance to the core edge Wide.

Claude should review whether any concentration change is justified before calibration.

## Audit trail

Each Chappy/Otori card preserves:

- `source` (signal_engine / manual_chat)
- `portfolio_style`
- `conviction`
- `decision_log`
- signal board for every horse
- role assignments
- dynamic weights
- exact-condition evidence
- Otori gate checks
- model / config provenance already used by the broader pipeline

The pre-race race snapshot also freezes `chappy_decision`.

Settled results preserve the decision log.

## Fixed-model A/B excludes Chappy/Otori

`results/model_compare.py` now explicitly compares only:

- kei
- tetsu
- gen

Chappy/Otori is a separate experimental track.

Reason:
the same integration overlay exists around both fixed-model variants and would dilute the Champion-vs-Challenger comparison.

## UI

The UI adds:

- Chappy as a 1000-yen predictor
- a monochrome six-loop knot-style inline mark reminiscent of the familiar ChatGPT visual language, implemented locally as SVG with no external asset dependency
- a distinct dark card
- "参考的中率" rather than implying calibrated probability
- Chappy in the predictor directory
- Otori replacing Chappy rather than appearing alongside it

The race list also retains the production "newest date first" ordering.

## Additional files / changes for Chappy

Added:
- `config/chappy.json`
- `logic/chappy.py`
- `tests/test_chappy.py`
- `docs/CHAPPY_MANUAL_OVERRIDE.md`
- `data/chappy_manual/README.md`

Updated:
- `logic/build_predictions.py`
- `logic/model_registry.py`
- `logic/snapshots.py`
- `results/build_results.py`
- `results/model_compare.py`
- `docs/ui/adapter.js`
- `docs/ui/keiba-3cards-mock-v7.html`
- relevant tests / CI
- MODEL_HISTORY / MODEL_OPERATIONS

## Additional Claude review questions

26. Is Chappy genuinely adding a useful integration layer, or just hidden hand-tuned complexity?
27. Are dynamic per-horse weight shifts principled enough to keep, or should they only be logged first?
28. Is exact course+distance 3 runs / 75% Top3 too strong a trigger for condition boosting?
29. Does market-unpopularity enter too strongly in `top3_edge` / `long_edge`?
30. Is the 1000-yen portfolio construction sufficiently diversified rather than simply higher variance?
31. Should manual overrides be permitted in official performance statistics, or reported in a separate track?
32. Is the manual override audit trail sufficient to prove it was created pre-race?
33. Should manual Chappy cards ever be allowed to trigger Otori, or should Otori be auto-only?
34. Is the current conviction formula meaningful enough to gate Otori?
35. Should incomplete combo odds always close Otori even when all other signals are very strong?
36. Is shifting 100 yen from insurance to the core Wide on Otori promotion defensible pre-calibration?
37. Are Chappy and Otori correctly excluded from fixed-model Champion/Challenger comparisons everywhere?
38. Are Chappy result statistics separately recoverable by source / portfolio_style / conviction?
39. Does the UI clearly communicate 500-yen fixed predictors vs 1000-yen Chappy/Otori?
40. Does the knot-style Chappy icon avoid external runtime dependencies and render correctly in the PWA?
41. Are there any post-race leakage paths through `data/chappy_manual` that need a stronger automated guard?

## Merge recommendation

Do not merge solely because the 2026-09-22 manual card hit.

Review this as two independent hypotheses:

A. Top3 partner Challenger is a better fixed-model variant.
B. Chappy dynamic integration is useful as a separate 1000-yen portfolio layer.

Both should be logged prospectively after merge.

Probability calibration remains the next major statistical priority.
