"""Combination evaluation with stage-specific ordering scores.
Model:  1st = win model p ; 2nd/3rd = softmax over remaining horses of place-model scores s2 / s3
Market comparator Q (used by the value rule): discounted Harville from the win pool (gamma/delta fitted on FIT years).
usage: python eval_order.py WIN_PRED_FILE PLACE_SCORE_FILE FIT_YEARS EVAL_YEARS TAG [harville]
  last arg 'harville' -> ignore place scores and use discounted Harville for the model too (baseline)
"""
import itertools
import json
import os
import sys

import numpy as np
import pandas as pd

from common import D, OUT, race_index, fit_clogit
from combos import RATE

win_file, place_file = sys.argv[1], sys.argv[2]
fit_years = [int(x) for x in sys.argv[3].split(',')]
eval_years = [int(x) for x in sys.argv[4].split(',')]
tag = sys.argv[5]
use_harville = len(sys.argv) > 6 and sys.argv[6] == 'harville'
MAIN_ONLY = os.environ.get('MAIN') == '1'

wp = pd.read_parquet(win_file)[['race_id', 'horse_number', 'year', 'p_res']]
runs = pd.read_parquet(f'{D}/runs.parquet')[['race_id', 'horse_number', 'finish', 'date', 'start_min', 'mkt_p', 'race_num']]
df = wp.merge(runs, on=['race_id', 'horse_number'])
if not use_harville:
    ps = pd.read_parquet(place_file)[['race_id', 'horse_number', 's2', 's3']]
    df = df.merge(ps, on=['race_id', 'horse_number'], how='left')
df = df.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)


def fit_gd(d, col):
    d = d.copy()
    d['pw'] = d[col] / d.groupby('race_id')[col].transform('sum')
    res = []
    for place in (2, 3):
        e = d[~(d.finish < place)].copy()
        e['yy'] = (e.finish == place).astype(float)
        e = e[e.groupby('race_id').yy.transform('sum') == 1]
        ok = d.groupby('race_id').finish.apply(lambda s: (s < place).sum() == place - 1)
        e = e[e.race_id.map(ok)]
        s, i = race_index(e.race_id.values)
        w, _ = fit_clogit(np.log(e.pw.values)[:, None], e.yy.values, s, i)
        res.append(float(w[0]))
    return res


if os.environ.get('FIXED_GD') == '1':          # market gamma/delta fixed (fitted on 2019); model uses its own place scores
    gq, dq, gm, dm = 0.838, 0.737, 1.0, 1.0
else:
    fit = df[df.year.isin(fit_years)]
    gq, dq = fit_gd(fit, 'mkt_p')
    gm, dm = fit_gd(fit, 'p_res')
print(f'market gamma/delta {gq:.3f}/{dq:.3f}  model(harville) {gm:.3f}/{dm:.3f}  use_harville={use_harville}')


def tables(p1, s2, s3):
    n = len(p1)
    e2 = np.exp(s2 - s2.max())
    e3 = np.exp(s3 - s3.max())
    S2, S3 = e2.sum(), e3.sum()
    P2 = p1[:, None] * e2[None, :] / (S2 - e2[:, None])
    np.fill_diagonal(P2, 0)
    den = S3 - e3[:, None, None] - e3[None, :, None]
    P3 = P2[:, :, None] * e3[None, None, :] / np.where(den > 1e-300, den, np.nan)
    ix = np.arange(n)
    P3[ix, ix, :] = 0
    P3[ix, :, ix] = 0
    P3[:, ix, ix] = 0
    P3 = np.nan_to_num(P3)
    um = P2 + P2.T
    srp = sum(P3.transpose(p) for p in itertools.permutations(range(3)))
    wd = srp.sum(axis=2)
    return P2, P3, um, srp, wd


pay = pd.read_parquet(f'{D}/payouts.parquet')
ev = df[df.year.isin(eval_years)]
if MAIN_ONLY:
    ev = ev[ev.race_num == 11]
pay = pay[pay.race_id.isin(ev.race_id.unique())]
single = {}
for bt in ['馬連', '馬単', '三連複', '三連単']:
    d = pay[pay.bet_type == bt]
    cnt = d.groupby('race_id').size()
    d = d[d.race_id.map(cnt) == 1]
    single[bt] = dict(zip(d.race_id, zip(d.horse_numbers.apply(tuple), d.payout)))
wide = {rid: {tuple(sorted(h)): v for h, v in zip(gp.horse_numbers, gp.payout)}
        for rid, gp in pay[pay.bet_type == 'ワイド'].groupby('race_id')}

