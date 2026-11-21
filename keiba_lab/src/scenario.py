"""Race-shape scenario formulas (odds-free) and multi-formula boxes.

Race shape (known only after the race, used ONLY to choose training races):
  shape = mean 4th-corner position (pct of field) of the top-3 finishers
  front race  : shape <= 0.25   (top 3 came from the front)
  closer race : shape >= 0.375  (top 3 came from behind)
Model F = binary top-3 GBM trained on front races; Model C = trained on closer races (train 2011-2022, early-stop 2023).
Both are applied to EVERY race of 2024-2026 (the shape of a future race is unknown).

Ticket strategies (3連複 boxes; a race is covered if its top-3 set is inside one of the boxes):
  main5          : main formula top-5 box (10 tickets)
  F4|C4          : F top-4 box + C top-4 box
  main4|F4|C4    : main top-4 + F top-4 + C top-4
  main6          : main top-6 (20 tickets)
  F5|C5          : F top-5 + C top-5
"""
import itertools
import json
import os

import lightgbm as lgb
import numpy as np
import pandas as pd

from models_lib import load_features

OUT = '/home/claude/keiba_lab/out'
D = '/home/claude/keiba_lab/data'
ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
F, FUND = load_features(f'{D}/features_v3.parquet')
FEATS = [c for c in FUND if c not in ODDS]
runs = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'last_corner'])
F = F.merge(runs, on=['race_id', 'horse_number'], how='left')
F['pos4'] = F.last_corner / F.field
F['top3'] = (F.finish <= 3).astype(float)
shape = F[F.finish <= 3].groupby('race_id').pos4.mean()
F['shape'] = F.race_id.map(shape)

params = dict(objective='binary', learning_rate=0.03, num_leaves=63, min_data_in_leaf=200, feature_fraction=0.7,
              bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=1, verbosity=-1)
te = F[F.year >= 2024].copy()
Xte = te[FEATS].values.astype(np.float32)
for name, cond in (('F', lambda s: s <= 0.25), ('C', lambda s: s >= 0.375)):
    tr = F[F.year.between(2011, 2022) & cond(F['shape'])]
    va = F[(F.year == 2023) & cond(F['shape'])]
    dtr = lgb.Dataset(tr[FEATS].values.astype(np.float32), tr.top3.values)
    dva = lgb.Dataset(va[FEATS].values.astype(np.float32), va.top3.values, reference=dtr)
    b = lgb.train(params, dtr, 4000, valid_sets=[dva], callbacks=[lgb.early_stopping(150, verbose=False)])
    te[f's_{name}'] = b.predict(Xte, raw_score=True)
    b.save_model(f'{OUT}/scenario_{name}.txt')
    print(name, 'train races', tr.race_id.nunique(), 'iters', b.best_iteration, flush=True)
main = pd.read_parquet(f'{OUT}/top3_final_v3_holdout.parquet', columns=['race_id', 'horse_number', 'p_m0'])
te = te.merge(main, on=['race_id', 'horse_number'], how='inner')
te[['race_id', 'horse_number', 'year', 'finish', 'popularity', 'race_num', 'field', 's_F', 's_C', 'p_m0', 'pos4', 'shape']] \
    .to_parquet(f'{OUT}/scenario_scores.parquet')

pay = pd.read_parquet(f'{D}/payouts.parquet')
srp = pay[pay.bet_type == '三連複']
cnt = srp.groupby('race_id').size()
srp = srp[srp.race_id.map(cnt) == 1]
srp_pay = dict(zip(srp.race_id, srp.payout))
wd = pay[pay.bet_type == 'ワイド']
wide_pay = {rid: {tuple(sorted(h)): v for h, v in zip(g.horse_numbers, g.payout)} for rid, g in wd.groupby('race_id')}


def top(g, col, k):
    return list(g.sort_values(col, ascending=False).horse_number.values[:k])


STRATS = {
    'main5': [('p_m0', 5)],
    'F4|C4': [('s_F', 4), ('s_C', 4)],
    'main4|F4|C4': [('p_m0', 4), ('s_F', 4), ('s_C', 4)],
    'main3|F3|C3': [('p_m0', 3), ('s_F', 3), ('s_C', 3)],
    'main6': [('p_m0', 6)],
    'F5|C5': [('s_F', 5), ('s_C', 5)],
    'main5|C5': [('p_m0', 5), ('s_C', 5)],
}
rows = []
for rid, g in te.groupby('race_id'):
    if len(g) < 8 or g.finish.isna().all():
        continue
    actual = tuple(sorted(g[g.finish <= 3].horse_number.values))
    if len(actual) != 3:
        continue
    rec = {'race_id': rid, 'race_num': g.race_num.iat[0], 'shape': g['shape'].iat[0]}
    for sname, parts in STRATS.items():
        triples, pairs = set(), set()
        for col, k in parts:
            box = sorted(top(g, col, k))
            triples |= set(itertools.combinations(box, 3))
            pairs |= set(itertools.combinations(box, 2))
        hit = actual in triples
        rec[f'{sname}_n'] = len(triples)
        rec[f'{sname}_hit'] = hit
        rec[f'{sname}_ret'] = srp_pay.get(rid, 0) if hit else 0
        wp = wide_pay.get(rid, {})
        rec[f'{sname}_wn'] = len(pairs)
        rec[f'{sname}_wret'] = sum(wp.get(p, 0) for p in pairs)
    rows.append(rec)
R = pd.DataFrame(rows)


def summary(R):
    out = {}
    for s in STRATS:
        out[s] = {'tickets_per_race': round(R[f'{s}_n'].mean(), 2), 'sanrenpuku_hit': round(R[f'{s}_hit'].mean(), 4),
                  'sanrenpuku_roi': round(R[f'{s}_ret'].sum() / (R[f'{s}_n'].sum() * 100), 4),
                  'wide_tickets': round(R[f'{s}_wn'].mean(), 2),
                  'wide_roi': round(R[f'{s}_wret'].sum() / (R[f'{s}_wn'].sum() * 100), 4)}
    return pd.DataFrame(out).T


print('=== 2024-2026 all races (8+ runners):', len(R))
print(summary(R).to_string())
R11 = R[R.race_num == 11]
print('=== 11R only:', len(R11))
print(summary(R11).to_string())
for lab, sub in (('front races', R[R['shape'] <= 0.25]), ('closer races', R[R['shape'] >= 0.375])):
    print(f'=== by actual shape: {lab} ({len(sub)})')
    print(summary(sub)[['tickets_per_race', 'sanrenpuku_hit']].to_string())
g = te[te.race_id == '202609040811'].copy()
g['rank_main'] = g.p_m0.rank(ascending=False).astype(int)
g['rank_F'] = g.s_F.rank(ascending=False).astype(int)
g['rank_C'] = g.s_C.rank(ascending=False).astype(int)
print('=== Sirius S')
print(g.sort_values('finish')[['horse_number', 'finish', 'popularity', 'rank_main', 'rank_F', 'rank_C', 'pos4']].to_string(index=False))
R.to_parquet(f'{OUT}/scenario_strats.parquet')
