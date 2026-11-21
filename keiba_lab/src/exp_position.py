"""位置取りの予測を材料にする（2段の式）。
段1：各馬の1角・4角の位置（頭数比、0=先頭）を、発走前の材料だけで予測する回帰。
     学習年の予測は「その年を抜いて学んだ式」で出す（年ごとの抜き取り＝自分の答えを見ない）。
段2：予測位置と、そこから作るレースの形（前に行きそうな馬の数、その馬の予測位置の順位など）を
     3着以内の式に足す。物差しは exp_harness と同じ。"""
import json

import lightgbm as lgb
import numpy as np
import pandas as pd

from models_lib import load_features
from pre_lib import FAST, keep, save_lgb, load_lgb

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
F, FUND = load_features(f'{D}/features_v3.parquet')
FEATS = keep([c for c in FUND if c not in ODDS])
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'first_corner', 'last_corner'])
r['race_id'] = r.race_id.astype(str); F['race_id'] = F.race_id.astype(str)
F = F.merge(r, on=['race_id', 'horse_number'], how='left')
F['pos1'] = F.first_corner / F.field
F['pos4'] = F.last_corner / F.field
pp = dict(objective='regression', learning_rate=0.08, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
          bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=1, verbosity=-1)
folds = [(2011, 2013), (2014, 2016), (2017, 2019), (2020, 2022)]
for tgt in ('pos1', 'pos4'):
    F[f'pred_{tgt}'] = np.nan
    ok = F[tgt].notna()
    if FAST:                                 # 2022年までの行は前回の値、2023年以降は保存した式
        old = pd.read_parquet(f'{D}/features_pos.parquet', columns=['race_id', 'horse_number', f'pred_{tgt}'])
        old['race_id'] = old.race_id.astype(str)
        F[f'pred_{tgt}'] = F[['race_id', 'horse_number']].merge(old, on=['race_id', 'horse_number'], how='left')[f'pred_{tgt}'].values
        test = F.year >= 2023
        F.loc[test, f'pred_{tgt}'] = load_lgb(f'pos_{tgt}', FEATS).predict(F.loc[test, FEATS].values.astype(np.float32))
        assert F.loc[F.year < 2023, f'pred_{tgt}'].notna().all()
    for lo, hi in ([] if FAST else folds + [(2023, 2026)]):
        test = F.year.between(lo, hi)
        train = F.year.between(2011, 2022) & ~test & ok
        b = lgb.train(pp, lgb.Dataset(F.loc[train, FEATS].values.astype(np.float32), F.loc[train, tgt].values), 400)
        F.loc[test, f'pred_{tgt}'] = b.predict(F.loc[test, FEATS].values.astype(np.float32))
        if lo == 2023:
            save_lgb(b, f'pos_{tgt}', FEATS)
    m = F.year >= 2024
    c = np.corrcoef(F.loc[m & ok, tgt], F.loc[m & ok, f'pred_{tgt}'])[0, 1]
    print(json.dumps({'target': tgt, 'r2_2024_26': round(float(c ** 2), 3)}), flush=True)
g = F.groupby('race_id')
F['pred_pos1_rk'] = g.pred_pos1.rank() / F.field
F['pred_pos4_rk'] = g.pred_pos4.rank() / F.field
F['n_pred_front'] = g.pred_pos1.transform(lambda s: (s < 0.25).sum())
F['pred_front_gap'] = F.pred_pos1 - g.pred_pos1.transform('min')
NEW = ['pred_pos1', 'pred_pos4', 'pred_pos1_rk', 'pred_pos4_rk', 'n_pred_front', 'pred_front_gap']
F[['race_id', 'horse_number'] + NEW].to_parquet(f'{D}/features_pos.parquet')
