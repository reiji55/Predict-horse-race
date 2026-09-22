# Champion / Challenger Operations

## Purpose

モデル変更を「上書き」ではなく「比較可能な実験」にする。

- **Champion**: UIに表示し、通常成績として扱う現行モデル。
- **Challenger**: UIには表示しない。同じraw・同じ時刻・同じレースを裏で予想して凍結し、同じ結果で採点する。

設定の正本: `config/models.json`

## Current setup

Champion:
`win-v1-speed-guard`

Challenger:
`top3-partner-v1`

## Files produced

Champion:
- `data/predictions.json`
- `data/snapshots/{race_id}.json`
- `data/results.json`

Challenger:
- `data/challengers/{model_id}/predictions.json`
- `data/challengers/{model_id}/snapshots/{race_id}.json`
- `data/challengers/{model_id}/results.json`

Comparison:
- `data/model_comparison.json`

## Fair comparison rule

`data/model_comparison.json` compares only the **intersection of race IDs** available to both models.

Example:
- Champion history: 100 races
- Challenger history: 20 races
- comparison: exactly those same 20 races for both

This prevents pre-Challenger history from biasing the comparison.

Manual/chat special predictions are excluded.

## Reproducibility

Every generated race snapshot records:

- `model_id`
- `model_role`
- `git_commit`
- `config_hash`

Every card also records:
- `model_version`
- `model_role`
- objective
- place_partner_mode
- probability_model

This is enough to identify which source revision + config family generated a historical prediction.

## Promotion

Promotion should be a **config change first**, not deletion of old code.

When evidence is sufficient:

1. review `data/model_comparison.json`
2. review per-character and per-ticket behavior, not ROI alone
3. update `config/models.json`
4. move the chosen challenger spec to `champion`
5. keep the old Champion as a Challenger or Archived model for a transition period
6. update `docs/MODEL_HISTORY.md`
7. PR + tests + review

Do not delete the old implementation during promotion.

## Rollback

If a promoted model becomes clearly worse or has a structural bug:

Fast path:
1. restore previous Champion spec in `config/models.json`
2. redeploy / rerun prediction pipeline

Exact code rollback:
- use the recorded Git commit / merge commit and Git revert when the implementation itself must be removed.

Because snapshots store model identity, historical results remain attributable after rollback.

## How long to compare

There is intentionally no automatic "20 races = winner" rule yet.

Small samples are noisy, especially for high-payout betting returns.

Use:
- common race count
- ROI
- balance
- card hit rate
- by-character performance
- by-ticket-type performance
- longshot Top3 capture diagnostics

before promotion.

A future statistical layer should add confidence intervals / bootstrap or walk-forward analysis.

## What NOT to A/B

Do not shadow-test every change.

Normally A/B:
- new scoring factor
- new probability model
- new partner-selection logic
- new stake allocation strategy

Normally direct-fix after review:
- parsing bugs
- data leakage bugs
- invalid bet units
- broken timestamps
- UI-only changes


## Chappy / Otori is a separate evaluation track

Chappy is not one side of the Champion/Challenger fixed-model A/B.

- Champion/Challenger comparison: `kei/tetsu/gen` only.
- Chappy/Otori: independent 1000-yen integration-layer track.

Reason:
If the same Chappy overlay were included on both sides, it would inflate spend/payout denominators and hide the actual difference between fixed models.

Track Chappy/Otori by:
- source = signal_engine / manual_chat
- portfolio_style
- conviction
- decision_log
- card result / ROI

## Manual Chappy workflow

When a ChatGPT conversation produces a stronger contextual card than the automatic signal engine:

1. build the card **before post time**
2. save `data/chappy_manual/{race_id}.json`
3. commit before race
4. run prediction pipeline
5. verify snapshot contains `source=manual_chat`
6. do not edit after result

Spec:
`docs/CHAPPY_MANUAL_OVERRIDE.md`

## Otori semantics

Otori is no longer an independent 500-yen character.

It is the same Chappy 1000-yen slot in high-conviction state:
- normal: char=chappy
- gate passed: char=otori

There must never be both Chappy and Otori cards for the same race.

Until probability calibration is complete, the gate is provisional and conservative.
