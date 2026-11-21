"""複勝 value test with real closing place odds and real payouts.
P_top3 per horse from the ordering model (win p + stage-2/3 place scores); comparator = market-only discounted Harville.
Rule: bet 100 yen on 複勝 when P_top3 * place_min > th  (place_min = lower end of the closing 複勝 odds range).
usage: python fuku_model.py WIN_PRED_FILE PLACE_SCORE_FILE EVAL_YEARS TAG
"""
import json
import os
import sys

import numpy as np
import pandas as pd

from common import D, OUT

GQ, DQ = 0.838, 0.737          # market gamma/delta fitted on 2019 (eval_order.py output)
win_file, place_file, tag = sys.argv[1], sys.argv[2], sys.argv[4]
years = [int(x) for x in sys.argv[3].split(',')]
MAIN_ONLY = os.environ.get('MAIN') == '1'

wp = pd.read_parquet(win_file)[['race_id', 'horse_number', 'year', 'p_res']]
ps = pd.read_parquet(place_file)[['race_id', 'horse_number', 's2', 's3']]
runs = pd.read_parquet(f'{D}/runs.parquet')[['race_id', 'horse_number', 'mkt_p', 'finish', 'race_num', 'dead_heat_win']]
odds = pd.read_parquet(f'{D}/odds.parquet')[['race_id', 'horse_number', 'place_min', 'place_max']]
pay = pd.read_parquet(f'{D}/payouts.parquet')
fk = pay[pay.bet_type == '複勝'].copy()
fk['hn'] = fk.horse_numbers.apply(lambda x: x[0])
fmap = {(r, h): v for r, h, v in zip(fk.race_id, fk.hn, fk.payout)}

df = wp[wp.year.isin(years)].merge(ps, on=['race_id', 'horse_number']).merge(runs, on=['race_id', 'horse_number']) \
    .merge(odds, on=['race_id', 'horse_number'])
if MAIN_ONLY:
    df = df[df.race_num == 11]
df = df.sort_values(['race_id', 'horse_number'])


def top_probs(p1, s2, s3):
    n = len(p1)
    e2 = np.exp(s2 - s2.max())
    e3 = np.exp(s3 - s3.max())
    P2 = p1[:, None] * e2[None, :] / (e2.sum() - e2[:, None])
    np.fill_diagonal(P2, 0)
    den = e3.sum() - e3[:, None, None] - e3[None, :, None]
    P3 = P2[:, :, None] * e3[None, None, :] / np.where(den > 1e-300, den, np.nan)
    ix = np.arange(n)
    P3[ix, ix, :] = 0
    P3[ix, :, ix] = 0
    P3[:, ix, ix] = 0
    P3 = np.nan_to_num(P3)
    top2 = p1 + P2.sum(axis=0)
    top3 = top2 + P3.sum(axis=(0, 1))
    return top2, top3


rows = []
for rid, g in df.groupby('race_id', sort=False):
    n = len(g)
    if n < 5 or g.place_min.isna().any() or g.dead_heat_win.any():
        continue
    pm = g.p_res.values / g.p_res.sum()
    pq = g.mkt_p.values / g.mkt_p.sum()
    m2, m3 = top_probs(pm, g.s2.values, g.s3.values)
    q2, q3 = top_probs(pq, GQ * np.log(pq), DQ * np.log(pq))
    pt_m = m2 if n <= 7 else m3
    pt_q = q2 if n <= 7 else q3
    for j, h in enumerate(g.horse_number.values):
        rows.append((rid, g.year.iat[0], h, pt_m[j], pt_q[j], g.place_min.values[j], g.place_max.values[j],
                     fmap.get((rid, h), 0)))
R = pd.DataFrame(rows, columns=['race_id', 'year', 'h', 'pm', 'pq', 'lo', 'hi', 'payout'])
R['hit'] = R.payout > 0
ll = lambda p: -(R.hit * np.log(np.clip(p, 1e-9, 1)) + (~R.hit) * np.log(np.clip(1 - p, 1e-9, 1))).mean()
out = {'tag': tag, 'horses': len(R), 'races': R.race_id.nunique(),
       'binary_logloss_model': round(ll(R.pm), 5), 'binary_logloss_mkt': round(ll(R.pq), 5)}
for col in ('pm', 'pq'):
    for th in (1.0, 1.1, 1.2):
        s = R[R[col] * R.lo > th]
        if len(s) == 0:
            continue
        rng = np.random.default_rng(0)
        g_ = s.groupby('race_id').agg(b=('payout', 'size'), r=('payout', 'sum'))
        bs = [g_.r.values[ix].sum() / (g_.b.values[ix].sum() * 100)
              for ix in (rng.integers(0, len(g_), len(g_)) for _ in range(1000))]
        out[f'{col}_EV>{th}'] = {'bets': len(s), 'hit': round(s.hit.mean(), 3),
                                 'roi': round(s.payout.sum() / (len(s) * 100), 4),
                                 'ci95': [round(float(np.quantile(bs, .025)), 3), round(float(np.quantile(bs, .975)), 3)],
                                 'by_year': {int(y): round(d.payout.sum() / (len(d) * 100), 3) for y, d in s.groupby('year')}}
print(json.dumps(out, ensure_ascii=False, indent=1))
with open(f'{OUT}/fuku_{tag}.json', 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