KS_TOP = [1, 3, 5, 10]
KS_VAL = [1.0, 1.1, 1.2, 1.3, 1.5, 2.0]
CAPS = {'馬連': 200, 'ワイド': 60, '馬単': 400, '三連複': 1000, '三連単': 5000}
BTS = ['馬連', 'ワイド', '馬単', '三連複', '三連単']
llr = {bt: [] for bt in BTS}
top = {(bt, k): [0, 0, 0.0] for bt in BTS for k in KS_TOP}
val = {(bt, k): [0, 0.0, []] for bt in BTS for k in KS_VAL}
IDX = {}
nr = 0
for rid, g in ev.groupby('race_id', sort=False):
    n = len(g)
    if n < 5:
        continue
    if not use_harville and g.s2.isna().any():
        continue
    nr += 1
    yr = g.year.iat[0]
    pm = g.p_res.values / g.p_res.sum()
    pq = g.mkt_p.values / g.mkt_p.sum()
    if use_harville:
        A = tables(pm, gm * np.log(pm), dm * np.log(pm))
    else:
        A = tables(pm, g.s2.values, g.s3.values)
    B = tables(pq, gq * np.log(pq), dq * np.log(pq))
    pos = {h: i for i, h in enumerate(g.horse_number.values)}
    if n not in IDX:
        iu = np.triu_indices(n, 1)
        IDX[n] = (iu, np.c_[iu], np.array(list(itertools.combinations(range(n), 3))),
                  np.array(list(itertools.permutations(range(n), 3))), np.array(list(itertools.permutations(range(n), 2))))
    iu, pairs, tri, perm, ut = IDX[n]

    def flat(T):
        P2, P3, um, srp, wd = T
        return {'馬連': (pairs, um[iu]), 'ワイド': (pairs, wd[iu]), '馬単': (ut, P2[ut[:, 0], ut[:, 1]]),
                '三連複': (tri, srp[tri[:, 0], tri[:, 1], tri[:, 2]]), '三連単': (perm, P3[perm[:, 0], perm[:, 1], perm[:, 2]])}

    FA, FB = flat(A), flat(B)
    for bt in BTS:
        idx, P = FA[bt]
        Q = FB[bt][1]
        if bt == 'ワイド':
            if rid not in wide:
                continue
            win = {tuple(sorted(pos[h] for h in c)): v for c, v in wide[rid].items() if all(h in pos for h in c)}
        else:
            if rid not in single[bt]:
                continue
            c, v = single[bt][rid]
            if not all(h in pos for h in c):
                continue
            ci = [pos[h] for h in c]
            win = {tuple(sorted(ci)) if bt in ('馬連', '三連複') else tuple(ci): v}
        keyf = (lambda t: tuple(sorted(t))) if bt in ('馬連', 'ワイド', '三連複') else tuple
        # LLR at realised combinations
        lookup = {keyf(tuple(t)): j for j, t in enumerate(idx)} if bt != '三連単' else None
        for c, v in win.items():
            if bt == '三連単':
                pmod = A[1][c[0], c[1], c[2]]
            else:
                pmod = P[lookup[c]]
            llr[bt].append(np.log(max(pmod, 1e-12)) - np.log(RATE[bt] * 100 / v))
        order = np.argsort(-P)
        for k in KS_TOP:
            sel = order[:k]
            ret = sum(win.get(keyf(tuple(idx[s])), 0) for s in sel)
            t = top[(bt, k)]
            t[0] += len(sel)
            t[1] += int(ret > 0)
            t[2] += ret
        ratio = P / np.maximum(Q, 1e-12)
        implied = RATE[bt] / np.maximum(Q, 1e-12)
        for k in KS_VAL:
            sel = np.flatnonzero((ratio > k) & (implied < CAPS[bt]))
            if len(sel) == 0:
                continue
            ret = sum(win.get(keyf(tuple(idx[s])), 0) for s in sel)
            a = val[(bt, k)]
            a[0] += len(sel)
            a[1] += ret
            a[2].append((yr, len(sel), ret))

out = {'tag': tag, 'races': nr}
for bt in BTS:
    L = np.array(llr[bt])
    o = {'meanLLR': round(L.mean(), 4), 'kelly_growth': round(L.mean() + np.log(RATE[bt]), 4)}
    for k in KS_TOP:
        b_, h_, r_ = top[(bt, k)]
        o[f'top{k}'] = {'hit': round(h_ / nr, 4), 'roi': round(r_ / (b_ * 100), 4)}
    for k in KS_VAL:
        b_, r_, per = val[(bt, k)]
        if b_ == 0:
            continue
        per = pd.DataFrame(per, columns=['year', 'bets', 'ret'])
        rng = np.random.default_rng(0)
        bs = []
        for _ in range(1000):
            ix = rng.integers(0, len(per), len(per))
            bs.append(per.ret.values[ix].sum() / (per.bets.values[ix].sum() * 100))
        by = {int(y): round(d.ret.sum() / (d.bets.sum() * 100), 3) for y, d in per.groupby('year')}
        o[f'value>{k}'] = {'bets': int(b_), 'roi': round(r_ / (b_ * 100), 4),
                           'ci95': [round(float(np.quantile(bs, .025)), 3), round(float(np.quantile(bs, .975)), 3)],
                           'by_year': by}
    out[bt] = o
print(json.dumps(out, ensure_ascii=False, indent=1))
with open(f'{OUT}/order_{tag}.json', 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
