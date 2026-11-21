"""Show one race through the model: win probabilities, and the value rule for 3連複 / 馬連."""
import itertools
import sys

import numpy as np
import pandas as pd

from common import D, OUT
from combos import combo_tables, RATE

rid = sys.argv[1]
GM, DM, GQ, DQ = [float(x) for x in sys.argv[2].split(',')]
pr = pd.read_parquet(f'{OUT}/preds_holdout.parquet')
runs = pd.read_parquet(f'{D}/runs.parquet')
pay = pd.read_parquet(f'{D}/payouts.parquet')
g = pr[pr.race_id == rid].merge(runs[['race_id', 'horse_number', 'horse_name', 'odds', 'popularity', 'finish']],
                                on=['race_id', 'horse_number']).sort_values('horse_number')
pm = g.p_res.values / g.p_res.sum()
pq = g.p_mkt.values / g.p_mkt.sum()
g['p_model'] = pm
g['p_market'] = pq
g['ratio'] = pm / pq
print(g[['horse_number', 'horse_name', 'odds', 'popularity', 'p_market', 'p_model', 'ratio', 'finish']]
      .round(4).to_string(index=False))
n = len(g)
hn = g.horse_number.values
A = combo_tables(pm, GM, DM)
B = combo_tables(pq, GQ, DQ)
tri = list(itertools.combinations(range(n), 3))
rows = []
for t in tri:
    P = A[3][t]
    Q = B[3][t]
    rows.append((tuple(hn[list(t)]), P, Q, P / Q, RATE['三連複'] / Q))
T = pd.DataFrame(rows, columns=['combo', 'P_model', 'Q_win', 'ratio', 'implied_odds'])
real = pay[(pay.race_id == rid) & (pay.bet_type == '三連複')]
for _, r in real.iterrows():
    c = tuple(sorted(r.horse_numbers))
    row = T[T.combo == c].iloc[0]
    rank_m = (T.P_model > row.P_model).sum() + 1
    rank_q = (T.Q_win > row.Q_win).sum() + 1
    print(f'\n3連複 result {c} payout {r.payout}円 | P_model={row.P_model:.5f} (rank {rank_m}/{len(T)})'
          f'  Q_win={row.Q_win:.5f} (rank {rank_q})  ratio={row.ratio:.2f}  win-pool implied odds {row.implied_odds:.0f}倍'
          f'  actual {r.payout / 100:.0f}倍')
for k in (1.3, 1.5):
    sel = T[(T.ratio > k) & (T.implied_odds < 1000)]
    print(f'rule ratio>{k}: buys {len(sel)} 3連複 combos; includes result: {any(sel.combo == tuple(sorted(real.horse_numbers.iloc[0])))}')
print('\ntop-10 3連複 by model:')
print(T.sort_values('P_model', ascending=False).head(10).round(5).to_string(index=False))
