# Holdout protocol (written 2026-10-22 JST, before any 2024–2026 result was computed)

Data: public warehouse (netkeiba-derived) 2010-01 … 2026-10-04, JRA flat races only, final odds.

## Model (locked)
- Win probability = market-residual GBM ("M3"):
  score_i = theta·log(pi_i) + tree_i(features), p = per-race softmax(score)
  pi = normalised 1/odds (closing), theta fitted by conditional logit on the training years.
  Trees: LightGBM, custom per-race softmax objective, PARAMS_RES (lr 0.02, 15 leaves, min_data 2000, L2 50),
  early stopping on the year before the test year.
  Features: the 108 point-in-time features of features.py + log(pi) + rank of pi (same as development).
- Combinations: discounted Harville with gamma/delta fitted on development predictions (2019–2023, out-of-sample).

## Walk-forward on the holdout
- 2024: train 2011–2022, early-stop 2023, predict 2024
- 2025: train 2011–2023, early-stop 2024, predict 2025
- 2026: train 2011–2024, early-stop 2025, predict 2026-01..2026-10-04

## What is reported (all of it, win or lose)
1. Win log loss vs market (raw and theta-corrected), race-bootstrap 95% CI, per year and pooled.
2. Win bets at closing odds: EV = p·odds > 1.1 and > 1.2 — bets, hit rate, ROI, ROI without top-5 payouts.
3. Combination value rule (needs only win odds): bet when P_model(c)/Q_win(c) > 1.3 and > 1.5 with caps
   馬連<200倍, ワイド<60倍, 馬単<400倍, 三連複<1000倍, 三連単<5000倍 (implied by the win pool) — ROI with CI, by year.
4. "Accuracy": top-K hit rates (K=1,3,5,10) for 馬連 / 3連複 vs market-only probabilities.
5. Main-race (11R) subset of the above.

## Trials run on development data before locking (for multiple-testing honesty)
market power (theta), quadratic market, 24 rating configs, fundamental GBM, two-stage Benter blend,
market-residual GBM, cross-market 複勝 (Dr Z), top-K combos, value rule (5 bet types × 6 thresholds),
win EV thresholds (6), 9 feature-group ablations, linear equation variants.

## Addendum A (written 2026-10-22 08:05 JST, after the win-model holdout was reported, before any place-model holdout run)
Disclosure: the 2024–2026 win-model and Harville-combination results above were already seen when this was written.
The place models were designed and checked only on 2020–2023.
- Place models (locked): stage-2 / stage-3 market-residual GBMs, place_models.py as committed (same params as win model).
  Holdout walk-forward: 2024 / 2025 / 2026 exactly as the win model.
- Report on the holdout, whatever the result:
  1. stage-2 / stage-3 log-loss gain vs market-power Harville exponent (per year)
  2. mean LLR of realised combinations vs the combination pools (馬連, ワイド, 馬単, 三連複, 三連単)
  3. 複勝 value rule with closing place odds (lower bound): bet when P_top3(model) × place_min > 1.0 / 1.1 / 1.2
     (P_top2 for fields ≤ 7), real 複勝 payouts; same rule with market-only P_top3 as comparator
  4. the P_model/Q_win combination value rule with place models (expected to be weak; reported anyway)
