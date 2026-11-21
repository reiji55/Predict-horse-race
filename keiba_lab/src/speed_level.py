"""Two new point-in-time feature families (no odds), added to features_v2 -> features_v3.

A) Speed figure (タイム指数) with a daily track variant (馬場差):
   tpk      = time / (distance/1000)                         seconds per km
   standard = mean winner tpk at the course in EARLIER years (cumulative, leak-free), after class offset
   variant  = shrunk mean over that day's winners at the venue/surface of (tpk - standard - class offset)
   fig      = -(tpk - standard - variant) * 10                higher = faster
   per horse (past runs only): last, mean of 3, best of 5, best on same surface, best in same 200m band; race-relative.

B) Race level (レースレベル) of the horse's previous race, as known on the day of the current race:
   opponents = top-5 finishers of the previous race (excluding the horse)
   opp_wins_since / opp_top3_since = wins / top-3s those opponents collected after that race and before today
   lvl_gain = their latest rating before today minus their rating at that race
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
runs = pd.read_parquet(f'{D}/runs.parquet')
runs = runs.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
runs['row'] = np.arange(len(runs))
runs['tpk'] = runs.time_seconds / (runs.distance / 1000.0)
runs['course'] = runs.venue_code.astype(str) + '_' + runs.surf + '_' + runs.distance.astype(str)
runs['clsg'] = runs.cls.clip(lower=0, upper=8)

# ---------- A) speed figures ----------
w = runs[(runs.finish == 1) & runs.tpk.notna()].copy()
# class offset per surface from 2010-2018 winners (relative to course median)
early = w[w.year <= 2018].copy()
early['dev0'] = early.tpk - early.groupby('course').tpk.transform('median')
cls_off = early.groupby(['surf', 'clsg']).dev0.median()
w['cls_off'] = [cls_off.get((s, c), 0.0) for s, c in zip(w.surf, w.clsg)]
w['adj'] = w.tpk - w.cls_off
# cumulative course standard from earlier years
cy = w.groupby(['course', 'year']).adj.agg(['sum', 'count']).reset_index().sort_values(['course', 'year'])
cy['csum'] = cy.groupby('course')['sum'].cumsum() - cy['sum']
cy['ccnt'] = cy.groupby('course')['count'].cumsum() - cy['count']
cy['std_prev'] = np.where(cy.ccnt >= 5, cy.csum / cy.ccnt.clip(lower=1), cy['sum'] / cy['count'])   # first year: same-year mean
std = cy.set_index(['course', 'year']).std_prev
runs['std'] = [std.get((c, y), np.nan) for c, y in zip(runs.course, runs.year)]
w['std'] = [std.get((c, y), np.nan) for c, y in zip(w.course, w.year)]
w['dev'] = (w.adj - w['std']).clip(-1.5, 1.5)
var = w.groupby(['date', 'venue_code', 'surf']).dev.agg(['sum', 'count'])
var['variant'] = var['sum'] / (var['count'] + 1.5)
runs = runs.merge(var[['variant']].reset_index(), on=['date', 'venue_code', 'surf'], how='left')
runs['variant'] = runs.variant.fillna(0.0)
runs['fig'] = -(runs.tpk - runs['std'] - runs.variant) * 10
runs.loc[~runs.completed, 'fig'] = np.nan
runs['fig'] = runs.fig.clip(-60, 40)
runs['dband'] = runs.surf + '_' + (runs.distance // 200).astype(str)
runs = runs.sort_values('row').reset_index(drop=True)

h = runs.sort_values(['horse_id', 'date', 'start_min']).copy()
g = h.groupby('horse_id', sort=False)
sh = g.fig.shift(1)
h['fig_last'] = sh
h['fig_mean3'] = sh.groupby(h.horse_id).rolling(3, min_periods=1).mean().reset_index(level=0, drop=True)
h['fig_best5'] = sh.groupby(h.horse_id).rolling(5, min_periods=1).max().reset_index(level=0, drop=True)
gs = h.groupby(['horse_id', 'surf'], sort=False)
shs = gs.fig.shift(1)
h['fig_best_surf'] = shs.groupby([h.horse_id, h.surf]).rolling(5, min_periods=1).max().reset_index(level=[0, 1], drop=True)
gb = h.groupby(['horse_id', 'dband'], sort=False)
shb = gb.fig.shift(1)
h['fig_best_band'] = shb.groupby([h.horse_id, h.dband]).rolling(3, min_periods=1).max().reset_index(level=[0, 1], drop=True)
h['fig_trend'] = h.fig_last - h.fig_mean3.groupby(h.horse_id).shift(1)
h['prev_race_id'] = g.race_id.shift(1)
h['won'] = (h.finish == 1).astype(int)
h['t3'] = ((h.finish <= 3) & h.completed).astype(int)
h['wins_incl'] = g.won.cumsum()
h['top3_incl'] = g.t3.cumsum()
runs = runs.merge(h[['row', 'fig_last', 'fig_mean3', 'fig_best5', 'fig_best_surf', 'fig_best_band', 'fig_trend',
                     'prev_race_id', 'wins_incl', 'top3_incl']], on='row', how='left')

# ---------- B) race level of the previous race ----------
F2 = pd.read_parquet(f'{D}/features_v2.parquet', columns=['race_id', 'horse_number', 'rating'])
runs = runs.merge(F2, on=['race_id', 'horse_number'], how='left')
top5 = runs[(runs.finish <= 5) & runs.completed][['race_id', 'horse_id', 'rating', 'wins_incl', 'top3_incl']]
top5 = top5.rename(columns={'race_id': 'prev_race_id', 'horse_id': 'opp', 'rating': 'opp_rating_then',
                            'wins_incl': 'opp_wins_then', 'top3_incl': 'opp_top3_then'})
cur = runs[runs.prev_race_id.notna()][['row', 'horse_id', 'prev_race_id', 'date', 'start_min']]
pairs = cur.merge(top5, on='prev_race_id', how='inner')
pairs = pairs[pairs.opp != pairs.horse_id]
# opponent's latest run strictly before today: its pre-race rating and cumulative wins / top-3s including that run
hist = runs[['horse_id', 'date', 'rating', 'wins_incl', 'top3_incl']].rename(
    columns={'horse_id': 'opp', 'rating': 'opp_rating_now', 'wins_incl': 'opp_wins_now', 'top3_incl': 'opp_top3_now'})
hist = hist.sort_values('date')
pairs = pairs.sort_values('date')
pairs = pd.merge_asof(pairs, hist, on='date', by='opp', direction='backward', allow_exact_matches=False)
pairs['wins_since'] = (pairs.opp_wins_now - pairs.opp_wins_then).clip(lower=0)
pairs['top3_since'] = (pairs.opp_top3_now - pairs.opp_top3_then).clip(lower=0)
pairs['gain'] = pairs.opp_rating_now - pairs.opp_rating_then
agg = pairs.groupby('row').agg(opp_wins_since=('wins_since', 'sum'), opp_top3_since=('top3_since', 'sum'),
                               lvl_now=('opp_rating_now', 'mean'), lvl_then=('opp_rating_then', 'mean'),
                               lvl_gain=('gain', 'mean'), n_opp=('opp', 'size')).reset_index()
runs = runs.merge(agg, on='row', how='left')
for c in ('opp_wins_since', 'opp_top3_since'):
    runs[c] = runs[c].fillna(0)

new = ['fig_last', 'fig_mean3', 'fig_best5', 'fig_best_surf', 'fig_best_band', 'fig_trend', 'variant',
       'opp_wins_since', 'opp_top3_since', 'lvl_now', 'lvl_then', 'lvl_gain']
keep_new = runs[['race_id', 'horse_number'] + new].copy()        # メモリが足りない（8GBの環境）ので、途中の表を消してから読む
del runs, h, g, gs, gb, sh, shs, shb, pairs, hist, cur, top5, F2, agg
import gc
gc.collect()
F = pd.read_parquet(f'{D}/features_v2.parquet')
F = F.merge(keep_new.rename(columns={'variant': 'today_variant_unused'}),
            on=['race_id', 'horse_number'], how='left')
F = F.drop(columns=['today_variant_unused'])            # today's variant uses today's results -> never a feature
gp = F.groupby('race_id')
for c in ('fig_best5', 'fig_mean3', 'fig_best_surf', 'fig_best_band', 'lvl_now'):
    F[f'{c}_rel'] = F[c] - gp[c].transform('mean')
    F[f'{c}_rk'] = gp[c].rank(pct=True)
F.to_parquet(f'{D}/features_v3.parquet')
print('done', F.shape)
chk = F[F.race_id == '202609040811'].sort_values('finish')[['horse_number', 'finish', 'fig_last', 'fig_mean3', 'fig_best5',
                                                            'fig_best_band', 'fig_best5_rk', 'opp_wins_since',
                                                            'lvl_gain', 'lvl_now_rk']]
print(chk.round(2).to_string(index=False))
