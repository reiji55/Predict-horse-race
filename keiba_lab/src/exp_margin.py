"""着差を学ばせる：1レースの全頭について「勝ち馬から何秒差か（距離で割って1000mあたり）」を当てる回帰。
大差負け（3秒超）は3秒で頭打ち。2011–2022学習、2023で止めどき、2024–2026で測る。
二値の式（exp_base）と順位を平均した合成も測る。"""
import json

import lightgbm as lgb
import numpy as np
import pandas as pd

from models_lib import load_features

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
F, FUND = load_features(f'{D}/features_v3.parquet')
FEATS = [c for c in FUND if c not in ODDS]
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'margin_sec'])
r['race_id'] = r.race_id.astype(str); F['race_id'] = F.race_id.astype(str)
F = F.merge(r, on=['race_id', 'horse_number'], how='left')
F['top3'] = (F.finish <= 3).astype(float)
F['tgt'] = (F.margin_sec.clip(0, 3) / F.distance * 1000)
fit = F.tgt.notna()
tr, va = F[F.year.between(2011, 2022) & fit], F[(F.year == 2023) & fit]
te = F[(F.year >= 2024) & (F.field >= 8) & F.finish.notna()]
p = dict(objective='huber', alpha=0.5, learning_rate=0.04, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
         bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=1, verbosity=-1)
dtr = lgb.Dataset(tr[FEATS].values.astype(np.float32), tr.tgt.values)
dva = lgb.Dataset(va[FEATS].values.astype(np.float32), va.tgt.values, reference=dtr)
b = lgb.train(p, dtr, 4000, valid_sets=[dva], callbacks=[lgb.early_stopping(150, verbose=False)])
out = te[['race_id', 'horse_number', 'year', 'finish', 'top3', 'race_num']].copy()
out['m'] = -b.predict(te[FEATS].values.astype(np.float32))
base = pd.read_parquet(f'{OUT}/exp_base.parquet')[['race_id', 'horse_number', 's']]
out = out.merge(base, on=['race_id', 'horse_number'])
out['rk_m'] = out.groupby('race_id').m.rank(ascending=False)
out['rk_b'] = out.groupby('race_id').s.rank(ascending=False)
out['mix'] = -(out.rk_m + out.rk_b)
out.to_parquet(f'{OUT}/exp_margin.parquet')


def ev(col, d):
    d = d.assign(rk=d.groupby('race_id')[col].rank(ascending=False, method='first'))
    g = d.groupby('race_id')
    top1 = g.apply(lambda x: x.loc[x.rk == 1, 'top3'].sum(), include_groups=False).mean()
    in5 = g.apply(lambda x: x.loc[x.rk <= 5, 'top3'].sum(), include_groups=False)
    return {'no1_top3': round(float(top1), 4), 'top3_in_top5': round(float(in5.mean()), 4),
            'all3_in_top5': round(float((in5 == 3).mean()), 4)}


for col in ('s', 'm', 'mix'):
    print(json.dumps({'model': {'s': 'binary(base)', 'm': 'margin', 'mix': 'binary+margin'}[col], 'iters': b.best_iteration,
                      'all': ev(col, out), '11R': ev(col, out[out.race_num == 11])}, ensure_ascii=False), flush=True)
