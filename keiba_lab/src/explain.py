"""Where does the model disagree with the market, and was it right?  (dev 2019-2023 + holdout 2024-2026)
For buckets of (ability-rating rank in field) x (odds band): market-expected wins vs actual wins vs model-expected wins.
"""
import numpy as np
import pandas as pd
from common import D, OUT

F = pd.read_parquet(f'{D}/features.parquet', columns=['race_id', 'horse_number', 'rating_rk', 'rating_rel', 'days_off',
                                                      'horse_weight_rel', 'style_min5', 'trn_mratio', 'n_runs', 'odds', 'y'])
P = pd.concat([pd.read_parquet(f'{OUT}/preds_dev.parquet')[['race_id', 'horse_number', 'year', 'p_mkt', 'p_res']],
               pd.read_parquet(f'{OUT}/preds_holdout.parquet')])
df = P.merge(F, on=['race_id', 'horse_number'])
df['period'] = np.where(df.year <= 2023, 'dev', 'holdout')
df['oband'] = pd.cut(df.odds, [1, 5, 10, 20, 50, 1000], labels=['<5', '5-10', '10-20', '20-50', '50+'])
df['rband'] = pd.cut(df.rating_rk, [0, .5, .8, .95, 1.0], labels=['bottom half', '50-80%', '80-95%', 'top 5%'])
df.loc[df.rating_rk.isna(), 'rband'] = np.nan


def summ(d):
    return pd.Series({'n': len(d), 'mkt_exp': d.p_mkt.sum(), 'model_exp': d.p_res.sum(), 'wins': d.y.sum(),
                      'act/mkt': d.y.sum() / d.p_mkt.sum(), 'model/mkt': d.p_res.sum() / d.p_mkt.sum(),
                      'win_roi': (d.y * d.odds).sum() / len(d)})


for per in ('dev', 'holdout'):
    print(f'===== {per}: rating rank in field x odds band  (act/mkt = actual wins / market-expected wins)')
    t = df[df.period == per].groupby(['rband', 'oband'], observed=True).apply(summ, include_groups=False)
    print(t[['n', 'wins', 'act/mkt', 'model/mkt', 'win_roi']].round(3).to_string())
# model's biggest upgrades vs downgrades
df['r'] = df.p_res / df.p_mkt
df['rq'] = pd.qcut(df.r, 10, labels=False)
print('===== deciles of model/market ratio (all periods split)')
print(df.groupby(['period', 'rq']).apply(summ, include_groups=False)[['n', 'model/mkt', 'act/mkt', 'win_roi']].round(3).to_string())
