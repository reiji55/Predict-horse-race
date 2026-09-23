# ChatGPT handoff — reliability hardening (2026-09-23)

## Review status

**Do not merge yet. Claude review requested before production integration.**

Branch:
`chatgpt/reliability-hardening-20260923`

Base:
`e5908524a94fa7c4cb58e8855b4a0005ec4b2818`
(current default/production branch: `claude/content-review-full-rw32o2`)

## Why this change

The prediction logic already has strong fail-safe behavior (speed guard, pre-race snapshots,
manual-card timestamp validation, Champion/Challenger shadow evaluation), but three operational
gaps remained:

1. `config_hash` did not include `base_times.json`, even though base times directly change the speed score.
2. There was no normal push/PR CI covering both Python and the JavaScript UI adapter tests.
3. Scraper/prediction stages intentionally continue after individual failures, so a workflow could look successful
   while silently producing incomplete production data.

## Changes

### 1. Reproducibility fingerprint includes base times

`logic/model_registry.py`

- Added `base_times.json` to `HASH_CONFIGS`.
- Historical snapshots can now distinguish runs whose code/model IDs are identical but whose base-time table differs.
- Added a regression test that changes only `base_times.json` and verifies the hash changes.

### 2. Scraper collection completeness is recorded

`scraper/build_raw.py`

Each run now writes top-level `collection_report`:

- `requested_dates`
- `selected` — races selected by the main-race rule
- `built` — races successfully built
- `failed` — race-list or race-build failures

This report describes **the current fetch run**, while `raw.races` still contains the merged week history.

### 3. Data-quality report + production gate

New:
`logic/data_quality.py`
`tests/test_data_quality.py`

The report compares `raw/{week}.json` with `data/predictions.json` and writes
`data/quality_report.json`.

Critical (production push is blocked):
- explicit scraper collection failure
- selected race missing from the built set
- raw race missing from predictions
- zero runners
- empty marks/cards
- zero past-run coverage
- zero win-odds coverage

Warning only (fallback is designed to remain safe):
- partial past-run / win-odds coverage
- missing jockey/trainer statistics
- incomplete combination odds
- speed guard disabling the speed factor

Current partial-coverage warning threshold: **80%**.

`.github/workflows/run_pipeline.yml` now:
- fails if the raw file is not produced
- runs the data-quality gate before committing generated production data
- therefore does not push a critically incomplete prediction set

### 4. Normal CI for every push / PR

New:
`.github/workflows/ci.yml`

Runs:
- Python 3.12
- `python -m pytest -q`
- Node 22
- `node tests/test_adapter.js`

Also uses CI concurrency cancellation for obsolete runs on the same ref.

## Test result on this branch

GitHub Actions CI run:
- Python: **184 passed, 13 skipped**
- UI adapter: **15 passed**
- Job conclusion: **success**

Run ID: `35855772825`

## Points for Claude review

Please review especially:

1. Should `base_times.json` remain inside the shared `config_hash`, or should it get a dedicated
   `base_times_hash` field for clearer attribution?
2. Are the quality-gate severities reasonable?
   - zero past-runs / zero odds = critical
   - partial coverage below 80% = warning
   - D/E and combo-odds loss = warning
3. Is adding `collection_report` to the raw top-level schema acceptable, or should operational metadata live elsewhere?
4. Is blocking the production commit **before push** the desired behavior for critical quality failures?
5. Any backward-compatibility issues with old raw files that do not contain `collection_report`?
6. CI currently runs on all pushes including data-only automated commits. Keep for simplicity, or add path filtering?

No prediction weights, horse selection logic, payout calculation, snapshot semantics, or UI behavior were intentionally changed.
