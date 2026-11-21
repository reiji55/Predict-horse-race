"""Evaluate combination bets built from any win-probability column against real payouts.

usage: python eval_combos.py PRED_FILE PCOL FIT_YEARS EVAL_YEARS [TAG]
  PRED_FILE : parquet with race_id, horse_number, year, <PCOL>
  FIT_YEARS : years used to fit gamma/delta (discounted Harville) on finishing orders, e.g. 2019
  EVAL_YEARS: years evaluated, e.g. 2020,2021,2022,2023

Outputs per bet type:
  - mean LLR vs the combination market (realised payout -> q = rate*100/payout)
  - Kelly-proportional growth per race (= LLR + log rate)  (>0 means a profitable bettor in theory)
  - top-K flat betting: hit rate and ROI (exact, needs only realised payouts)
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
tag = sys.argv[5] if len(sys.argv) > 5 else pcol

pr = pd.read_parquet(pred_file)
runs = pd.read_parquet(f'{D}/runs.parquet')[['race_id', 'horse_number', 'finish', 'date', 'start_min', 'race_num']]
pr = pr.merge(runs, on=['race_id', 'horse_number'], how='left')
pr = pr.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
pr['pw'] = pr[pcol] / pr.groupby('race_id')[pcol].transform('sum')


def exploded(df, place):
    d = df[~(df.finish < place)].copy()
    d['yy'] = (d.finish == place).astype(float)
    d = d[d.groupby('race_id').yy.transform('sum') == 1]
    ok = df.groupby('race_id').finish.apply(lambda s: (s < place).sum() == place - 1)
    d = d[d.race_id.map(ok)]
    s, i = race_index(d.race_id.values)
    return np.log(d.pw.values)[:, None], d.yy.values, s, i


fit = pr[pr.year.isin(fit_years)]
g, _ = fit_clogit(*exploded(fit, 2))
dl, _ = fit_clogit(*exploded(fit, 3))
gamma, delta = float(g[0]), float(dl[0])

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

KS = [1, 3, 5, 10, 20]
IDX_CACHE = {}
stat = {bt: {'llr': [], 'K': {k: [0, 0, 0.0] for k in KS}} for bt in ['馬連', 'ワイド', '馬単', '三連複', '三連単']}
nr = 0
for rid, grp in ev.groupby('race_id', sort=False):
    p = grp.pw.values.astype(float)
    hn = grp.horse_number.values
    pos = {h: i for i, h in enumerate(hn)}
    n = len(p)
    if n < 4:
        continue
    nr += 1
    P2, P3, um, srp, wd = combo_tables(p, gamma, delta)
    if n not in IDX_CACHE:
        iu_ = np.triu_indices(n, 1)
        IDX_CACHE[n] = (iu_, np.c_[iu_], np.array(list(itertools.combinations(range(n), 3))),
                        np.array(list(itertools.permutations(range(n), 3))),
                        np.array(list(itertools.permutations(range(n), 2))))
    iu, pairs, tri, perm, ut = IDX_CACHE[n]
    tables = {
        '馬連': (pairs, um[iu]),
        'ワイド': (pairs, wd[iu]),
        '馬単': (ut, P2[ut[:, 0], ut[:, 1]]),
        '三連複': (tri, srp[tri[:, 0], tri[:, 1], tri[:, 2]]),
        '三連単': (perm, P3[perm[:, 0], perm[:, 1], perm[:, 2]]),
    }
    for bt, (idx, probs) in tables.items():
        order = np.argsort(-probs)
        if bt == 'ワイド':
            if rid not in wide:
                continue
            winners = {}
            for combo, payout in wide[rid].items():
                if all(h in pos for h in combo):
                    winners[tuple(sorted(pos[h] for h in combo))] = payout
            for k in KS:
                top = idx[order[:k]]
                ret = sum(winners.get(tuple(sorted(t)), 0) for t in top)
                hit = any(tuple(sorted(t)) in winners for t in top)
                stat[bt]['K'][k][0] += min(k, len(top))
                stat[bt]['K'][k][1] += int(hit)
                stat[bt]['K'][k][2] += ret
            # per-pair LLR for wide (marginal top-3 pair probability vs payout-implied)
            for t, payout in winners.items():
                pm = wd[t[0], t[1]]
                stat[bt]['llr'].append(np.log(max(pm, 1e-12)) - np.log(RATE[bt] * 100 / payout))
            continue
        if rid not in single[bt]:
            continue
        combo, payout = single[bt][rid]
        if not all(h in pos for h in combo):
            continue
        ci = [pos[h] for h in combo]
        key = tuple(sorted(ci)) if bt in ('馬連', '三連複') else tuple(ci)
        # probability of realised combination
        if bt == '馬連':
            pm = um[key[0], key[1]]
        elif bt == '馬単':
            pm = P2[key[0], key[1]]
        elif bt == '三連複':
            pm = srp[key[0], key[1], key[2]]
        else:
            pm = P3[key[0], key[1], key[2]]
        stat[bt]['llr'].append(np.log(max(pm, 1e-12)) - np.log(RATE[bt] * 100 / payout))
        for k in KS:
            top = idx[order[:k]]
            hit = any(tuple(t) == key for t in top)
            stat[bt]['K'][k][0] += min(k, len(top))
            stat[bt]['K'][k][1] += int(hit)
            stat[bt]['K'][k][2] += payout if hit else 0

out = {'tag': tag, 'gamma': round(gamma, 4), 'delta': round(delta, 4), 'races': nr}
for bt, s in stat.items():
    llr = np.array(s['llr'])
    out[bt] = {'meanLLR': round(llr.mean(), 4), 'kelly_growth': round(llr.mean() + np.log(RATE[bt]), 4)}
    for k, (bets, hits, ret) in s['K'].items():
        out[bt][f'top{k}'] = {'hit': round(hits / nr, 4), 'roi': round(ret / (bets * 100), 4)}
print(json.dumps(out, ensure_ascii=False, indent=1))
with open(f'{OUT}/combos_{tag}.json', 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
