"""An explicit, readable equation:  p_i = exp(s_i) / sum_j exp(s_j)
   s_i = theta * log(pi_i) + sum_k w_k * z_ik
pi = market win probability (normalised 1/odds), z = standardised features (missing -> 0, plus a missing flag).
Fitted by maximum likelihood on train years, evaluated walk-forward.
usage: python equation.py FEATURE[,FEATURE...] TEST_YEARS [L2]
"""
import json
import sys

import numpy as np
import pandas as pd

from common import D, OUT, race_index, group_softmax, race_logloss, fit_clogit, bootstrap_mean_ci

feats = sys.argv[1].split(',')
years = [int(x) for x in sys.argv[2].split(',')]
l2 = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
src = sys.argv[4] if len(sys.argv) > 4 else f'{D}/features.parquet'

F = pd.read_parquet(src, columns=list({'race_id', 'horse_number', 'date', 'start_min', 'year', 'y', 'mkt_p',
                                         'dead_heat_win', *feats}))
F = F[~F.dead_heat_win & (F.year >= 2011)].sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
lp = np.log(F.mkt_p.values)
R = F.race_id.values
y = F.y.values.astype(float)
out = []
for Y in years:
    tr = F.year.between(2011, Y - 1).values
    te = (F.year == Y).values
    cols, names = [lp], ['log_pi']
    for c in feats:
        x = F[c].values.astype(float)
        mu, sd = np.nanmean(x[tr]), np.nanstd(x[tr]) + 1e-9
        z = np.nan_to_num((x - mu) / sd)
        z = np.clip(z, -4, 4)
        cols.append(z)
        names.append(c)
        if np.isnan(x[tr]).mean() > 0.01:
            cols.append(np.isnan(x).astype(float))
            names.append(c + '_missing')
    X = np.column_stack(cols)
    s1, i1 = race_index(R[tr])
    w, _ = fit_clogit(X[tr], y[tr], s1, i1, l2=l2 * 1e-3, w0=np.r_[1.0, np.zeros(X.shape[1] - 1)])
    s2, i2 = race_index(R[te])
    p = group_softmax(X[te] @ w, s2, i2)
    s0 = group_softmax(lp[te], s2, i2)
    th, _ = fit_clogit(lp[tr][:, None], y[tr], s1, i1)
    st = group_softmax(th[0] * lp[te], s2, i2)
    ll, ll0, llt = race_logloss(p, y[te], s2), race_logloss(s0, y[te], s2), race_logloss(st, y[te], s2)
    m, lo, hi = bootstrap_mean_ci(ll0 - ll)
    m2, lo2, hi2 = bootstrap_mean_ci(llt - ll)
    rec = {'year': Y, 'LL_eq': round(ll.mean(), 5), 'LL_mkt': round(ll0.mean(), 5),
           'gain_vs_mkt': f'{m:+.5f} [{lo:+.5f},{hi:+.5f}]', 'gain_vs_mkt_theta': f'{m2:+.5f} [{lo2:+.5f},{hi2:+.5f}]',
           'w': {n: round(float(v), 4) for n, v in zip(names, w)}}
    print(json.dumps(rec, ensure_ascii=False), flush=True)
    out.append(rec)
