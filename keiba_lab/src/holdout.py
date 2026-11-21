"""Locked holdout run (see PREREGISTRATION.md): market-residual GBM, 2024 / 2025 / 2026."""
import json

import numpy as np
import pandas as pd

from common import OUT, race_index, group_softmax, race_logloss, fit_clogit, bootstrap_mean_ci
from models_lib import load_features, train_gbm, PARAMS_RES

F, FUND = load_features()
feats = FUND + ['log_pi', 'pi_rank']
R = F.race_id.values
yv = F.y.values.astype(float)
logpi = F.log_pi.values
X = F[feats].values.astype(np.float32)
store, allg = [], []
for Y in (2024, 2025, 2026):
    tr = F.year.between(2011, Y - 2).values
    va = (F.year == Y - 1).values
    te = (F.year == Y).values
    s, i = race_index(R[tr])
    th, _ = fit_clogit(logpi[tr][:, None], yv[tr], s, i)
    off = th[0] * logpi
    bst = train_gbm(X[tr], yv[tr], R[tr], off[tr], X[va], yv[va], R[va], off[va], PARAMS_RES)
    s_te, i_te = race_index(R[te])
    p = group_softmax(bst.predict(X[te], raw_score=True) + off[te], s_te, i_te)
    pt = group_softmax(off[te], s_te, i_te)
    ll, ll0, llt = (race_logloss(v, yv[te], s_te) for v in (p, F.mkt_p.values[te], pt))
    m, lo, hi = bootstrap_mean_ci(ll0 - ll)
    m2, lo2, hi2 = bootstrap_mean_ci(llt - ll)
    allg.append(ll0 - ll)
    print(json.dumps({'year': Y, 'races': len(s_te), 'theta': round(th[0], 4), 'iters': bst.best_iteration,
                      'LL_model': round(ll.mean(), 5), 'LL_mkt': round(ll0.mean(), 5),
                      'gain_vs_mkt': f'{m:+.5f} [{lo:+.5f},{hi:+.5f}]',
                      'gain_vs_mkt_theta': f'{m2:+.5f} [{lo2:+.5f},{hi2:+.5f}]'}), flush=True)
    store.append(pd.DataFrame({'race_id': R[te], 'horse_number': F.horse_number.values[te], 'year': Y,
                               'p_mkt': F.mkt_p.values[te], 'p_res': p}))
    bst.save_model(f'{OUT}/m3_{Y}.txt')
g = np.concatenate(allg)
m, lo, hi = bootstrap_mean_ci(g)
print(json.dumps({'pooled_2024_2026_gain': f'{m:+.5f} [{lo:+.5f},{hi:+.5f}]', 'races': len(g)}))
pd.concat(store).to_parquet(f'{OUT}/preds_holdout.parquet')
