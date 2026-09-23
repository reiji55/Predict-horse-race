# ChatGPT handoff — odds observation history (2026-09-24)

## Review status

**Data-collection PR only. No prediction signal is changed. Claude review requested before merge.**

Branch:
`chatgpt/odds-history-20260924`

Base:
`afca048b7a09ceb58347bffad129d9d607bb77c2`

This PR is intentionally separate from PR #5 (race-regime / abstention).

## Why collect this now

Research discussed with the user suggests late market movement may contain information, but we must not assume:

`late money == informed money`

without live evidence.

The correct first step is therefore to preserve pre-race odds at multiple times and evaluate them later.

## What is collected

New:
- `logic/odds_history.py`
- `scraper/capture_late_odds.py`
- `tests/test_odds_history.py`
- `.github/workflows/run_late_odds.yml`

Stored at:

`data/odds_history/{race_id}.json`

Each observation contains:
- `observed_at` — our clock time
- `source_time` — netkeiba `official_datetime` returned with the odds
- `phase` — `pipeline` or `late`
- horse number
- win odds
- popularity

The history file also keeps race/source/post metadata.

## Normal pipeline observations

`.github/workflows/run_pipeline.yml` now calls:

`python -m logic.odds_history --raw raw/{week}.json --phase pipeline`

after the raw build.

Since the normal pipeline runs around 10:00 and 13:00 JST, those observations begin the time series.

Only race_ids from the current `collection_report.built` are appended.
If the current run builds zero races, **zero old races are relabelled as a new observation**.

## Late observation

New workflow schedule:

15:10 JST (06:10 UTC), Saturday and Sunday.

It loads that week's already-committed raw file and fetches **win odds only** for today's target races.

Why win-only:
- the research question is market support movement
- no need to re-fetch 馬連/ワイド/3連複
- one odds API call per race instead of four
- existing HTTP courtesy spacing still applies

15:10 is deliberately earlier than a true "last minute" scrape:
- 11R post times differ by venue
- GitHub Actions can start late
- a late start must never create a post-race observation and call it pre-race

`capture_late_odds.py` therefore checks each race's post time and skips races where `now >= post_at`.

For typical 11R times this aims to capture roughly 15–35 minutes before post.
If later evidence says the signal is concentrated inside the final 5–10 minutes, a more precise per-race scheduler can be considered separately.

## Dedupe

An observation is considered duplicate when:
- API `source_time` is the same, AND
- the actual horse/odds payload is the same.

Different local phase/clock times alone do not create fake extra observations.

## Failure behaviour

- one venue/race failure does not erase successful observations from the others
- a race already past post time is skipped, not captured
- if there were attempted pre-race targets but **all** fetches fail, the late workflow exits non-zero
- a no-op run can safely commit nothing

## Prediction-model boundary

This PR does **not**:
- add odds movement to p
- add odds movement to myomi
- change cards
- change Chappy
- claim late movers are better horses
- change Champion/Challenger behaviour

It only starts collecting evidence.

## Claude review focus

Please review:

1. Is the pre/post boundary fail-closed enough?
2. Is `official_datetime` correctly preserved as `source_time`?
3. Any possibility of stale weekly raw being written as a fresh pipeline observation?
4. Is one win-odds API call per late race the minimal safe request pattern?
5. Is 15:10 JST a sensible first observation time given current 11R post times and Actions delay?
6. Does the scheduled workflow behave safely when there is no history directory / no changes?
7. Any race around an unusual earlier 11R post time will be skipped rather than mislabelled; confirm this is the preferred tradeoff.
8. Run full CI and the targeted odds-history tests.

If there is a major operational concern with the scheduled request timing, report it before merge.
