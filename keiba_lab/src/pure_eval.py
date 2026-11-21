"""Evaluate top-3 probabilities from stage scores (s1, s2, s3) — odds-free models — against reality.
usage: python pure_eval.py TAG SCORE_FILE [SCORE_FILE2 ...]   (each file: race_id, horse_number, year, s1, s2, s3)
Prints, per model and for the market (discounted Harville from final odds, for reference only):
  binary log loss / Brier for 'finished top 3', calibration by probability band,
  how often the model's No.1 / No.2 / No.3 horse finished top 3, and AUC.
Writes out/top3_<TAG>.parquet with p_top3 per model.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

from common import D, OUT

GQ, DQ = 0.838, 0.737
tag = sys.argv[1]
files = sys.argv[2:]
MAIN_ONLY = os.environ.get('MAIN') == '1'

runs = pd.read_parquet(f'{D}/runs.parquet')[['race_id', 'horse_number', 'finish', 'mkt_p', 'race_num', 'dead_heat_win',
                                              'field', 'odds', 'popularity']]
base = None
for k, fpath in enumerate(files):
    sc = pd.read_parquet(fpath)[['race_id', 'horse_number', 'year', 's1', 's2', 's3']]
    sc = sc.rename(columns={'s1': f's1_{k}', 's2': f's2_{k}', 's3': f's3_{k}'})
    base = sc if base is None else base.merge(sc.drop(columns='year'), on=['race_id', 'horse_number'])
df = base.merge(runs, on=['race_id', 'horse_number'])
df = df[~df.dead_heat_win]
if MAIN_ONLY:
    df = df[df.race_num == 11]
df = df.sort_values(['race_id', 'horse_number']).reset_index(drop=True)


def top3_probs(p1, e2, e3):
    n = len(p1)
    P2 = p1[:, None] * e2[None, :] / (e2.sum() - e2[:, None])
    np.fill_diagonal(P2, 0)
    den = e3.sum() - e3[:, None, None] - e3[None, :, None]
    P3 = P2[:, :, None] * e3[None, None, :] / np.where(den > 1e-300, den, np.nan)
    ix = np.arange(n)
    P3[ix, ix, :] = 0
    P3[ix, :, ix] = 0
    P3[:, ix, ix] = 0
    P3 = np.nan_to_num(P3)
    return p1 + P2.sum(axis=0) + P3.sum(axis=(0, 1))


models = [f'm{k}' for k in range(len(files))] + ['market']
out = {m: np.zeros(len(df)) for m in models}
starts = np.flatnonzero(np.r_[True, df.race_id.values[1:] != df.race_id.values[:-1]])
ends = np.r_[starts[1:], len(df)]
for a, b in zip(starts, ends):
    if b - a < 4:
        for m in models:
            out[m][a:b] = np.nan
        continue
    for k in range(len(files)):
        s1 = df[f's1_{k}'].values[a:b]
        s2 = df[f's2_{k}'].values[a:b]
        s3 = df[f's3_{k}'].values[a:b]
        p1 = np.exp(s1 - s1.max())
        p1 /= p1.sum()
        out[f'm{k}'][a:b] = top3_probs(p1, np.exp(s2 - s2.max()), np.exp(s3 - s3.max()))
    q = df.mkt_p.values[a:b] / df.mkt_p.values[a:b].sum()
    out['market'][a:b] = top3_probs(q, q ** GQ, q ** DQ)
for m in models:
    df[f'p_{m}'] = out[m]
df = df[df[f'p_m0'].notna()].copy()
df['top3'] = (df.finish <= 3).astype(float)
df['p_uniform'] = np.minimum(3, df.field) / df.field


def auc(y, s):
    o = np.argsort(s)
    r = np.empty(len(s))
    r[o] = np.arange(1, len(s) + 1)
    n1 = y.sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))


rep = {'tag': tag, 'races': int(df.race_id.nunique()), 'horses': len(df), 'files': files}
y = df.top3.values
for m in models + ['uniform']:
    p = np.clip(df[f'p_{m}'].values, 1e-6, 1 - 1e-6)
    r = {'logloss': round(float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()), 5),
         'brier': round(float(((p - y) ** 2).mean()), 5)}
    if m != 'uniform':
        r['auc'] = round(float(auc(y, p)), 4)
        rk = df.groupby('race_id')[f'p_{m}'].rank(ascending=False, method='first')
        for k in (1, 2, 3):
            r[f'rank{k}_top3_rate'] = round(float(df.top3[rk == k].mean()), 4)
        r['top3_of_top3'] = round(float(df.top3[rk <= 3].sum() / df.race_id.nunique()), 3)   # avg # of top-3 finishers among model's top 3
    rep[m] = r
    print(m, r, flush=True)
# calibration table for model 0
b = pd.cut(df.p_m0, [0, .05, .1, .2, .3, .4, .5, .6, .7, .8, 1.0])
cal = df.groupby(b, observed=True).agg(n=('top3', 'size'), pred=('p_m0', 'mean'), actual=('top3', 'mean'))
print(cal.round(4).to_string())
rep['calibration_m0'] = {str(k): {kk: round(float(vv), 4) for kk, vv in v.items()} for k, v in cal.iterrows()}
# favourite (market) reference
fav = df[df.popularity == 1]
rep['market_favourite_top3_rate'] = round(float(fav.top3.mean()), 4)
print('market favourite top3 rate', rep['market_favourite_top3_rate'])
df[['race_id', 'horse_number', 'year', 'finish', 'top3', 'odds', 'popularity', 'field'] + [f'p_{m}' for m in models]] \
    .to_parquet(f'{OUT}/top3_{tag}.parquet')
with open(f'{OUT}/top3_{tag}.json', 'w') as f:
    json.dump(rep, f, ensure_ascii=False, indent=1)
