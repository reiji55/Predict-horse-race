"""Dedicated 2nd- and 3rd-place models (residual on the market), to replace the crude Harville ordering.
Stage k (k=2,3): among horses not already placed, P(j finishes k-th) = softmax_j( c_k*log(pi_j) + tree_k(x_j) )
Trained walk-forward exactly like the win model (train 2011..Y-2, early-stop Y-1, predict Y).
Output: out/place_scores.parquet with raw scores s2, s3 for every horse of every predicted year.
"""
import json
import sys

import numpy as np
import pandas as pd

from common import OUT, race_index, group_softmax, race_logloss, fit_clogit
from models_lib import load_features, train_gbm, PARAMS_RES

years = [int(x) for x in sys.argv[1].split(',')]
F, FUND = load_features()
feats = FUND + ['log_pi', 'pi_rank']
F['fin'] = F.finish.fillna(99)


def stage_data(mask, place):
    d = F[mask]
    # races where exactly place-1 horses finished better than `place` and exactly one horse is `place`
    better = d.groupby('race_id').fin.transform(lambda s: (s < place).sum())
    one = d.groupby('race_id').fin.transform(lambda s: (s == place).sum())
    d = d[(better == place - 1) & (one == 1) & (d.fin >= place)]
    return d


out = []
for Y in years:
    rec = {'year': Y}
    pred = F[F.year == Y][['race_id', 'horse_number']].copy()
    for place in (2, 3):
        tr = stage_data(F.year.between(2011, Y - 2), place)
        va = stage_data(F.year == Y - 1, place)
        ytr = (tr.fin == place).values.astype(float)
        yva = (va.fin == place).values.astype(float)
        s, i = race_index(tr.race_id.values)
        c, _ = fit_clogit(tr.log_pi.values[:, None], ytr, s, i)
        off_tr = c[0] * tr.log_pi.values
        off_va = c[0] * va.log_pi.values
        bst = train_gbm(tr[feats].values.astype(np.float32), ytr, tr.race_id.values, off_tr,
                        va[feats].values.astype(np.float32), yva, va.race_id.values, off_va, PARAMS_RES)
        te = F[F.year == Y]
        pred[f's{place}'] = bst.predict(te[feats].values.astype(np.float32), raw_score=True) + c[0] * te.log_pi.values
        pred[f's{place}_mkt'] = c[0] * te.log_pi.values
        # stage log loss on the test year (exploded set) vs market-only exponent
        ts = stage_data(F.year == Y, place)
        yts = (ts.fin == place).values.astype(float)
        s3_, i3_ = race_index(ts.race_id.values)
        raw = bst.predict(ts[feats].values.astype(np.float32), raw_score=True)
        ll_m = race_logloss(group_softmax(raw + c[0] * ts.log_pi.values, s3_, i3_), yts, s3_).mean()
        ll_0 = race_logloss(group_softmax(c[0] * ts.log_pi.values, s3_, i3_), yts, s3_).mean()
        rec[f'stage{place}'] = {'c': round(c[0], 4), 'iters': bst.best_iteration, 'LL_model': round(ll_m, 5),
                                'LL_market_pow': round(ll_0, 5), 'gain': round(ll_0 - ll_m, 5)}
    pred['year'] = Y
    out.append(pred)
    print(json.dumps(rec), flush=True)
pd.concat(out).to_parquet(f'{OUT}/place_scores_{"_".join(map(str, years))}.parquet')
