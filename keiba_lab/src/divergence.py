"""Where the odds-free model disagrees with the crowd: does it pay?
usage: python divergence.py TOP3_FILE [model_col]
Uses real 複勝 payouts (pays on top 3; top 2 when 7 or fewer runners).
"""
import os
import sys

import numpy as np
import pandas as pd

from common import D

f = sys.argv[1]
col = sys.argv[2] if len(sys.argv) > 2 else 'p_m0'
df = pd.read_parquet(f)
if os.environ.get('MAIN') == '1':
    meta = pd.read_parquet(f'{D}/race_meta.parquet')[['race_id', 'race_num']]
    df = df.merge(meta, on='race_id')
    df = df[df.race_num == 11]
pay = pd.read_parquet(f'{D}/payouts.parquet')
fk = pay[pay.bet_type == '複勝'].copy()
fk['horse_number'] = fk.horse_numbers.apply(lambda x: x[0])
df = df.merge(fk[['race_id', 'horse_number', 'payout']], on=['race_id', 'horse_number'], how='left')
df['payout'] = df.payout.fillna(0)
df['pm'] = df[col]
df['pq'] = df.p_market
df['ratio'] = df.pm / df.pq
df['rk'] = df.groupby('race_id').pm.rank(ascending=False, method='first')


def summ(d):
    return pd.Series({'n': len(d), 'model_p': d.pm.mean(), 'market_p': d.pq.mean(), 'actual': d.top3.mean(),
                      'fuku_roi': d.payout.sum() / (len(d) * 100)})


df['popg'] = pd.cut(df.popularity, [0, 3, 6, 9, 30], labels=['1-3番人気', '4-6番人気', '7-9番人気', '10番人気以下'])
print('=== 式の順位（レース内） x 人気')
print(df[df.rk <= 3].groupby(['rk', 'popg'], observed=True).apply(summ, include_groups=False).round(3).to_string())
print('=== 式とオッズの比（式の3着内確率 ÷ オッズから見た3着内確率）の10段階')
df['rq'] = pd.qcut(df.ratio, 10, labels=False)
print(df.groupby('rq').apply(summ, include_groups=False).assign(
    ratio_mean=df.groupby('rq').ratio.mean()).round(3).to_string())
print('=== 式の3着内確率が40%以上なのに7番人気以下（逸れた見方）')
s = df[(df.pm >= 0.4) & (df.popularity >= 7)]
print(summ(s).round(3).to_dict() if len(s) else 'none')
s = df[(df.pm >= 0.3) & (df.popularity >= 7)]
print('30%以上・7番人気以下', summ(s).round(3).to_dict() if len(s) else 'none')
