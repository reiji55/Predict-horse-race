"""Which feature groups create the market-residual gain?  (also a leakage probe)
usage: python ablation.py GROUPS TEST_YEARS TAG
  GROUPS: '+'-joined group names, e.g. MKT+CTX+RATING ; ALL ; ALL-RATING
"""
import json
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd

from common import D, OUT, race_index, group_softmax, race_logloss, fit_clogit, bootstrap_mean_ci
from models_lib import train_gbm, PARAMS_RES, load_features, GROUPS

spec, years, tag = sys.argv[1], [int(x) for x in sys.argv[2].split(',')], sys.argv[3]
F, FUND = load_features()
ALL = FUND + ['log_pi', 'pi_rank']
if spec == 'ALL':
    feats = ALL
elif spec.startswith('ALL-'):
    drop = set()
    for gname in spec[4:].split('+'):
        drop |= set(GROUPS[gname])
    feats = [c for c in ALL if c not in drop]
else:
    feats = []
    for gname in spec.split('+'):
        feats += [c for c in GROUPS[gname] if c not in feats]
R = F.race_id.values
yv = F.y.values.astype(float)
logpi = F.log_pi.values
gains = []
for Y in years:
    tr = F.year.between(2011, Y - 2).values
    va = (F.year == Y - 1).values
    te = (F.year == Y).values
    s, i = race_index(R[tr])
    th, _ = fit_clogit(logpi[tr][:, None], yv[tr], s, i)
    off = th[0] * logpi
    X = F[feats].values.astype(np.float32)
    bst = train_gbm(X[tr], yv[tr], R[tr], off[tr], X[va], yv[va], R[va], off[va], PARAMS_RES)
    s_te, i_te = race_index(R[te])
    p = group_softmax(bst.predict(X[te], raw_score=True) + off[te], s_te, i_te)
    ll = race_logloss(p, yv[te], s_te)
    ll0 = race_logloss(F.mkt_p.values[te], yv[te], s_te)
    g = ll0 - ll
    gains.append(g)
    m, lo, hi = bootstrap_mean_ci(g)
    print(json.dumps({'tag': tag, 'year': Y, 'n_feat': len(feats), 'iters': bst.best_iteration,
                      'gain': f'{m:+.5f} [{lo:+.5f},{hi:+.5f}]'}), flush=True)
g = np.concatenate(gains)
m, lo, hi = bootstrap_mean_ci(g)
print(json.dumps({'tag': tag, 'years': years, 'pooled_gain': f'{m:+.5f} [{lo:+.5f},{hi:+.5f}]'}), flush=True)
