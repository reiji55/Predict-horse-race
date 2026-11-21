"""Compare models on 'finding the top-3 finishers' (odds-free), with rank-average ensembles.
usage: python rank_eval.py TAG name=file:col [name=file:col ...]   (MAIN=1 -> 11R only)
Metrics per race (races with >= 8 runners): top-3 rate of the model's No.1/2/3; mean number of actual top-3 horses
inside the model's top 3 / top 5; share of races where all three are inside the top 5 / top 6 (= 3連複 box hits).
"""
import itertools
import os
import sys

import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
tag = sys.argv[1]
specs = [a.split('=', 1) for a in sys.argv[2:]]
base = None
names = []
for name, fc in specs:
    f, c = fc.rsplit(':', 1)
    d = pd.read_parquet(f, columns=['race_id', 'horse_number', c]).rename(columns={c: name})
    base = d if base is None else base.merge(d, on=['race_id', 'horse_number'])
    names.append(name)
runs = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'finish', 'popularity', 'race_num',
                                                      'field', 'dead_heat_win'])
df = base.merge(runs, on=['race_id', 'horse_number'])
df = df[(df.field >= 8) & ~df.dead_heat_win]
if os.environ.get('MAIN') == '1':
    df = df[df.race_num == 11]
df['top3'] = (df.finish <= 3).astype(int)
df['neg_pop'] = -df.popularity
g = df.groupby('race_id')
for n in names:
    df[f'{n}_pr'] = g[n].rank(pct=True)
# ensembles: every pair and the whole set (mean of within-race percentile ranks)
ens = {}
for k in (2, len(names)):
    if k < 2 or k > len(names):
        continue
    for combo in itertools.combinations(names, k):
        en = 'ens(' + '+'.join(combo) + ')'
        df[en] = df[[f'{c}_pr' for c in combo]].mean(axis=1)
        ens[en] = combo
models = names + list(ens) + ['neg_pop']


def metrics(col):
    rk = df.groupby('race_id')[col].rank(ascending=False, method='first')
    t = df.top3.values
    r = {}
    for k in (1, 2, 3):
        r[f'No{k}'] = t[rk.values == k].mean()
    in3 = pd.Series(t * (rk.values <= 3)).groupby(df.race_id.values).sum()
    in5 = pd.Series(t * (rk.values <= 5)).groupby(df.race_id.values).sum()
    in6 = pd.Series(t * (rk.values <= 6)).groupby(df.race_id.values).sum()
    r['in_top3'] = in3.mean()
    r['in_top5'] = in5.mean()
    r['all3_top5'] = (in5 == 3).mean()
    r['all3_top6'] = (in6 == 3).mean()
    return r


rows = {m.replace('neg_pop', '人気（参考）'): metrics(m) for m in models}
T = pd.DataFrame(rows).T
print(f'== {tag}  races: {df.race_id.nunique()}')
print(T.round(4).to_string())
T.to_csv(f'/home/claude/keiba_lab/out/rank_eval_{tag}.csv')
