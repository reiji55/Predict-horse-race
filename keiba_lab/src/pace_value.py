"""How much would knowing the race shape help, and can it be predicted before the race?

1. pace_resid = actual first-3F time minus the norm for that course / class / going (negative = faster than usual)
2. ex-post value: add (actual pace_resid x horse's closer index) and (horse's actual 4th-corner position) to the
   formula score; measure the gain in ranking log-likelihood of the top 3 on 2024-2026
3. ex-ante: predict pace_resid from the field's past running styles; R^2 on 2024-2026; value of the predicted pace
"""
import json

import numpy as np
import pandas as pd

from common import D, OUT, race_index, fit_clogit, group_softmax

F = pd.read_parquet(f'{D}/features_v2.parquet')
meta = pd.read_parquet(f'{D}/race_meta.parquet')[['race_id', 'venue_name', 'pace_first3f']]
runs = pd.read_parquet(f'{D}/runs.parquet')[['race_id', 'horse_number', 'last_corner', 'first_corner']]
F = F.drop(columns=[c for c in ('venue_name',) if c in F]).merge(meta, on='race_id').merge(runs, on=['race_id', 'horse_number'])
F = F[~F.dead_heat_win & (F.year >= 2011)].sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
F['fin'] = F.finish.fillna(99)

# formula2 score (fitted on 2011-2023)
fm = json.load(open(f'{OUT}/formula2_f23.json'))
g_ = F.groupby('race_id')
F['rating_fill_rel'] = F.rating_fill - g_.rating_fill.transform('mean')
F['log_days_off'] = np.log1p(F.days_off)
F['long_rest'] = (F.days_off >= 180).astype(float)
F['dist_change_abs'] = F.dist_change.abs()
F['weight_change_abs'] = F.weight_change.abs()
S = np.zeros(len(F))
for c, b in fm['beta'].items():
    mu, sd = fm['stats'][c]
    S += b * np.clip(np.nan_to_num((F[c].values - mu) / sd), -4, 4)
F['S'] = S

# pace norm per course x class group x going (train years), fallback course only
race = F.groupby('race_id').agg(year=('year', 'first'), venue=('venue_name', 'first'), surf=('surf', 'first'),
                                dist=('distance', 'first'), cls=('cls', 'first'), cond=('cond', 'first'),
                                p3=('pace_first3f', 'first'), field=('field', 'first'),
                                fast=('fast_starters', 'first'), style_mean=('style_mean5', 'mean'),
                                style_min=('style_mean5', 'min')).reset_index()
race['clsg'] = pd.cut(race.cls, [-1, 1, 3, 4, 9], labels=['新馬未勝利', '1-2勝', '3勝', 'OP以上']).astype(str)
race['condg'] = (race.cond >= 2).astype(int)
trr = race[race.year <= 2023]
norm1 = trr.groupby(['venue', 'surf', 'dist', 'clsg', 'condg']).p3.median()
norm0 = trr.groupby(['venue', 'surf', 'dist']).p3.median()
cnt1 = trr.groupby(['venue', 'surf', 'dist', 'clsg', 'condg']).p3.size()
k1 = list(zip(race.venue, race.surf, race.dist, race.clsg, race.condg))
k0 = list(zip(race.venue, race.surf, race.dist))
race['norm'] = [norm1.get(a) if cnt1.get(a, 0) >= 15 else norm0.get(b) for a, b in zip(k1, k0)]
race['pace_resid'] = race.p3 - race.norm
sd_p = race.loc[race.year <= 2023, 'pace_resid'].std()
race['pace_z'] = race.pace_resid / sd_p
print('pace residual sd (s):', round(sd_p, 3))

# ex-ante pace model: linear regression on field composition (train <=2023)
Xr = np.column_stack([np.ones(len(race)), race.fast.fillna(0), race.style_mean.fillna(race.style_mean.mean()),
                      race.style_min.fillna(race.style_min.mean()), race.field])
ok = race.pace_z.notna().values
trm = (race.year <= 2023).values & ok
coef, *_ = np.linalg.lstsq(Xr[trm], race.pace_z.values[trm], rcond=None)
race['pace_pred'] = Xr @ coef
tem = (race.year >= 2024).values & ok
resid = race.pace_z.values[tem] - race.pace_pred.values[tem]
r2 = 1 - resid.var() / race.pace_z.values[tem].var()
print('ex-ante pace prediction R^2 on 2024-2026:', round(r2, 4), ' coef', np.round(coef, 3))

