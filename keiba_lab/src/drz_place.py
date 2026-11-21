"""Cross-market test (Hausch–Ziemba 'Dr. Z' idea) on JRA 複勝, 2022-2026.
P(top3) from the WIN market via discounted Harville  vs  the PLACE market's own odds (min-max range).
Bet 複勝 when P_top3 * place_odds_min > threshold.  Payouts are the real 複勝 payouts.
"""
import sys
import numpy as np
import pandas as pd
from combos import tri_tensor
from common import D, OUT

GAMMA, DELTA = 0.8568, 0.7366            # fitted on 2014-07..2019 finishing orders (market_combos.py)
src = sys.argv[1] if len(sys.argv) > 1 else None   # optional parquet with model win probs (race_id,horse_number,p)

runs = pd.read_parquet(f'{D}/runs.parquet')
odds = pd.read_parquet(f'{D}/odds.parquet')[['race_id', 'horse_number', 'place_min', 'place_max']]
pay = pd.read_parquet(f'{D}/payouts.parquet')
fuku = pay[pay.bet_type == '複勝'].copy()
fuku['hn'] = fuku.horse_numbers.apply(lambda x: x[0])
fuku_map = {(r, h): v for r, h, v in zip(fuku.race_id, fuku.hn, fuku.payout)}

df = runs.merge(odds, on=['race_id', 'horse_number'], how='inner')
if src:
    mp = pd.read_parquet(src)
    df = df.merge(mp[['race_id', 'horse_number', 'p']], on=['race_id', 'horse_number'], how='inner')
    df['pw'] = df.p
else:
    df['pw'] = df.mkt_p
df = df[~df.dead_heat_win].sort_values(['race_id', 'horse_number'])
rows = []
for rid, g in df.groupby('race_id', sort=False):
    p = g.pw.values / g.pw.values.sum()
    n = len(p)
    if n < 5 or g.place_min.isna().any():
        continue
    P2, P3 = tri_tensor(p, GAMMA, DELTA)
    top2 = P2.sum(axis=0) + p
    top3 = P3.sum(axis=(0, 1)) + P3.sum(axis=(0, 2)) + P3.sum(axis=(1, 2))
    pt = top2 if n <= 7 else top3
    for j, (h, lo, hi) in enumerate(zip(g.horse_number.values, g.place_min.values, g.place_max.values)):
        rows.append((rid, g.year.iat[0], h, pt[j], lo, hi, fuku_map.get((rid, h), 0)))
R = pd.DataFrame(rows, columns=['race_id', 'year', 'h', 'pt', 'lo', 'hi', 'payout'])
R['hit'] = R.payout > 0
print('horses', len(R), 'races', R.race_id.nunique())
# calibration of P_top3 from the win market vs reality
b = pd.cut(R.pt, [0, .05, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1])
cal = R.groupby(b, observed=True).agg(n=('hit', 'size'), p=('pt', 'mean'), rate=('hit', 'mean'),
                                       roi=('payout', lambda s: s.sum() / (len(s) * 100)))
print(cal.round(4).to_string())
for oddcol in ['lo', 'mid']:
    if oddcol == 'mid':
        R['mid'] = np.sqrt(R.lo * R.hi)
    for th in [0.9, 1.0, 1.05, 1.1, 1.2, 1.3]:
        sel = R[R.pt * R[oddcol] > th]
        if len(sel) == 0:
            continue
        print(f'odds={oddcol} EV>{th}: bets={len(sel):6d} hit={sel.hit.mean():.3f} ROI={sel.payout.sum() / (len(sel) * 100):.4f}'
              f'  by year ' + ' '.join(f'{y}:{s.payout.sum() / (len(s) * 100):.3f}({len(s)})' for y, s in sel.groupby('year')))
R.to_parquet(f'{OUT}/drz_{"model" if src else "market"}.parquet')
