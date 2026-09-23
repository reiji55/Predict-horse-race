# ChatGPT handoff — race regime / formal abstention (2026-09-24)

## Review status

**Claude review requested before production integration. Do not merge blindly.**

Branch:
`chatgpt/race-regime-abstain-20260924`

Base:
`afca048b7a09ceb58347bffad129d9d607bb77c2`
(PR #4 merged production HEAD)

## User decision behind this change

The user accepted the proposal to stop treating every 11R as automatically worth betting.

Key product/model intent:

- 11R remains the discovery universe. "11R" itself is **not** assumed to mean an upset-prone race.
- A race can be SOLID / NEUTRAL / OPEN based only on information available before post time.
- Gen is the longshot/chaos specialist. On a clearly SOLID race he may explicitly **PASS** instead of inventing a longshot.
- Kei stays active on SOLID races; that is the environment where a conservative view may be most useful.
- Tetsu is intentionally unchanged for now. The user has not decided his final role, and keeping him active provides a useful control.
- Automatic Chappy must not force a low-quality longshot merely because its portfolio has a `long_edge` slot.
- Manual pre-race Chappy ("本線モデル", source=manual_chat) remains unconstrained by the automatic regime policy. It may pick favourites in a solid race if the full analysis supports them.

## Important semantic rule

`race_regime-v1` is **not an "upset prediction" model**.

It describes the pre-race market/model state:

- `solid`: market support is concentrated near the top AND the Win model broadly agrees
- `open`: the market is dispersed and/or the model strongly disagrees
- `neutral`: mixed state
- `unknown`: insufficient paired p/q data

Actual race outcome must never be used to assign the label.

## 1. New pre-race regime classifier

New:
- `config/race_regime.json`
- `logic/race_regime.py`
- `tests/test_race_regime.py`

Signals:

1. market top-3 support share
2. normalized market entropy
3. total-variation distance between model p and market q
4. overlap between model top-3 and market top-3

Provisional v1 SOLID thresholds:

- market top-3 share >= 0.62
- normalized entropy <= 0.82
- model-market TV <= 0.16
- top-3 overlap >= 2

All SOLID checks must pass.

OPEN uses three diagnostic checks and requires at least two:
- market top-3 share <= 0.45
- entropy >= 0.90
- model-market TV >= 0.24

These thresholds are intentionally provisional. They are stored in config and the entire regime output is snapshotted so they can be evaluated rather than retrofitted.

The W38 frozen examples were checked before choosing the initial ranges. The clearly dispersed Dotonbori Stakes is OPEN-like; the other early races do not get falsely declared SOLID merely because the market had a few short prices.

## 2. New shadow challenger

`config/models.json` adds:

`race-regime-abstain-v1`

- role=challenger
- use_top3_partner=false
- use_race_regime=true

Champion behaviour is **not changed by the policy**.
Champion does record `race_regime` as diagnostic metadata so future live samples accumulate from the same clock-time data.

## 3. Gen can formally PASS

New helper:
`cards.generate_abstain_card(...)`

A pass is stored as a real prediction decision:

- `action: "pass"`
- `total: 0`
- `budget: 500` (what Gen normally would have risked)
- `bets: []`
- `abstain_reason: "race_regime:solid"`
- Gen-style comment:
  "今日は人気どころが素直に強そうだな。こういうガチガチの勝負は俺の出番じゃねぇ。勝負したいレースじゃないから見送るぜ。"

Why store it instead of omitting the card:
- omission cannot be evaluated later
- a formal 0-yen decision lets us compare the same race against Champion Gen's normal 500-yen card

PASS must **not** count as a miss.

Result/model comparison now tracks:
- opportunities
- purchased cards
- passes
- hits
- spent/payout/ROI

and keeps regime-specific aggregates.

## 4. Chappy: no forced longshot on SOLID auto races

`config/chappy.json` adds role score `solid_depth`.

Normal auto Chappy:
- 4th role selector remains `long_edge` (includes market-longshot signal)

SOLID auto Chappy in the regime challenger:
- keeps the 4-role portfolio shape
- but selects the 4th horse via `solid_depth`
- `solid_depth` uses win/top3/condition/recent/value and **zero explicit longshot-market weight**
- portfolio_style becomes `solid_consensus_1000`
- decision log records `role_policy.long_edge_selector = "solid_depth"`

Manual pre-race Chappy:
- exact manual bets are preserved
- regime does not rewrite its roles/bets
- decision log says `manual_unconstrained=true`

This directly addresses the prior structural problem where Chappy had to nominate a distinct `long_edge` horse even when no genuine longshot case existed.

## 5. Kei / Tetsu

- Kei: no pass policy. Keeps buying.
- Tetsu: no pass policy **on purpose**. The user's role decision is still open.
- Gen: pass_on=["solid"].

Please do not "complete the character design" by guessing a Tetsu policy during review. Keep him as the control unless a code correctness issue requires otherwise.

## 6. Result comparison

`results/build_results.py`
- preserves pass/action metadata
- PASS does not enter card-hit denominator
- reports passes

`results/model_compare.py`
- compares 0-yen passes against Champion's normal spend on the same race
- head-to-head records which characters passed
- adds Champion/Challenger aggregates by frozen pre-race regime

This is intended to answer after enough races:
- Did Gen avoiding SOLID races improve balance/ROI?
- Was Champion Gen actually profitable on the races the challenger skipped?
- Which regime favours Kei / Tetsu / Gen?

## Review focus for Claude

Please aggressively review:

1. Is the regime math implemented correctly (especially p/q re-normalisation under missing odds)?
2. Are the provisional threshold directions logically correct?
3. Is `all(solid_checks)` conservative enough for v1?
4. Is PASS represented safely through snapshots/results/model_compare?
5. Can any old code accidentally treat PASS as a miss or as a 500-yen purchase?
6. Does manual Chappy remain byte-for-byte independent of the SOLID auto policy where expected?
7. Is `solid_depth` sufficiently different from the old `long_edge` to remove forced longshot bias without simply duplicating another role?
8. Any backward-compatibility issue in old snapshot/result payloads that lack `action` and `race_regime`?
9. Please run full pytest + Node adapter tests and inspect W38 shadow output before merge.

If a substantial model-policy change is needed, report it before merging. Mechanical correctness fixes are welcome directly on this branch.

## Separate follow-up (not mixed into this PR)

We also want to start collecting **time-series odds observations**, especially nearer post time, because late market movement may carry information. That data should be collected first and only later considered as a model signal. Do not treat "late money" as informed money by assumption.
