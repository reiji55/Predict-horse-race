"""Alternative learners for 'which horses finish top 3' (odds-free):
  binary : LightGBM, label = finished top 3, then per-race shift so the probabilities add up to min(3, field)
  rank   : LightGBM lambdarank, relevance 3/2/1 for 1st/2nd/3rd, 0 otherwise (optimises the order at the top)
usage: python rank_models.py FEAT_FILE TRAIN_END VALID_YEAR TEST_YEARS TAG
writes out/rank_scores_<TAG>.parquet with race_id, horse_number, year, p_bin, s_rank
"""
import json
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd

from models_lib import load_features

ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
feat_file, train_end, valid_year = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
test_years = [int(x) for x in sys.argv[4].split(',')]
tag = sys.argv[5]
OUT = '/home/claude/keiba_lab/out'
F, FUND = load_features(feat_file)
FEATS = [c for c in FUND if c not in ODDS]
F['top3'] = ((F.finish <= 3)).astype(float)
F['rel'] = np.select([F.finish == 1, F.finish == 2, F.finish == 3], [3, 2, 1], 0)
tr = F[F.year.between(2011, train_end)]
va = F[F.year == valid_year]
te = F[F.year.isin(test_years)]
Xtr, Xva, Xte = (d[FEATS].values.astype(np.float32) for d in (tr, va, te))

# ---- binary ----
pb = dict(objective='binary', learning_rate=0.03, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
          bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=1, verbosity=-1)
dtr = lgb.Dataset(Xtr, tr.top3.values)
dva = lgb.Dataset(Xva, va.top3.values, reference=dtr)
bb = lgb.train(pb, dtr, 4000, valid_sets=[dva], callbacks=[lgb.early_stopping(150, verbose=False)])
raw = bb.predict(Xte, raw_score=True)


def shift_to_sum(logit, target, iters=40):
    c = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(logit + c)))
        f = p.sum() - target
        d = (p * (1 - p)).sum()
        c -= f / max(d, 1e-9)
    return 1 / (1 + np.exp(-(logit + c)))


out = te[['race_id', 'horse_number', 'year']].copy()
out['raw_bin'] = raw
out['p_bin'] = 0.0
for rid, idx in out.groupby('race_id').indices.items():
    n = len(idx)
    out.iloc[idx, out.columns.get_loc('p_bin')] = shift_to_sum(raw[idx], min(3, n))

# ---- lambdarank ----
import os
SKIP_RANK = os.environ.get('SKIP_RANK') == '1'
def groups(d):
    return d.groupby('race_id', sort=False).size().values


pr = dict(objective='lambdarank', learning_rate=0.03, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
          bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=1, verbosity=-1,
          eval_at=[3, 5], label_gain=[0, 1, 3, 7], lambdarank_truncation_level=8)
if not SKIP_RANK:
    dtr = lgb.Dataset(Xtr, tr.rel.values, group=groups(tr))
    dva = lgb.Dataset(Xva, va.rel.values, group=groups(va), reference=dtr)
    br = lgb.train(pr, dtr, 4000, valid_sets=[dva], callbacks=[lgb.early_stopping(150, verbose=False)])
    out['s_rank'] = br.predict(Xte)
    br.save_model(f'{OUT}/rank_{tag}.txt')
out.to_parquet(f'{OUT}/rank_scores_{tag}.parquet')
bb.save_model(f'{OUT}/bin_{tag}.txt')
print(json.dumps({'tag': tag, 'binary_iters': bb.best_iteration, 'features': len(FEATS)}))
