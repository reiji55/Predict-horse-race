"""Extra point-in-time features aimed at 'why did this horse make the top 3' (vectorised, past races only).
Adds to features.parquet -> features_v2.parquet:
  course record   : runs / top-3 rate at same venue+surface, same exact course (venue+surface+distance), same surface+distance band
  late speed      : last-3F rank within race (pct) averaged over last 5 runs; best of last 5
  position        : 4th-corner position pct (last 5 mean)
  class form      : top-3 count in last 5, top-3 count at OP-or-better (career), mean class of last 5 minus today's class,
                    class-weighted finish  (class - 3*finish_pct) mean of last 5
  weight carried  : impost minus horse's last-5 mean impost; impost minus field max
  pace fit        : projected early-pace pressure (# horses whose avg first-corner pct < 0.25) x horse's position
  shrunk rates    : course top-3 rates shrunk toward the horse's overall top-3 rate (m = 3)
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
runs = pd.read_parquet(f'{D}/runs.parquet')
meta = pd.read_parquet(f'{D}/race_meta.parquet')[['race_id', 'venue_name']]
r = runs.merge(meta, on='race_id', how='left')
r = r.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
r['field'] = r.groupby('race_id').horse_number.transform('size')
r['top3'] = ((r.finish <= 3) & r.completed).astype(float)
r['fin_pct'] = np.where(r.completed, (r.finish - 1) / (r.field - 1), 1.0)
r['l3_pct'] = r.groupby('race_id').last_3f.rank(pct=True, method='average')
r['pos4_pct'] = r.last_corner / r.field
r['pos1_pct'] = r.first_corner / r.field
r['vs'] = r.venue_name + '_' + r.surf
r['course'] = r.vs + '_' + r.distance.astype(str)
r['dband'] = r.surf + '_' + (r.distance // 200).astype(str)
r['op_top3'] = ((r.cls >= 5) & (r.top3 == 1)).astype(float)
r['cls_form'] = r.cls - 3 * r.fin_pct
r['row'] = np.arange(len(r))

h = r.sort_values(['horse_id', 'date', 'start_min']).copy()
g = h.groupby('horse_id', sort=False)


def past_roll(col, w, fn='mean'):
    s = g[col].shift(1)
    roll = s.groupby(h.horse_id, sort=False).rolling(w, min_periods=1)
    out = roll.mean() if fn == 'mean' else (roll.sum() if fn == 'sum' else roll.min())
    return out.reset_index(level=0, drop=True)


h['l3_pct_mean5'] = past_roll('l3_pct', 5)
h['l3_pct_best5'] = past_roll('l3_pct', 5, 'min')
h['pos4_pct_mean5'] = past_roll('pos4_pct', 5)
h['top3_cnt5'] = past_roll('top3', 5, 'sum')
h['cls_mean5'] = past_roll('cls', 5)
h['cls_form_mean5'] = past_roll('cls_form', 5)
h['impost_mean5'] = past_roll('impost', 5)
h['op_top3_career'] = g.op_top3.cumsum() - h.op_top3
h['n_prev'] = g.cumcount()
h['top3_prev'] = g.top3.cumsum() - h.top3


def key_counts(keycol, prefix):
    gg = h.groupby(['horse_id', keycol], sort=False)
    n = gg.cumcount()
    t = gg.top3.cumsum() - h.top3
    h[f'{prefix}_runs'] = n
    h[f'{prefix}_top3'] = t
    overall = (h.top3_prev + 0.2 * 3) / (h.n_prev + 3)                # horse's overall top-3 rate, lightly shrunk
    h[f'{prefix}_top3_rate'] = (t + 3 * overall) / (n + 3)           # course rate shrunk toward overall rate
    h[f'{prefix}_top3_edge'] = h[f'{prefix}_top3_rate'] - overall


key_counts('vs', 'vs')
key_counts('course', 'course')
key_counts('dband', 'dband')
new_cols = ['l3_pct_mean5', 'l3_pct_best5', 'pos4_pct_mean5', 'top3_cnt5', 'cls_mean5', 'cls_form_mean5', 'impost_mean5',
            'op_top3_career', 'vs_runs', 'vs_top3', 'vs_top3_rate', 'vs_top3_edge', 'course_runs', 'course_top3',
            'course_top3_rate', 'course_top3_edge', 'dband_runs', 'dband_top3', 'dband_top3_rate', 'dband_top3_edge']
r = r.merge(h[['row'] + new_cols], on='row', how='left')
r['cls_drop5'] = r.cls_mean5 - r.cls                  # >0: came down from higher classes
r['impost_vs_own'] = r.impost - r.impost_mean5         # weight carried vs its recent norm
r['impost_vs_max'] = r.impost - r.groupby('race_id').impost.transform('max')
r['fast_starters'] = r.groupby('race_id').pos4_pct_mean5.transform(lambda s: (s < 0.25).sum())
gp = r.groupby('race_id')
for c in ['l3_pct_mean5', 'pos4_pct_mean5', 'cls_form_mean5', 'top3_cnt5', 'dband_top3_rate', 'course_top3_rate']:
    r[f'{c}_rk'] = gp[c].rank(pct=True)
r['pace_fit'] = r.pos4_pct_mean5 * r.fast_starters      # closers (high pos pct) x many front-runners
keep = ['race_id', 'horse_number'] + new_cols + ['cls_drop5', 'impost_vs_own', 'impost_vs_max', 'fast_starters',
                                                 'pace_fit'] + [f'{c}_rk' for c in ['l3_pct_mean5', 'pos4_pct_mean5',
                                                                                   'cls_form_mean5', 'top3_cnt5',
                                                                                   'dband_top3_rate', 'course_top3_rate']]
F = pd.read_parquet(f'{D}/features.parquet')
F = F.merge(r[keep], on=['race_id', 'horse_number'], how='left')
F.to_parquet(f'{D}/features_v2.parquet')
print('done', F.shape)
chk = F[F.race_id == '202609040811'][['horse_number', 'course_runs', 'course_top3', 'vs_runs', 'vs_top3', 'dband_runs',
                                      'dband_top3', 'l3_pct_mean5', 'pos4_pct_mean5', 'top3_cnt5', 'op_top3_career',
                                      'cls_drop5', 'impost_vs_own', 'days_off', 'finish']].sort_values('finish')
print(chk.round(3).to_string(index=False))
