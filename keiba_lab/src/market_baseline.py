"""How good is the market (final win odds) as a probability? -> the wall to beat."""
import numpy as np
import pandas as pd
from common import D, race_index, group_softmax, race_logloss, fit_clogit

runs = pd.read_parquet(f'{D}/runs.parquet')
runs = runs[~runs.dead_heat_win].copy()
runs = runs.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
y = (runs.finish == 1).astype(float).values
starts, inv = race_index(runs.race_id.values)
logp = np.log(runs.mkt_p.values)
field = runs.groupby('race_id', sort=False).size().values
race_year = runs.year.values[starts]

ll_mkt = race_logloss(runs.mkt_p.values, y, starts)
ll0 = np.log(field)

# FLB-corrected market: p ∝ pi^theta, fitted on 2010-2017 only
tr = np.isin(runs.year.values, np.arange(2010, 2018))
tr_r = np.isin(race_year, np.arange(2010, 2018))


def sub(mask_rows):
    idx = np.flatnonzero(mask_rows)
    rid = runs.race_id.values[idx]
    s, i = race_index(rid)
    return idx, s, i


idx, s_tr, i_tr = sub(tr)
w, r = fit_clogit(logp[idx, None], y[idx], s_tr, i_tr)
theta = w[0]
p_theta = group_softmax(theta * logp, starts, inv)
ll_theta = race_logloss(p_theta, y, starts)

# richer: theta varies with log(odds) bands (piecewise) -> 2 extra params: logp, logp^2
X2 = np.c_[logp, logp ** 2]
w2, _ = fit_clogit(X2[idx], y[idx], s_tr, i_tr)
p2 = group_softmax(X2 @ w2, starts, inv)
ll_2 = race_logloss(p2, y, starts)

df = pd.DataFrame({'year': race_year, 'll0': ll0, 'mkt': ll_mkt, 'theta': ll_theta, 'quad': ll_2})
tab = df.groupby('year').mean()
tab['R2_mkt'] = 1 - tab.mkt / tab.ll0
tab['d_theta'] = tab.mkt - tab.theta
tab['d_quad'] = tab.mkt - tab.quad
print('theta (FLB power) =', round(theta, 4), ' quad =', np.round(w2, 4))
print(tab.round(4).to_string())
tot = df.mean()
print('ALL: ll0 %.4f  mkt %.4f  theta %.4f quad %.4f  pseudoR2 mkt %.4f' % (tot.ll0, tot.mkt, tot.theta, tot.quad, 1 - tot.mkt / tot.ll0))

# calibration of raw market probability by bins (all years)
b = pd.cut(runs.mkt_p, [0, .01, .02, .03, .05, .075, .1, .15, .2, .3, .4, .5, .7, 1])
cal = pd.DataFrame({'b': b, 'p': runs.mkt_p, 'y': y, 'odds': runs.odds}).groupby('b', observed=True).agg(
    n=('y', 'size'), mean_p=('p', 'mean'), win_rate=('y', 'mean'), roi=('odds', lambda o: np.nan))
roi = pd.DataFrame({'b': b, 'ret': y * runs.odds.values}).groupby('b', observed=True).ret.mean()
cal['ratio'] = cal.win_rate / cal.mean_p
cal['win_roi'] = roi
print(cal.round(4).to_string())
