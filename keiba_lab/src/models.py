"""Walk-forward search for a win-probability formula that beats the market.

For each test year Y:  train on 2011..Y-2, early-stop on Y-1, predict Y (never seen).
Models
  M0   market:   p = normalised 1/odds
  M0t  market^theta (longshot-bias power, theta fitted on train years)
  M1   fundamental GBM (conditional-logit objective, NO odds)  -> f
  M2   two-stage:  c ∝ exp(a·log f + b·log pi)  (a, b fitted on year Y-1, Benter 1994)
  M3   market-residual GBM: score = theta·log pi + tree(features incl. odds)  (learns only departures from the market)
Metric: mean per-race log loss; difference to M0 / M0t with race-bootstrap 95% CI.
"""
import json
import sys
import time

import lightgbm as lgb
import numpy as np
import pandas as pd

from common import D, OUT, race_index, group_softmax, race_logloss, fit_clogit, bootstrap_mean_ci

FEAT_FILE = sys.argv[1] if len(sys.argv) > 1 else f'{D}/features.parquet'
TEST_YEARS = [int(x) for x in (sys.argv[2].split(',') if len(sys.argv) > 2 else ['2019', '2020', '2021', '2022', '2023'])]
TAG = sys.argv[3] if len(sys.argv) > 3 else 'dev'

F = pd.read_parquet(FEAT_FILE)
F = F[~F.dead_heat_win & (F.year >= 2011)].copy()
F = F.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
F['log_pi'] = np.log(F.mkt_p)
F['pi_rank'] = F.groupby('race_id').mkt_p.rank(ascending=False, pct=True)
F['handicap'] = F.handicap.astype(int)
F['female_only'] = F.female_only.astype(int)
F['listed'] = F.listed.astype(int)

DROP = {'race_id', 'horse_number', 'date', 'year', 'start_min', 'venue_name', 'surf', 'sex', 'jockey_id', 'trainer_id',
        'finish', 'completed', 'odds', 'popularity', 'mkt_p', 'dead_heat_win', 'horse_id', 'y', 'log_pi', 'pi_rank'}
