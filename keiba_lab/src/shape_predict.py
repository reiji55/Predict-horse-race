"""(1) Can we tell before the race whether it will be a closers' race?  (2) Cross-formula wide tickets.
(1) race-level classifier: target = closer race (top-3 mean 4th-corner pos >= 0.375), features = pre-race field
    composition only (running styles, late speed, field size, course, going, class, same-day bias). AUC on 2024-2026.
    Then: a "shape-switching" strategy — use the closer formula's box when P(closer) is high, the front formula's box
    when low, main formula otherwise — compared with the main formula at the same ticket count.
(2) ワイド: front-formula top-3 x closer-formula top-3 (cross pairs) vs main top-5 box.
"""
import itertools

import lightgbm as lgb
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
OUT = '/home/claude/keiba_lab/out'
F = pd.read_parquet(f'{D}/features_v3.parquet', columns=[
    'race_id', 'year', 'field', 'distance', 'surf_code', 'venue_code', 'cond', 'cls', 'handicap', 'style_mean5',
    'pos4_pct_mean5', 'l3_pct_mean5', 'day_style_adv', 'day_n', 'fast_starters', 'finish', 'month'])
runs = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'last_corner', 'field'])
r = runs.copy()
r['pos4'] = r.last_corner / r.field
fin = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'finish'])
r = r.merge(fin, on=['race_id', 'horse_number'])
shape = r[r.finish <= 3].groupby('race_id').pos4.mean()
F['handicap'] = F.handicap.astype(int)
g = F.groupby('race_id')
race = g.agg(year=('year', 'first'), field=('field', 'first'), distance=('distance', 'first'),
             surf=('surf_code', 'first'), venue=('venue_code', 'first'), cond=('cond', 'first'), cls=('cls', 'first'),
             handicap=('handicap', 'first'), month=('month', 'first'), fast=('fast_starters', 'first'),
             day_bias=('day_style_adv', 'first'), day_n=('day_n', 'first'),
             style_mean=('style_mean5', 'mean'), style_min=('style_mean5', 'min'), style_std=('style_mean5', 'std'),
             pos4_mean=('pos4_pct_mean5', 'mean'), pos4_min=('pos4_pct_mean5', 'min'),
             l3_mean=('l3_pct_mean5', 'mean'), l3_min=('l3_pct_mean5', 'min')).reset_index()
race['n_front'] = g.apply(lambda d: (d.style_mean5 < 0.15).sum(), include_groups=False).values
race['shape'] = race.race_id.map(shape)
race = race[race['shape'].notna() & (race.field >= 8)]
race['closer'] = (race['shape'] >= 0.375).astype(int)
feats = [c for c in race.columns if c not in ('race_id', 'year', 'shape', 'closer')]
tr = race[race.year.between(2011, 2022)]
va = race[race.year == 2023]
te = race[race.year >= 2024].copy()
p = dict(objective='binary', learning_rate=0.03, num_leaves=15, min_data_in_leaf=200, feature_fraction=0.8,
         bagging_fraction=0.8, bagging_freq=1, lambda_l2=10, num_threads=2, verbosity=-1, seed=1)
b = lgb.train(p, lgb.Dataset(tr[feats], tr.closer), 3000, valid_sets=[lgb.Dataset(va[feats], va.closer)],
              callbacks=[lgb.early_stopping(100, verbose=False)])
te['p_closer'] = b.predict(te[feats])


def auc(y, s):
    o = np.argsort(s)
    rk = np.empty(len(s))
    rk[o] = np.arange(1, len(s) + 1)
    n1 = y.sum()
    return (rk[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * (len(y) - n1))


print('closer-race base rate (2024-26):', round(te.closer.mean(), 3), ' AUC:', round(auc(te.closer.values, te.p_closer.values), 4))
q = pd.qcut(te.p_closer, 5, labels=False)
print('closer-race rate by predicted quintile:', te.groupby(q).closer.mean().round(3).to_dict())
print('Sirius S: P(closer) =', round(float(te.loc[te.race_id == '202609040811', 'p_closer'].iloc[0]), 3),
      ' quintile', int(q[te.race_id == '202609040811'].iloc[0]))
imp = pd.Series(b.feature_importance('gain'), index=feats).sort_values(ascending=False)
print('top race-shape signals:', imp.head(6).round(0).to_dict())

# ---- strategies using the horse-level scores ----
S = pd.read_parquet(f'{OUT}/scenario_scores.parquet')
S = S.merge(te[['race_id', 'p_closer']], on='race_id', how='inner')
pay = pd.read_parquet(f'{D}/payouts.parquet')
srp = pay[pay.bet_type == '三連複']
c = srp.groupby('race_id').size()
srp = srp[srp.race_id.map(c) == 1]
srp_pay = dict(zip(srp.race_id, srp.payout))
wd = pay[pay.bet_type == 'ワイド']
wide_pay = {rid: {tuple(sorted(h)): v for h, v in zip(gg.horse_numbers, gg.payout)} for rid, gg in wd.groupby('race_id')}
hi, lo = te.p_closer.quantile(0.8), te.p_closer.quantile(0.2)
rows = []
for rid, gg in S.groupby('race_id'):
    actual = tuple(sorted(gg[gg.finish <= 3].horse_number.values))
    if len(actual) != 3 or len(gg) < 8:
        continue
    top = lambda col, k: sorted(gg.sort_values(col, ascending=False).horse_number.values[:k])
    pc = gg.p_closer.iat[0]
    col = 's_C' if pc >= hi else ('s_F' if pc <= lo else 'p_m0')
    rec = {'race_id': rid, 'race_num': gg.race_num.iat[0]}
    for name, box in (('main5', top('p_m0', 5)), ('switch5', top(col, 5))):
        tri = set(itertools.combinations(box, 3))
        rec[f'{name}_hit'] = actual in tri
        rec[f'{name}_ret'] = srp_pay.get(rid, 0) if actual in tri else 0
    wp = wide_pay.get(rid, {})
    cross = {tuple(sorted((a, b2))) for a in top('s_F', 3) for b2 in top('s_C', 3) if a != b2}
    box5 = set(itertools.combinations(top('p_m0', 5), 2))
    for name, pairs in (('wide_cross', cross), ('wide_main5', box5)):
        rec[f'{name}_n'] = len(pairs)
        rec[f'{name}_hits'] = sum(1 for pp in pairs if pp in wp)
        rec[f'{name}_ret'] = sum(wp.get(pp, 0) for pp in pairs)
    rows.append(rec)
R = pd.DataFrame(rows)
for lab, sub in (('all', R), ('11R', R[R.race_num == 11])):
    print(f'=== {lab} ({len(sub)} races)')
    for n in ('main5', 'switch5'):
        print(f'  3連複 {n}: hit {sub[f"{n}_hit"].mean():.4f}  ROI {sub[f"{n}_ret"].sum() / (len(sub) * 1000):.4f}')
    for n in ('wide_cross', 'wide_main5'):
        print(f'  {n}: tickets/race {sub[f"{n}_n"].mean():.2f}  races with a hit {(sub[f"{n}_hits"] > 0).mean():.4f}'
              f'  ROI {sub[f"{n}_ret"].sum() / (sub[f"{n}_n"].sum() * 100):.4f}')
s = R[R.race_id == '202609040811']
print('Sirius S wide_cross hits:', int(s.wide_cross_hits.iloc[0]), ' wide_main5 hits:', int(s.wide_main5_hits.iloc[0]))
