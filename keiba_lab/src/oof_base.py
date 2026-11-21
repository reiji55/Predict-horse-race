"""答えを見ない基礎スコア（年ごとの抜き取り）。fold の年は、その年を含まない 2011–2023 で学習した式で予測する。
2024–26 は 2011–2023 全部で学習。材料は chain4 と同じ。固定 650 回。out/oof_base.parquet"""
import json
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd

from models_lib import load_features

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
extra = sys.argv[1].split(',')
F, FUND = load_features(f'{D}/features_v3.parquet')
FEATS = [c for c in FUND if c not in ODDS]
KEEP = ['race_id', 'horse_number', 'year', 'finish', 'field', 'race_num']
F = F[list(dict.fromkeys(KEEP + FEATS))].copy()      # 省メモリ：使う列だけ、数値は32ビット
F[FEATS] = F[FEATS].astype(np.float32)
if extra:
    import gc
    import os
    import pyarrow.parquet as pq
    F['race_id'] = F.race_id.astype(str)
    for fname in ('features_x', 'features_pos', 'features_x2', 'features_x3', 'features_sub', 'features_x4',
                  'features_x5', 'features_x6', 'features_x7', 'features_x8', 'features_x9'):
        if not os.path.exists(f'{D}/{fname}.parquet'):
            continue
        have = [c for c in extra if c in pq.read_schema(f'{D}/{fname}.parquet').names and c not in F.columns]
        if have:
            X = pd.read_parquet(f'{D}/{fname}.parquet', columns=['race_id', 'horse_number'] + have)
            X['race_id'] = X.race_id.astype(str)
            X[have] = X[have].astype(np.float32)
            F = F.merge(X, on=['race_id', 'horse_number'], how='left')
            del X
            gc.collect()
    g = F.groupby('race_id')
    rel = pd.DataFrame({f'{c}_rel': (F[c] - g[c].transform('mean')).astype(np.float32) for c in extra}, index=F.index)
    F = pd.concat([F, rel], axis=1)
    del rel
    gc.collect()
    FEATS = FEATS + extra + [f'{c}_rel' for c in extra]
F['top3'] = (F.finish <= 3).astype(float)
p = dict(objective='binary', learning_rate=0.04, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
         bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=1, verbosity=-1)
ok = F.finish.notna()
F['s'] = np.nan
for lo, hi in [(2014, 2016), (2017, 2019), (2020, 2022), (2023, 2023), (2024, 2026)]:
    test = F.year.between(lo, hi)
    train = F.year.between(2011, 2023) & ~test & ok
    b = lgb.train(p, lgb.Dataset(F.loc[train, FEATS].values.astype(np.float32), F.loc[train, 'top3'].values), 650)
    F.loc[test, 's'] = b.predict(F.loc[test, FEATS].values.astype(np.float32), raw_score=True)
    d = F[test & ok & (F.field >= 8)]
    pr = 1 / (1 + np.exp(-d.s))
    print(json.dumps({'fold': [lo, hi], 'logloss': round(float(-np.mean(d.top3 * np.log(pr) + (1 - d.top3) * np.log(1 - pr))), 5)}), flush=True)
    F.loc[F.year >= 2014, ['race_id', 'horse_number', 'year', 'finish', 'field', 'race_num', 'top3', 's']].to_parquet(f'{OUT}/oof_base.parquet')
print('DONE', flush=True)
