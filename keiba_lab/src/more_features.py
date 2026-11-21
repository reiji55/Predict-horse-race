"""v4 features (vectorised, past races only) on top of features_v3:
  jockey change     : current jockey's top-3 rate minus the previous ride's jockey's rate (both as of their races)
  fresh record      : horse's past top-3 rate in first starts after 60+ days off, and whether today is such a start
  trainer form      : trainer's top-3 rate over the previous 60 days (time window, excludes today)
  jockey x course   : jockey's past top-3 rate on this exact course (shrunk)
  impost / weight   : impost divided by horse weight
  position spread   : std of 4th-corner position over the last 5 runs
  class-up after win: won last time and moves up in class today
  days since last top-3
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
F = pd.read_parquet(f'{D}/features_v3.parquet')
runs = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'last_corner', 'venue_code', 'trainer_id'])
F = F.merge(runs, on=['race_id', 'horse_number'], how='left', suffixes=('', '_r'))
F = F.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
F['row'] = np.arange(len(F))
F['top3f'] = ((F.finish <= 3) & F.completed).astype(float)
F['pos4'] = F.last_corner / F.field
F['course'] = F.venue_code.astype(str) + F.surf + F.distance.astype(str)

h = F.sort_values(['horse_id', 'date', 'start_min']).copy()
g = h.groupby('horse_id', sort=False)
h['prev_jky_top3'] = g.jky_top3.shift(1)
h['jky_upgrade'] = h.jky_top3 - h.prev_jky_top3
h['is_fresh'] = (h.days_off >= 60).astype(float)
fresh_top3 = (h.is_fresh * h.top3f)
h['fresh_runs_prev'] = g.is_fresh.cumsum() - h.is_fresh
h['fresh_top3_prev'] = fresh_top3.groupby(h.horse_id).cumsum() - fresh_top3
h['fresh_rate'] = (h.fresh_top3_prev + 0.25) / (h.fresh_runs_prev + 1)
h['pos4_std5'] = g.pos4.shift(1).groupby(h.horse_id).rolling(5, min_periods=2).std().reset_index(level=0, drop=True)
h['won_last'] = (g.finish.shift(1) == 1).astype(float)
h['up_after_win'] = h.won_last * (h.cls_change > 0)
last_t3_date = h.date.where(h.top3f == 1)
h['last_t3_date'] = last_t3_date.groupby(h.horse_id).shift(1)
h['last_t3_date'] = h.groupby('horse_id').last_t3_date.ffill()
h['days_since_top3'] = (h.date - h.last_t3_date).dt.days
F = F.merge(h[['row', 'jky_upgrade', 'is_fresh', 'fresh_rate', 'fresh_runs_prev', 'pos4_std5', 'up_after_win',
               'days_since_top3']], on='row', how='left')

# trainer form over the previous 60 days (excluding today)
t = F[['row', 'trainer_id', 'date', 'top3f']].sort_values(['trainer_id', 'date'])
day = t.groupby(['trainer_id', 'date']).top3f.agg(['sum', 'count']).reset_index()
day = day.sort_values(['trainer_id', 'date'])
roll = day.set_index('date').groupby('trainer_id')[['sum', 'count']].rolling('60D', closed='left').sum().reset_index()
roll = roll.rename(columns={'sum': 'trn60_t3', 'count': 'trn60_n'})
F = F.merge(roll, on=['trainer_id', 'date'], how='left')
F['trn60_rate'] = (F.trn60_t3.fillna(0) + 0.21 * 5) / (F.trn60_n.fillna(0) + 5)

# jockey x exact course, past only
j = F[['row', 'jockey_id', 'course', 'date', 'start_min', 'top3f']].sort_values(['date', 'start_min'])
gj = j.groupby(['jockey_id', 'course'], sort=False)
j['jc_n'] = gj.cumcount()
j['jc_t3'] = gj.top3f.cumsum() - j.top3f
F = F.merge(j[['row', 'jc_n', 'jc_t3']], on='row', how='left')
F['jky_course_rate'] = (F.jc_t3 + F.jky_top3.fillna(0.21) * 10) / (F.jc_n + 10)
F['jky_course_edge'] = F.jky_course_rate - F.jky_top3
F['impost_per_kg'] = F.impost / F.horse_weight
gp = F.groupby('race_id')
for c in ('trn60_rate', 'jky_course_rate', 'fresh_rate', 'impost_per_kg'):
    F[f'{c}_rel'] = F[c] - gp[c].transform('mean')
drop = ['last_corner', 'venue_code_r', 'trainer_id_r', 'row', 'top3f', 'pos4', 'course', 'trn60_t3', 'jc_t3']
F = F.drop(columns=[c for c in drop if c in F.columns])
F.to_parquet(f'{D}/features_v4.parquet')
print('done', F.shape)
