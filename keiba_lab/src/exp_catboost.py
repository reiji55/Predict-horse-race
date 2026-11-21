"""CatBoost（騎手・調教師・馬主・競馬場などを名前のまま扱い、組み合わせも自動で学ぶ）。
材料は exp_harness の chain2 と同じ＋カテゴリ。物差しも同じ（2011–22学習、2023止めどき、2024–26・8頭立て以上）。"""
import json
import os
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from catboost import CatBoostClassifier, Pool

from models_lib import load_features
from pre_lib import FAST, PRE, MODELS

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
extra = sys.argv[1].split(',')
tag = sys.argv[2]
F, FUND = load_features(f'{D}/features_v3.parquet')
F['race_id'] = F.race_id.astype(str)
FEATS = [c for c in FUND if c not in ODDS]
for fname in ('features_x', 'features_pos', 'features_x2', 'features_x3', 'features_sub', 'features_x4', 'features_x5', 'features_x11', 'features_x12'):
    if not os.path.exists(f'{D}/{fname}.parquet'):
        continue
    have = [c for c in extra if c in pq.read_schema(f'{D}/{fname}.parquet').names]
    if have:
        X = pd.read_parquet(f'{D}/{fname}.parquet', columns=['race_id', 'horse_number'] + have)
        X['race_id'] = X.race_id.astype(str)
        F = F.merge(X, on=['race_id', 'horse_number'], how='left')
for c in extra:
    F[f'{c}_rel'] = F[c] - F.groupby('race_id')[c].transform('mean')
FEATS = [c for c in FEATS + extra + [f'{c}_rel' for c in extra] if c not in PRE]
own = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'owner_id'])
own['race_id'] = own.race_id.astype(str)
F = F.merge(own, on=['race_id', 'horse_number'], how='left')
CATS = ['jockey_id', 'trainer_id', 'owner_id', 'venue_code', 'course_code']
for c in CATS:
    F[c + '_cat'] = pd.factorize(F[c].astype(str))[0].astype(np.int32)
CATN = [c + '_cat' for c in CATS]
COLS = list(dict.fromkeys([c for c in FEATS if c not in CATS] + CATN))
F['top3'] = (F.finish <= 3).astype(int)
import gc
NUM = [c for c in COLS if c not in CATN]
F[NUM] = F[NUM].astype(np.float32)
keep = list(dict.fromkeys(['race_id', 'horse_number', 'year', 'finish', 'top3', 'race_num', 'field'] + COLS))
F = F[keep]
gc.collect()
masks = {'tr': F.year.between(2011, 2022), 'va': F.year == 2023,
         'te': (F.year >= 2024) & (F.field >= 8) & F.finish.notna()}
te = F.loc[masks['te'], ['race_id', 'horse_number', 'year', 'finish', 'top3', 'race_num']].copy()


def pool(name, label=True):
    d = F.loc[masks[name], COLS]
    return Pool(d, F.loc[masks[name], 'top3'].values if label else None, cat_features=CATN)


m = CatBoostClassifier(iterations=3000, learning_rate=0.05, depth=7, l2_leaf_reg=10, border_count=64,
                       loss_function='Logloss', thread_count=2, random_seed=int(os.environ.get('SEED', 1)),
                       od_type='Iter', od_wait=150, verbose=250, allow_writing_files=False, has_time=True)
if FAST:
    m.load_model(f'{MODELS}/m_{tag}.cbm')
    assert list(m.feature_names_) == COLS, '保存した式と材料の並びが違う（作り直しが必要）'
else:
    ptr, pva = pool('tr'), pool('va')
    m.fit(ptr, eval_set=pva, use_best_model=True)
    m.save_model(f'{MODELS}/m_{tag}.cbm')
    del ptr, pva
gc.collect()
out = te
out['s'] = m.predict(pool('te', False), prediction_type='RawFormulaVal')
out.to_parquet(f'{OUT}/exp_{tag}.parquet')
if os.path.exists(f'{OUT}/pick_ids.txt'):
    _ids = set(open(f'{OUT}/pick_ids.txt').read().split())
    masks['pk'] = F.race_id.astype(str).isin(_ids)
    if masks['pk'].sum():
        _o = F.loc[masks['pk'], ['race_id', 'horse_number', 'field', 'race_num']].copy()
        _o['s'] = m.predict(pool('pk', False), prediction_type='RawFormulaVal')
        _o.to_parquet(f'{OUT}/pick_{tag}.parquet')
pr = 1 / (1 + np.exp(-out.s))
ll = -np.mean(out.top3 * np.log(pr) + (1 - out.top3) * np.log(1 - pr))
d = out.assign(rk=out.groupby('race_id').s.rank(ascending=False, method='first'))
g = d.groupby('race_id')
in5 = g.apply(lambda x: x.loc[x.rk <= 5, 'top3'].sum(), include_groups=False)
imp = pd.Series(m.get_feature_importance(), index=COLS).sort_values(ascending=False)
print(json.dumps({'tag': tag, 'iters': m.get_best_iteration(), 'logloss': round(float(ll), 5),
                  'no1_top3': round(float(g.apply(lambda x: x.loc[x.rk == 1, 'top3'].sum(), include_groups=False).mean()), 4),
                  'top3_in_top5': round(float(in5.mean()), 4), 'all3_in_top5': round(float((in5 == 3).mean()), 4),
                  'cat_importance': {c: round(float(imp[c]), 2) for c in CATN}}, ensure_ascii=False), flush=True)
