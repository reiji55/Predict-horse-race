"""同じ物差しで材料の効き目を比べる。二値LightGBM（3着以内か）を2011–2022で学習、2023で止めどき、
2024–2026（8頭立て以上）で測る。usage: python exp_harness.py TAG [extra_col,...|none] [WEIGHT]"""
import json
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd

from models_lib import load_features
from pre_lib import FAST, PRE, save_lgb, load_lgb

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
tag = sys.argv[1]
extra = [] if len(sys.argv) < 3 or sys.argv[2] == 'none' else sys.argv[2].split(',')
weight = sys.argv[3] if len(sys.argv) > 3 else 'none'
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
                  'features_x5', 'features_x6', 'features_x7', 'features_x8', 'features_x9', 'features_x10', 'features_x11', 'features_x12'):
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
_drop = [c for c in __import__('os').environ.get('DROP', '').split(',') if c]      # 例 DROP=horse_weight,weight_change：前日には分からない材料を外す
if _drop:
    FEATS = [c for c in FEATS if c not in _drop and not (c.endswith('_rel') and c[:-4] in _drop)]
FEATS = [c for c in FEATS if c not in PRE]
F['top3'] = (F.finish <= 3).astype(float)
tr, va, te = F[F.year.between(2011, 2022)], F[F.year == 2023], F[(F.year >= 2024) & (F.field >= 8) & F.finish.notna()]
w = None
if weight.startswith('recent'):
    half = float(weight[6:] or 5)          # half-life in years
    w = 0.5 ** ((2022 - tr.year.values) / half)
p = dict(objective='binary', learning_rate=0.04, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
         bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=int(__import__('os').environ.get('SEED', 1)), verbosity=-1)
SOFT = __import__('os').environ.get('SOFT')     # 例 SOFT=0.5：学習の目標を 0.5×実際の結果 + 0.5×市場が見た3着内確率 にする
ytr = tr.top3.values
if SOFT is not None:
    lam = float(SOFT)
    Q = pd.read_parquet(f'{D}/mkt_top3.parquet')
    Q['race_id'] = Q.race_id.astype(str)
    q3 = tr[['race_id', 'horse_number']].astype({'race_id': str}).merge(Q, on=['race_id', 'horse_number'], how='left').q3.values
    ytr = np.where(np.isnan(q3), ytr, lam * ytr + (1 - lam) * q3)
    p['objective'] = 'cross_entropy'
dtr = lgb.Dataset(tr[FEATS].values.astype(np.float32), ytr, weight=w)
dva = lgb.Dataset(va[FEATS].values.astype(np.float32), va.top3.values, reference=dtr)
import os
SPLIT = os.environ.get('SPLIT')          # 例 SPLIT=surf_code：条件ごとに別々の式を作る
if SPLIT:
    te = te.copy()
    te['s'] = np.nan
    for val in sorted(F[SPLIT].dropna().unique()):
        mtr, mva, mte = tr[SPLIT] == val, va[SPLIT] == val, te[SPLIT] == val
        if mte.sum() == 0 or mtr.sum() < 5000:
            continue
        wv = None if w is None else w[mtr.values]
        d1 = lgb.Dataset(tr.loc[mtr, FEATS].values.astype(np.float32), tr.loc[mtr, 'top3'].values, weight=wv)
        d2 = lgb.Dataset(va.loc[mva, FEATS].values.astype(np.float32), va.loc[mva, 'top3'].values, reference=d1)
        b = lgb.train(p, d1, 4000, valid_sets=[d2], callbacks=[lgb.early_stopping(150, verbose=False)])
        te.loc[mte, 's'] = b.predict(te.loc[mte, FEATS].values.astype(np.float32), raw_score=True)
    te = te[te.s.notna()][['race_id', 'horse_number', 'year', 'finish', 'top3', 'race_num', 's']].copy()
else:
    if FAST:
        b = load_lgb(f'm_{tag}', FEATS)
        b.best_iteration = b.current_iteration()
    else:
        b = lgb.train(p, dtr, 4000, valid_sets=[dva], callbacks=[lgb.early_stopping(150, verbose=False)])
        save_lgb(b, f'm_{tag}', FEATS)
    te = te[['race_id', 'horse_number', 'year', 'finish', 'top3', 'race_num']].copy()
    te['s'] = b.predict(F.loc[te.index, FEATS].values.astype(np.float32), raw_score=True)
te.to_parquet(f'{OUT}/exp_{tag}.parquet')
if os.path.exists(f'{OUT}/pick_ids.txt') and not SPLIT:      # まだ走っていないレース（picks_ingest.py が一覧を書く）の見立て
    _ids = set(open(f'{OUT}/pick_ids.txt').read().split())
    _pk = F[F.race_id.astype(str).isin(_ids)]
    if len(_pk):
        _o = _pk[['race_id', 'horse_number', 'field', 'race_num']].copy()
        _o['s'] = b.predict(_pk[FEATS].values.astype(np.float32), raw_score=True)
        _o.to_parquet(f'{OUT}/pick_{tag}.parquet')
pr = 1 / (1 + np.exp(-te.s))
ll = -np.mean(te.top3 * np.log(pr) + (1 - te.top3) * np.log(1 - pr))
te['rk'] = te.groupby('race_id').s.rank(ascending=False, method='first')
g = te.groupby('race_id')
top1 = g.apply(lambda d: d.loc[d.rk == 1, 'top3'].sum(), include_groups=False).mean()
in5 = g.apply(lambda d: d.loc[d.rk <= 5, 'top3'].sum(), include_groups=False)
m11 = te.race_num == 11
g11 = te[m11].groupby('race_id')
res = {'tag': tag, 'iters': b.best_iteration, 'n_feats': len(FEATS), 'logloss': round(float(ll), 5),
       'no1_top3': round(float(top1), 4), 'top3_in_top5': round(float(in5.mean()), 4),
       'all3_in_top5': round(float((in5 == 3).mean()), 4),
       'no1_top3_11R': round(float(g11.apply(lambda d: d.loc[d.rk == 1, 'top3'].sum(), include_groups=False).mean()), 4),
       'all3_in_top5_11R': round(float((g11.apply(lambda d: d.loc[d.rk <= 5, 'top3'].sum(), include_groups=False) == 3).mean()), 4)}
if extra and not SPLIT:
    imp = pd.Series(b.feature_importance('gain'), index=FEATS)
    (imp / imp.sum()).sort_values(ascending=False).round(5).to_json(f'{OUT}/imp_{tag}.json')
    res['extra_gain_share'] = round(float(imp[[c for c in FEATS if any(c.startswith(e) for e in extra)]].sum() / imp.sum()), 4)
print(json.dumps(res, ensure_ascii=False), flush=True)
