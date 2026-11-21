"""Odds-free ordering model: P(1st), P(2nd | 1st), P(3rd | 1st, 2nd) from horse / past-race / race-condition data only.
No odds, no popularity, no feature derived from past odds.

Stage k (k = 1, 2, 3): among horses not yet placed, P(j finishes k-th) = softmax_j( g_k(x_j) ), g_k = LightGBM.
usage: python pure_models.py TRAIN_END VALID_YEAR TEST_YEARS TAG
  e.g. python pure_models.py 2021 2022 2023 dev      (train 2011..2021, early-stop 2022, predict 2023)
"""
import json
import sys

import numpy as np
import pandas as pd

from common import OUT, race_index, group_softmax, race_logloss
from models_lib import load_features, train_gbm, PARAMS_FUND

ODDS_DERIVED = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5',
                'logodds_mean5_rel', 'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk',
                'jky_mratio', 'trn_mratio', 'own_mratio', 'jky90_mratio', 'log_pi', 'pi_rank'}

train_end, valid_year = int(sys.argv[1]), int(sys.argv[2])
test_years = [int(x) for x in sys.argv[3].split(',')]
tag = sys.argv[4]

import os
F, FUND = load_features(os.environ.get('FEAT_FILE', '/home/claude/keiba_lab/data/features.parquet'))
FEATS = [c for c in FUND if c not in ODDS_DERIVED]
F['fin'] = F.finish.fillna(99)
print('features used:', len(FEATS), flush=True)


def stage_data(mask, place):
    d = F[mask]
    if place == 1:
        return d
    better = d.groupby('race_id').fin.transform(lambda s: (s < place).sum())
    one = d.groupby('race_id').fin.transform(lambda s: (s == place).sum())
    return d[(better == place - 1) & (one == 1) & (d.fin >= place)]


test_mask = F.year.isin(test_years)
pred = F[test_mask][['race_id', 'horse_number', 'year']].copy()
rec = {'tag': tag, 'train': f'2011-{train_end}', 'valid': valid_year, 'test': test_years}
for place in (1, 2, 3):
    tr = stage_data(F.year.between(2011, train_end), place)
    va = stage_data(F.year == valid_year, place)
    ytr = (tr.fin == place).values.astype(float)
    yva = (va.fin == place).values.astype(float)
    bst = train_gbm(tr[FEATS].values.astype(np.float32), ytr, tr.race_id.values, np.zeros(len(tr)),
                    va[FEATS].values.astype(np.float32), yva, va.race_id.values, np.zeros(len(va)), PARAMS_FUND)
    bst.save_model(f'{OUT}/pure_{tag}_s{place}.txt')
    te = F[test_mask]
    pred[f's{place}'] = bst.predict(te[FEATS].values.astype(np.float32), raw_score=True)
    ts = stage_data(test_mask, place)
    yts = (ts.fin == place).values.astype(float)
    s_, i_ = race_index(ts.race_id.values)
    p = group_softmax(bst.predict(ts[FEATS].values.astype(np.float32), raw_score=True), s_, i_)
    n_per = np.diff(np.r_[s_, len(ts)])
    rec[f'stage{place}'] = {'iters': bst.best_iteration, 'LL_model': round(float(race_logloss(p, yts, s_).mean()), 5),
                            'LL_uniform': round(float(np.log(n_per).mean()), 5)}
    imp = pd.Series(bst.feature_importance('gain'), index=FEATS).sort_values(ascending=False)
    rec[f'stage{place}_top_features'] = imp.head(15).round(0).to_dict()
    print(json.dumps(rec[f'stage{place}'], ensure_ascii=False), flush=True)
pred.to_parquet(f'{OUT}/pure_scores_{tag}.parquet')
with open(f'{OUT}/pure_{tag}.json', 'w') as f:
    json.dump(rec, f, ensure_ascii=False, indent=1)
with open(f'{OUT}/pure_features.json', 'w') as f:
    json.dump(FEATS, f, ensure_ascii=False)
print(json.dumps(rec, ensure_ascii=False))