MARKET_LIKE = {'last_logodds', 'last_pop_pct', 'logodds_mean5', 'logodds_mean5_rel', 'logodds_mean5_rk',
               'mkt_beat_mean5', 'last_mkt_beat', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk'}
FUND = [c for c in F.columns if c not in DROP and F[c].dtype != object]
MKT_FEATS = FUND + ['log_pi', 'pi_rank']
print('n features fundamental', len(FUND))


def make_obj(offset, starts, inv):
    def obj(preds, data):
        y = data.get_label()
        p = group_softmax(preds + offset, starts, inv)
        return p - y, np.maximum(p * (1 - p), 1e-6)
    return obj


def make_feval(offset, starts, inv):
    def feval(preds, data):
        y = data.get_label()
        p = group_softmax(preds + offset, starts, inv)
        return 'race_ll', float(race_logloss(p, y, starts).mean()), False
    return feval


def train_gbm(Xtr, ytr, rtr, offtr, Xva, yva, rva, offva, params, rounds=3000):
    s_tr, i_tr = race_index(rtr)
    s_va, i_va = race_index(rva)
    dtr = lgb.Dataset(Xtr, label=ytr, free_raw_data=False)
    dva = lgb.Dataset(Xva, label=yva, reference=dtr, free_raw_data=False)
    p = dict(params)
    p['objective'] = make_obj(offtr, s_tr, i_tr)
    p['verbosity'] = -1
    # validation uses its own offsets -> evaluate through a closure keyed on dataset identity
    feval_va = make_feval(offva, s_va, i_va)
    feval_tr = make_feval(offtr, s_tr, i_tr)

    def feval(preds, data):
        return feval_va(preds, data) if data is dva else feval_tr(preds, data)

    bst = lgb.train(p, dtr, num_boost_round=rounds, valid_sets=[dva], feval=feval,
                    callbacks=[lgb.early_stopping(150, verbose=False)])
    return bst


PARAMS_FUND = dict(learning_rate=0.03, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
                   bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=1)
PARAMS_RES = dict(learning_rate=0.02, num_leaves=15, min_data_in_leaf=2000, feature_fraction=0.6,
                  bagging_fraction=0.8, bagging_freq=1, lambda_l2=50.0, max_bin=63, num_threads=2, seed=1)

results = []
pred_store = []
for Y in TEST_YEARS:
    t0 = time.time()
    tr = F.year.between(2011, Y - 2).values
    va = (F.year == Y - 1).values
    te = (F.year == Y).values
    R = F.race_id.values
    yv = F.y.values.astype(float)
    logpi = F.log_pi.values

    # M0t: theta on train years
    s, i = race_index(R[tr])
    th, _ = fit_clogit(logpi[tr][:, None], yv[tr], s, i)
    theta = th[0]

    # M1 fundamental
    X = F[FUND].values.astype(np.float32)
    zero = np.zeros(len(F))
    bst1 = train_gbm(X[tr], yv[tr], R[tr], zero[tr], X[va], yv[va], R[va], zero[va], PARAMS_FUND)
    f_va = bst1.predict(X[va], raw_score=True)
    f_te = bst1.predict(X[te], raw_score=True)
    s_va, i_va = race_index(R[va])
    s_te, i_te = race_index(R[te])
    pf_va = group_softmax(f_va, s_va, i_va)
    pf_te = group_softmax(f_te, s_te, i_te)

    # M2 two-stage on Y-1
    Z_va = np.c_[np.log(pf_va), logpi[va]]
    ab, _ = fit_clogit(Z_va, yv[va], s_va, i_va)
    Z_te = np.c_[np.log(pf_te), logpi[te]]
    pc_te = group_softmax(Z_te @ ab, s_te, i_te)

    # M3 market-residual GBM
    XM = F[MKT_FEATS].values.astype(np.float32)
    off = theta * logpi
    bst3 = train_gbm(XM[tr], yv[tr], R[tr], off[tr], XM[va], yv[va], R[va], off[va], PARAMS_RES)
    pr_te = group_softmax(bst3.predict(XM[te], raw_score=True) + off[te], s_te, i_te)

    p0_te = F.mkt_p.values[te]
    p0t_te = group_softmax(theta * logpi[te], s_te, i_te)
    ll = {k: race_logloss(v, yv[te], s_te) for k, v in
          {'M0': p0_te, 'M0t': p0t_te, 'M1': pf_te, 'M2': pc_te, 'M3': pr_te}.items()}
    rec = {'year': Y, 'races': len(s_te), 'theta': round(theta, 4), 'a': round(ab[0], 4), 'b': round(ab[1], 4),
           'iters_M1': bst1.best_iteration, 'iters_M3': bst3.best_iteration}
    for k in ll:
        rec[f'LL_{k}'] = round(ll[k].mean(), 5)
    for k in ['M2', 'M3']:
        m, lo, hi = bootstrap_mean_ci(ll['M0'] - ll[k])
        rec[f'gain_{k}_vs_M0'] = f'{m:+.5f} [{lo:+.5f},{hi:+.5f}]'
        m, lo, hi = bootstrap_mean_ci(ll['M0t'] - ll[k])
        rec[f'gain_{k}_vs_M0t'] = f'{m:+.5f} [{lo:+.5f},{hi:+.5f}]'
    rec['secs'] = round(time.time() - t0)
    print(json.dumps(rec, ensure_ascii=False), flush=True)
    results.append(rec)
    pred_store.append(pd.DataFrame({'race_id': R[te], 'horse_number': F.horse_number.values[te], 'year': Y,
                                    'p_mkt': p0_te, 'p_mkt_t': p0t_te, 'p_fund': pf_te, 'p_two': pc_te, 'p_res': pr_te}))
    if Y == TEST_YEARS[-1]:
        imp = pd.Series(bst1.feature_importance('gain'), index=FUND).sort_values(ascending=False)
        print('M1 top features:', imp.head(25).round(0).to_dict())
        imp3 = pd.Series(bst3.feature_importance('gain'), index=MKT_FEATS).sort_values(ascending=False)
        print('M3 top features:', imp3.head(25).round(0).to_dict())

pd.DataFrame(results).to_csv(f'{OUT}/walkforward_{TAG}.csv', index=False)
pd.concat(pred_store).to_parquet(f'{OUT}/preds_{TAG}.parquet')
