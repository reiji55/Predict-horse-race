"""Box bets from the odds-free model's top-K horses (by P(top3)), real payouts. Compare with the K most popular horses."""
import os, sys
import numpy as np, pandas as pd
from itertools import combinations
f = sys.argv[1]; col = sys.argv[2] if len(sys.argv) > 2 else 'p_m0'
df = pd.read_parquet(f)
meta = pd.read_parquet('/home/claude/keiba_lab/data/race_meta.parquet')[['race_id', 'race_num']]
df = df.merge(meta, on='race_id')
if os.environ.get('MAIN') == '1':
    df = df[df.race_num == 11]
pay = pd.read_parquet('/home/claude/keiba_lab/data/payouts.parquet')
pay = pay[pay.race_id.isin(df.race_id.unique())]
P = {bt: {} for bt in ['三連複', 'ワイド', '馬連']}
for bt in P:
    for rid, g in pay[pay.bet_type == bt].groupby('race_id'):
        P[bt][rid] = {tuple(sorted(h)): v for h, v in zip(g.horse_numbers, g.payout)}
res = []
for rid, g in df.groupby('race_id'):
    if len(g) < 8:
        continue
    top3 = set(g[g.finish <= 3].horse_number)
    for who, key in (('式', col), ('人気', 'popularity')):
        order = g.sort_values(key, ascending=(key == 'popularity')).horse_number.tolist()
        for K in (4, 5, 6):
            box = order[:K]
            r = {'race_id': rid, 'who': who, 'K': K, 'in_box': len(top3 & set(box))}
            for bt, size in (('三連複', 3), ('ワイド', 2), ('馬連', 2)):
                combos = list(combinations(sorted(box), size))
                ret = sum(P[bt].get(rid, {}).get(c, 0) for c in combos)
                r[f'{bt}_n'] = len(combos); r[f'{bt}_ret'] = ret; r[f'{bt}_hit'] = ret > 0
            res.append(r)
R = pd.DataFrame(res)
out = R.groupby(['who', 'K']).agg(races=('race_id', 'size'), avg_in_box=('in_box', 'mean'),
                                   all3=('in_box', lambda s: (s == 3).mean()),
                                   sanrenpuku_hit=('三連複_hit', 'mean'), wide_hit=('ワイド_hit', 'mean'), umaren_hit=('馬連_hit', 'mean'))
for bt in ('三連複', 'ワイド', '馬連'):
    out[f'{bt}_roi'] = R.groupby(['who', 'K']).apply(lambda d: d[f'{bt}_ret'].sum() / (d[f'{bt}_n'].sum() * 100), include_groups=False)
print(out.round(3).to_string())
