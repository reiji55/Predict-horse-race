# ChatGPT handoff — Context Layers v1 (2026-09-24)

## Status

**Claude review requested before merge.**
Branch: `chatgpt/context-layers-20260924`
Base: `efe1baf9a03831a79a3d21e2271dc64989662c5c`

This change deliberately does **not** alter prediction scores or ticket selection.

## User intent

The long-term goal is to stop treating surprising results as unexplained "luck" when
pre-race evidence might have existed.

We are separating:

1. baseline ability,
2. day-of-race condition,
3. immediate pre-race biological/market signals,

while acknowledging that some race-course causes are inherently unavailable before post.

## Added

### Historical body weight
- C2 past-five parser now preserves body weight from Data06, e.g. `510(0)`.
- fallback horse-history parser also preserves exact weight tokens.

### Context layer engine
- `config/context_layers.json`
- `logic/context_layers.py`

Observe-only features:
- horse-specific body-weight baseline/deviation
- robust MAD z-score
- layoff/quick-return interval
- JRA track metrics
- odds movement
- paddock structured features

No score adjustment is emitted.

### Track metrics
- `scraper/fetchers/g_jra_track.py`
- `scraper/capture_track_context.py`

Low-frequency JRA official-page collection:
- turf/dirt moisture at goal / 4th corner
- cushion value only when unambiguously parsed

Date mismatch fails closed.
Ambiguous cushion reference scales must never be mistaken for actual values.

### Late body weight
- late-odds workflow re-fetches shutuba once per target race
- measured body weight goes to `data/condition_history/{race_id}/...`
- after-post observations are rejected

### Paddock contract
- `logic/paddock.py`
- shared 0..1 schema for manual / ChatGPT Vision / future licensed CV input
- pre-race time validation
- no raw video downloader/rehosting

### Independent context snapshots
- `logic/refresh_context.py`
- writes `data/context_snapshots/{race_id}/{timestamp}_{phase}.json`
- does NOT edit prediction snapshots or cards
- normal pipeline and late workflow use separate phases/files

### Research anomaly queue
- `results/anomaly_review.py`
- after results, queues top-3 finishers that were model rank >=6 OR market rank >=6
- joins only frozen pre-race context
- output: `data/research/anomaly_report.json`
- every interpretation is `hypothesis_only`

This is the first self-improvement loop, but not self-modifying weights:
miss → context bundle → repeated hypothesis → Challenger → live test → possible promotion.

## Critical review questions

1. Verify C2 historical body-weight regex does not accidentally match another Data06 token.
2. Check fallback horse-history row-wide weight matching against real samples.
3. Review body-weight profile semantics:
   - minimum history=3
   - abs deviation 4%
   - MAD robust-z 2.5
   These are diagnostic flags only, not betting thresholds.
4. Review JRA track parser against current live HTML. It must prefer null over a guessed value.
5. Confirm all track collection failures are non-blocking because feature is observe-only.
6. Check late workflow request load. Added request is one shutuba page per successfully observed target race.
7. Verify body-weight capture cannot cross post time.
8. Verify context snapshots cannot modify prediction snapshots.
9. Verify context files remain conflict-friendly under concurrent normal/late Actions.
10. Verify paddock payloads cannot enter anomaly analysis unless captured into a pre-race context snapshot.
11. Review anomaly thresholds/ranking logic and confirm it does not claim causality.
12. Confirm old raw/snapshots/results without these fields remain backward-compatible.
13. Do NOT add context_layers.json to model config_hash while mode=observe_only; it does not change bets.
14. Run full pytest + Node adapter CI.

## Known limitation / intentional boundary

Automatic paddock video analysis is not implemented here because reliable permitted video ingestion is a separate
infrastructure/rightsholder problem. The structured interface is implemented now so user-provided frames/video
or a future permitted CV service can plug in without changing prediction/result schemas.

See:
`docs/CONTEXT_LAYERS.md`


## Latest validation

- Python: **235 passed, 13 skipped**
- Node adapter: **17 passed**
- CI: success on head `891156f4f9953a91f1d46ad4d4f749308603aff0`
- Earlier CI exposed one parser-test issue: cushion value selection stopped scanning before measurement time.
  Fixed by continuing through the section after finding the selected cushion value; final CI is green.
