"""A second, different formula: the 'longshot detector' (穴専用の式).
Among the horses the main odds-free formula ranks 6th or lower, which ones finish top 3?
Train on 2023-2024 (main-formula scores there are out-of-sample), test on 2025-2026.
Compare: within each race's 'outside the top 5' horses, the detector's No.1 vs the main formula's own No.6.
"""
import json

import lightgbm as lgb
import numpy as np
import pandas as pd

from models_lib import load_features

D = '/home/claude/keiba_lab/data'
OUT = '/home/claude/keiba_lab/out'
ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
F, FUND = load_features(f'{D}/features_v3.parquet')
FEATS = [c for c in FUND if c not in ODDS]
F = F[F.year >= 2023]
m = pd.concat([pd.read_parquet(f'{OUT}/top3_dev2023_v3.parquet', columns=['race_id', 'horse_number', 'p_m0']),
               pd.read_parquet(f'{OUT}/top3_final_v3_holdout.parquet', columns=['race_id', 'horse_number', 'p_m0'])])
F = F.merge(m, on=['race_id', 'horse_number'], how='inner')
F = F[F.field >= 8 & F.finish.notna()] if False else F[(F.field >= 8) & F.finish.notna()]
F['rank_main'] = F.groupby('race_id').p_m0.rank(ascending=False, method='first')
F['top3'] = (F.finish <= 3).astype(int)

# how often does the top 3 include a horse from outside the main top 5?
g = F[F.top3 == 1].groupby('race_id').rank_main
outside = (g.max() > 5)
print('races where at least one of the top 3 was outside the main top 5:', round(outside.mean(), 3),
      ' two or more:', round((F[F.top3 == 1].assign(o=lambda d: d.rank_main > 5).groupby('race_id').o.sum() >= 2).mean(), 3))
print('top-3 rate by main rank:', F.groupby(F.rank_main.clip(upper=12)).top3.mean().round(3).to_dict())

S = F[F.rank_main >= 6].copy()
X_COLS = FEATS + ['p_m0', 'rank_main']
tr_all = S[S.year.between(2023, 2024)]
rids = tr_all.race_id.unique()
rng = np.random.default_rng(1)
va_ids = set(rng.choice(rids, size=len(rids) // 6, replace=False))
tr = tr_all[~tr_all.race_id.isin(va_ids)]
va = tr_all[tr_all.race_id.isin(va_ids)]
te = S[S.year >= 2025].copy()
p = dict(objective='binary', learning_rate=0.02, num_leaves=15, min_data_in_leaf=300, feature_fraction=0.6,
         bagging_fraction=0.8, bagging_freq=1, lambda_l2=20.0, num_threads=2, seed=1, verbosity=-1)
b = lgb.train(p, lgb.Dataset(tr[X_COLS].values.astype(np.float32), tr.top3.values), 3000,
              valid_sets=[lgb.Dataset(va[X_COLS].values.astype(np.float32), va.top3.values)],
              callbacks=[lgb.early_stopping(150, verbose=False)])
te['s_ana'] = b.predict(te[X_COLS].values.astype(np.float32), raw_score=True)
print('detector iterations:', b.best_iteration, ' train rows', len(tr), ' test rows', len(te))


def auc(y, s):
    o = np.argsort(s)
    rk = np.empty(len(s))
    rk[o] = np.arange(1, len(s) + 1)
    n1 = y.sum()
    return (rk[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))


# within-race AUC among outside-top-5 horses (average over races with at least one top-3 horse there)
res = {'detector': [], 'main': []}
pick = {'detector_1': [], 'main_1': [], 'detector_2': [], 'main_2': []}
for rid, gg in te.groupby('race_id'):
    y = gg.top3.values
    for k, col in (('detector', 's_ana'), ('main', 'p_m0')):
        if 0 < y.sum() < len(y):
            res[k].append(auc(y, gg[col].values))
        order = gg.sort_values(col, ascending=False).top3.values
        pick[f'{k}_1'].append(order[0])
        pick[f'{k}_2'].append(order[:2].sum() > 0)
print('within-race AUC (outside top 5):', {k: round(float(np.mean(v)), 4) for k, v in res.items()})
print('pick No.1 of the rest -> top 3 rate:', {k: round(float(np.mean(v)), 4) for k, v in pick.items()})
te11 = te[te.race_id.str[-2:] == '11']
p11 = {'detector': [], 'main': []}
for rid, gg in te11.groupby('race_id'):
    for k, col in (('detector', 's_ana'), ('main', 'p_m0')):
        p11[k].append(gg.sort_values(col, ascending=False).top3.values[0])
print('11R only, pick No.1 of the rest -> top 3:', {k: round(float(np.mean(v)), 4) for k, v in p11.items()}, len(p11['main']))
sir = te[te.race_id == '202609040811'].copy()
sir['rank_ana'] = sir.s_ana.rank(ascending=False).astype(int)
print('Sirius S, outside main top 5:')
print(sir.sort_values('rank_ana')[['horse_number', 'finish', 'rank_main', 'rank_ana']].to_string(index=False))
imp = pd.Series(b.feature_importance('gain'), index=X_COLS).sort_values(ascending=False)
print('detector top signals:', imp.head(10).round(0).to_dict())
te[['race_id', 'horse_number', 'year', 'finish', 'rank_main', 's_ana']].to_parquet(f'{OUT}/anaba_scores.parquet')
