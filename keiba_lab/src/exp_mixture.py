"""ペースで条件づけた式に、予測したペースの分布を代入して平均する（掛け合わせ）。
  式M：3着以内か ~ 材料（chain と同じ）＋ 実際のペース t_pace（学習時だけ答えのペースを入れる）
  予測：P = Σ_k w_k · M(材料, ペース = 予測ペース + 誤差の幅 × q_k)（5点の正規分布の近似）
  上限の参考：M(材料, 実際のペース)（事前には分からない）
物差しは exp_harness と同じ（2011–22学習、2023止めどき、2024–26・8頭立て以上）。"""
import json
import os

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from models_lib import load_features

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
F, FUND = load_features(f'{D}/features_v5.parquet')
F['race_id'] = F.race_id.astype(str)
FEATS = [c for c in FUND if c not in ODDS]
for fname in ('features_x3', 'features_sub'):
    X = pd.read_parquet(f'{D}/{fname}.parquet')
    X['race_id'] = X.race_id.astype(str)
    F = F.merge(X, on=['race_id', 'horse_number'], how='left')
    FEATS += [c for c in X.columns if c not in ('race_id', 'horse_number')]
races = pd.read_parquet(f'{D}/races.parquet', columns=['race_id', 'surface', 'distance', 'pace_first3f'])
races['race_id'] = races.race_id.astype(str)
g = races.groupby(['surface', 'distance']).pace_first3f
races['t_pace'] = ((races.pace_first3f - g.transform('median')) / g.transform('std')).clip(-3, 3)
F = F.merge(races[['race_id', 't_pace']], on='race_id', how='left')
rr = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'last_corner', 'first_corner'])
rr['race_id'] = rr.race_id.astype(str)
F = F.merge(rr, on=['race_id', 'horse_number'], how='left')
F['t_pos4'] = F.last_corner / F.field
F['t_pos1'] = F.first_corner / F.field
COND = os.environ.get('COND', 't_pace')
PRED = {'t_pace': 'sub_pace', 't_pos4': 'sub_pos4', 't_pos1': 'sub_pos1'}[COND]
LO, HI = {'t_pace': (-3, 3), 't_pos4': (0.0, 1.0), 't_pos1': (0.0, 1.0)}[COND]
F['top3'] = (F.finish <= 3).astype(float)
MF = FEATS + [COND]
tr, va = F[F.year.between(2011, 2022)], F[F.year == 2023]
te = F[(F.year >= 2024) & (F.field >= 8) & F.finish.notna()].copy()
p = dict(objective='binary', learning_rate=0.04, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
         bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2,
         seed=int(os.environ.get('SEED', 1)), verbosity=-1)
b = lgb.train(p, lgb.Dataset(tr[MF].values.astype(np.float32), tr.top3.values), 4000,
              valid_sets=[lgb.Dataset(va[MF].values.astype(np.float32), va.top3.values)],
              callbacks=[lgb.early_stopping(150, verbose=False)])
resid_sd = float(np.nanstd((va[COND] - va[PRED]) if COND != 't_pace' else va.drop_duplicates('race_id').eval('t_pace - sub_pace')))
Q = [(-1.6, 0.1), (-0.7, 0.2), (0.0, 0.4), (0.7, 0.2), (1.6, 0.1)]
X = te[MF].copy()
mix = np.zeros(len(te))
for q, wq in Q:
    X[COND] = (te[PRED] + resid_sd * q).clip(LO, HI)
    mix += wq * b.predict(X.values.astype(np.float32))
X[COND] = te[PRED]
point = b.predict(X.values.astype(np.float32))
oracle = b.predict(te[MF].values.astype(np.float32))
out = te[['race_id', 'horse_number', 'year', 'finish', 'top3', 'race_num']].copy()
tag = os.environ.get('TAG', 'mix')
for name, pr in (('mix', mix), ('point', point), ('oracle', oracle)):
    out['s'] = np.log(pr / (1 - pr))
    out.to_parquet(f'{OUT}/exp_{tag}_{name}.parquet')
    ll = -np.mean(out.top3 * np.log(pr) + (1 - out.top3) * np.log(1 - pr))
    d = out.assign(rk=out.groupby('race_id').s.rank(ascending=False, method='first'))
    gg = d.groupby('race_id')
    in5 = gg.apply(lambda x: x.loc[x.rk <= 5, 'top3'].sum(), include_groups=False)
    print(json.dumps({'tag': f'{tag}_{name}', 'resid_sd': round(resid_sd, 3), 'iters': b.best_iteration,
                      'logloss': round(float(ll), 5),
                      'no1_top3': round(float(gg.apply(lambda x: x.loc[x.rk == 1, 'top3'].sum(), include_groups=False).mean()), 4),
                      'top3_in_top5': round(float(in5.mean()), 4), 'all3_in_top5': round(float((in5 == 3).mean()), 4)},
                     ensure_ascii=False), flush=True)
