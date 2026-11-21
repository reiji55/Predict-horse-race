"""Implementable value rule for combination bets (needs only win odds + the model):
   bet 100 yen on combination c  if  P_model(c) / Q_win(c) > k   (and Q-implied odds within a cap)
   P_model = discounted Harville from the model's win probabilities
   Q_win   = discounted Harville from the market's win probabilities (what the win pool 'thinks')
Return uses the REAL payout of the realised combination, so the backtest is exact for this rule.
usage: python combo_value.py PRED_FILE PCOL FIT_YEARS EVAL_YEARS TAG
"""
import itertools
import json
import sys
import os
MAIN_ONLY = os.environ.get("MAIN") == "1"

import numpy as np
import pandas as pd

from common import D, OUT, race_index, fit_clogit
from combos import combo_tables, RATE

pred_file, pcol = sys.argv[1], sys.argv[2]
fit_years = [int(x) for x in sys.argv[3].split(',')]
eval_years = [int(x) for x in sys.argv[4].split(',')]
tag = sys.argv[5]

pr = pd.read_parquet(pred_file)
runs = pd.read_parquet(f'{D}/runs.parquet')[['race_id', 'horse_number', 'finish', 'date', 'start_min', 'mkt_p', 'race_num']]
pr = pr.drop(columns=[c for c in ('p_mkt',) if c in pr]).merge(runs, on=['race_id', 'horse_number'], how='left')
pr = pr.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)


def fit_gd(df, col):
    df = df.copy()
    df['pw'] = df[col] / df.groupby('race_id')[col].transform('sum')
    out = []
    for place in (2, 3):
        d = df[~(df.finish < place)].copy()
        d['yy'] = (d.finish == place).astype(float)
        d = d[d.groupby('race_id').yy.transform('sum') == 1]
        ok = df.groupby('race_id').finish.apply(lambda s: (s < place).sum() == place - 1)
        d = d[d.race_id.map(ok)]
        s, i = race_index(d.race_id.values)
        w, _ = fit_clogit(np.log(d.pw.values)[:, None], d.yy.values, s, i)
        out.append(float(w[0]))
    return out


fit = pr[pr.year.isin(fit_years)]
gm, dm = fit_gd(fit, pcol)
gq, dq = fit_gd(fit, 'mkt_p')
print(f'model gamma/delta {gm:.3f}/{dm:.3f}   market {gq:.3f}/{dq:.3f}')

pay = pd.read_parquet(f'{D}/payouts.parquet')
ev = pr[pr.year.isin(eval_years)]
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

KS = [1.0, 1.1, 1.2, 1.3, 1.5, 2.0]
CAPS = {'馬連': 200, 'ワイド': 60, '馬単': 400, '三連複': 1000, '三連単': 5000}
acc = {(bt, k): [0, 0, 0.0, []] for bt in CAPS for k in KS}
IDX = {}
for rid, g in ev.groupby('race_id', sort=False):
    n = len(g)
    if n < 5:
        continue
    pm = g[pcol].values / g[pcol].values.sum()
    pq = g.mkt_p.values / g.mkt_p.values.sum()
    pos = {h: i for i, h in enumerate(g.horse_number.values)}
    A2, A3, Aum, Asrp, Awd = combo_tables(pm, gm, dm)
    B2, B3, Bum, Bsrp, Bwd = combo_tables(pq, gq, dq)
    if n not in IDX:
        iu = np.triu_indices(n, 1)
        IDX[n] = (iu, np.c_[iu], np.array(list(itertools.combinations(range(n), 3))),
                  np.array(list(itertools.permutations(range(n), 3))), np.array(list(itertools.permutations(range(n), 2))))
    iu, pairs, tri, perm, ut = IDX[n]
    T = {
        '馬連': (pairs, Aum[iu], Bum[iu]),
        'ワイド': (pairs, Awd[iu], Bwd[iu]),
        '馬単': (ut, A2[ut[:, 0], ut[:, 1]], B2[ut[:, 0], ut[:, 1]]),
        '三連複': (tri, Asrp[tri[:, 0], tri[:, 1], tri[:, 2]], Bsrp[tri[:, 0], tri[:, 1], tri[:, 2]]),
        '三連単': (perm, A3[perm[:, 0], perm[:, 1], perm[:, 2]], B3[perm[:, 0], perm[:, 1], perm[:, 2]]),
    }
    for bt, (idx, P, Q) in T.items():
        ratio = P / np.maximum(Q, 1e-12)
        implied = RATE[bt] / np.maximum(Q, 1e-12)
        # winners for this bet type
        if bt == 'ワイド':
            if rid not in wide:
                continue
            win = {}
            for c, v in wide[rid].items():
                if all(h in pos for h in c):
                    win[tuple(sorted(pos[h] for h in c))] = v
        else:
            if rid not in single[bt]:
                continue
            c, v = single[bt][rid]
            if not all(h in pos for h in c):
                continue
            ci = [pos[h] for h in c]
            key = tuple(sorted(ci)) if bt in ('馬連', '三連複') else tuple(ci)
            win = {key: v}
        for k in KS:
            sel = np.flatnonzero((ratio > k) & (implied < CAPS[bt]))
            if len(sel) == 0:
                continue
            ret = 0
            for s_ in sel:
                key = tuple(idx[s_])
                if bt in ('馬連', 'ワイド', '三連複'):
                    key = tuple(sorted(key))
                ret += win.get(key, 0)
            a = acc[(bt, k)]
            a[0] += len(sel)
            a[1] += int(ret > 0)
            a[2] += ret
            a[3].append((g.year.iat[0], len(sel), ret))

res = {}
for (bt, k), (bets, hits, ret, per) in acc.items():
    if bets == 0:
        continue
    per = pd.DataFrame(per, columns=['year', 'bets', 'ret'])
    by = per.groupby('year').apply(lambda d: d.ret.sum() / (d.bets.sum() * 100), include_groups=False).round(3).to_dict()
    # race-bootstrap CI of ROI
    rng = np.random.default_rng(0)
    b_arr, r_arr = per.bets.values, per.ret.values
    bs = []
    for _ in range(1000):
        ix = rng.integers(0, len(per), len(per))
        bs.append(r_arr[ix].sum() / (b_arr[ix].sum() * 100))
    res[f'{bt} k>{k}'] = {'bets': bets, 'races_with_bets': len(per), 'roi': round(ret / (bets * 100), 4),
                          'ci95': [round(np.quantile(bs, .025), 3), round(np.quantile(bs, .975), 3)], 'by_year': by}
    print(f'{bt} ratio>{k}: bets={bets} races={len(per)} ROI={ret / (bets * 100):.4f} CI95={res[f"{bt} k>{k}"]["ci95"]} {by}')
with open(f'{OUT}/combo_value_{tag}.json', 'w') as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