F = F.merge(race[['race_id', 'pace_z', 'pace_pred']], on='race_id', how='left')
F['closer'] = (F.pos4_pct_mean5 - 0.48) / 0.21          # z of past 4th-corner position (high = closer)
F['closer'] = F.closer.fillna(0)
F['pos4_now'] = (F.last_corner / F.field).fillna(1.0)   # in-race information (not knowable beforehand)
F['x_pace_actual'] = F.pace_z.fillna(0) * F.closer
F['x_pace_pred'] = F.pace_pred.fillna(0) * F.closer


def stage_rows(mask, place):
    d = F[mask]
    if place == 1:
        return d.index.values
    better = d.groupby('race_id').fin.transform(lambda s: (s < place).sum())
    one = d.groupby('race_id').fin.transform(lambda s: (s == place).sum())
    return d[(better == place - 1) & (one == 1) & (d.fin >= place)].index.values


def stacked(mask):
    ix_l, y_l, k_l = [], [], []
    for place in (1, 2, 3):
        ix = stage_rows(mask, place)
        ix_l.append(ix)
        y_l.append((F.fin.values[ix] == place).astype(float))
        k_l.append(F.race_id.values[ix] + f'_{place}')
    ix, yy, kk = np.concatenate(ix_l), np.concatenate(y_l), np.concatenate(k_l)
    o = np.argsort(kk, kind='stable')
    return ix[o], yy[o], kk[o]


tr_ix, tr_y, tr_k = stacked(F.year.between(2011, 2023).values)
te_ix, te_y, te_k = stacked(F.year.between(2024, 2026).values)
s_tr, i_tr = race_index(tr_k)
s_te, i_te = race_index(te_k)
n_races_te = F.loc[F.year >= 2024, 'race_id'].nunique()


def rank_ll(cols):
    X = np.column_stack([F[c].values for c in cols])
    w, _ = fit_clogit(X[tr_ix], tr_y, s_tr, i_tr)
    p = group_softmax(X[te_ix] @ w, s_te, i_te)
    ll = np.sum(np.log(np.clip(p, 1e-300, 1)) * te_y) / n_races_te
    return ll, w


base, wb = rank_ll(['S'])
res = {'base_formula': round(base, 5)}
for name, cols in {'+ predicted pace x closer (before the race)': ['S', 'x_pace_pred'],
                   '+ actual pace x closer (after the race)': ['S', 'x_pace_actual'],
                   "+ horse's actual 4th-corner position (after the race)": ['S', 'pos4_now'],
                   '+ actual pace x closer + actual position': ['S', 'x_pace_actual', 'pos4_now']}.items():
    ll, w = rank_ll(cols)
    res[name] = {'gain_per_race': round(ll - base, 5), 'coef': np.round(w, 3).tolist()}
    print(name, res[name], flush=True)
# closers' top-3 rate by actual pace (all years)
d = F[F.pos4_pct_mean5.notna() & F.pace_z.notna()].copy()
d['top3'] = (d.finish <= 3).astype(float)
d['closer_g'] = pd.cut(d.pos4_pct_mean5, [0, 0.3, 0.6, 1.01], labels=['前（先行）', '中団', '後方（差し・追込）'])
d['pace_g'] = pd.cut(d.pace_z, [-10, -1, 1, 10], labels=['速い（1SD以上）', '平均', '遅い（1SD以上）'])
tab = d.groupby(['pace_g', 'closer_g'], observed=True).top3.mean().unstack().round(3)
print(tab.to_string())
sir = race[race.race_id == '202609040811'][['p3', 'norm', 'pace_resid', 'pace_z', 'pace_pred', 'fast']]
print('Sirius S:', sir.round(3).to_dict('records'))
res['top3_rate_by_pace_and_style'] = tab.to_dict()
res['sirius'] = sir.round(3).to_dict('records')
res['pace_r2'] = round(r2, 4)
json.dump(res, open(f'{OUT}/pace_value.json', 'w'), ensure_ascii=False, indent=1, default=str)
