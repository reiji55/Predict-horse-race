"""Fast rating-only pass to tune the Kalman-style ability rating.
Score = out-of-sample win log loss of softmax(k * rating_z) fitted per configuration on 2014-2016, evaluated 2017-2018.
Also reports the gain when the rating is blended with the market (does the rating carry info beyond the odds?).
"""
import itertools
import math
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

from common import D, race_index, group_softmax, race_logloss, fit_clogit

runs = pd.read_parquet(f'{D}/runs.parquet')
runs = runs.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
runs['date_ord'] = runs.date.map(pd.Timestamp.toordinal)
rid = runs.race_id.values
starts, inv = race_index(rid)
ends = np.r_[starts[1:], len(runs)]
hid = runs.horse_id.values
surf = runs.surf.values
dist = runs.distance.values.astype(float)
date = runs.date_ord.values
fin = runs.finish.values
comp = runs.completed.values
marg = runs.margin_sec.values
sec = runs.second_time.values
wt = runs.win_time.values
year = runs.year.values
y_win = (fin == 1).astype(float)
dead = runs.dead_heat_win.values


def run(S2, V0, Q, CAP, WCAP=0.6, VMIN=0.01):
    R = {}
    V = {}
    T = {}
    pre_m = np.full(len(runs), np.nan)
    pre_v = np.full(len(runs), np.nan)
    for a, b in zip(starts, ends):
        s = surf[a]
        n = b - a
        m = np.zeros(n)
        v = np.full(n, V0)
        known = np.zeros(n, bool)
        for j in range(n):
            key = (hid[a + j], s)
            if key in R:
                m[j] = R[key]
                v[j] = min(V[key] + Q * (date[a] - T[key]) / 30.0, V0)
                known[j] = True
        pre_m[a:b] = np.where(known, m, np.nan)
        pre_v[a:b] = v
        y = np.full(n, np.nan)
        for j in range(n):
            i = a + j
            if not comp[i] or np.isnan(marg[i]):
                continue
            if fin[i] == 1:
                lead = sec[i] - wt[i] if not np.isnan(sec[i]) else 0.0
                y[j] = min(lead * 1000.0 / dist[i], WCAP)
            else:
                y[j] = max(-marg[i] * 1000.0 / dist[i], -CAP)
        ob = ~np.isnan(y)
        if ob.sum() < 3:
            continue
        w = 1.0 / (v[ob] + S2)
        off = np.sum(w * (y[ob] - m[ob])) / w.sum()
        K = v / (v + S2)
        for j in np.flatnonzero(ob):
            key = (hid[a + j], s)
            R[key] = m[j] + K[j] * (y[j] - m[j] - off)
            V[key] = max((1 - K[j]) * v[j], VMIN)
            T[key] = date[a]
    return pre_m, pre_v


def evaluate(pre_m, pre_v):
    # feature: rating (fill unknown with race min - 0.2), unknown flag, sd
    df = pd.DataFrame({'rid': rid, 'm': pre_m, 'v': pre_v, 'y': y_win, 'yr': year, 'dead': dead,
                       'lp': np.log(runs.mkt_p.values)})
    mn = df.groupby('rid').m.transform('min')
    df['unk'] = df.m.isna().astype(float)
    df['mf'] = df.m.fillna(mn - 0.2).fillna(0.0)
    df['sd'] = np.sqrt(df.v)
    out = {}
    for name, tr_years, te_years in (('fit', (2014, 2016), (2017, 2018)),):
        tr = df.yr.between(*tr_years) & ~df.dead
        te = df.yr.between(*te_years) & ~df.dead
        X = df[['mf', 'unk', 'sd']].values
        s1, i1 = race_index(df.rid.values[tr])
        w, _ = fit_clogit(X[tr], df.y.values[tr], s1, i1)
        s2, i2 = race_index(df.rid.values[te])
        p = group_softmax(X[te] @ w, s2, i2)
        out['ll_rating'] = race_logloss(p, df.y.values[te], s2).mean()
        # blended with market
        Xm = np.c_[X, df.lp.values]
        wm, _ = fit_clogit(Xm[tr], df.y.values[tr], s1, i1)
        pm = group_softmax(Xm[te] @ wm, s2, i2)
        p0 = runs.mkt_p.values[te.values]
        out['ll_mkt'] = race_logloss(p0, df.y.values[te], s2).mean()
        out['ll_blend'] = race_logloss(pm, df.y.values[te], s2).mean()
        out['w_blend'] = np.round(wm, 3).tolist()
    return out


if __name__ == '__main__':
    grid = {
        'S2': [0.06, 0.12, 0.25],
        'V0': [0.15, 0.3],
        'Q': [0.002, 0.008],
        'CAP': [1.0, 2.0],
    }
    for combo in itertools.product(*grid.values()):
        cfg = dict(zip(grid.keys(), combo))
        pm_, pv_ = run(**cfg)
        r = evaluate(pm_, pv_)
        print(cfg, {k: (round(v, 5) if isinstance(v, float) else v) for k, v in r.items()}, flush=True)
