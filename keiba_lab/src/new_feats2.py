"""Candidate features v6 (point-in-time).
  jky_today   : 騎手の当日の調子。同じ日・同じ競馬場で、このレースより前に乗ったレースの
                「3着内の数 − 期待（その馬の人気ではなく頭数から 3/頭数）」と乗った数
  layoff      : 休み明けから何戦目か（90日以上空いた走を休み明けとする）、前走が休み明けだったか、
                前走との間隔の変化
writes data/features_x2.parquet
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'start_min', 'venue_code', 'horse_id',
                                                  'horse_number', 'jockey_id', 'finish', 'field'])
r['race_id'] = r.race_id.astype(str)
r['date'] = pd.to_datetime(r.date)
r = r.sort_values(['date', 'venue_code', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
out = r[['race_id', 'horse_number']].copy()

# ---- jockey today ----
r['t3'] = (r.finish <= 3).astype(float).where(r.finish.notna(), 0.0)
r['exp3'] = np.where(r.finish.notna(), 3 / r.field.clip(lower=3), 0.0)
r['rode'] = r.finish.notna().astype(float)
key = [r.date, r.venue_code, r.jockey_id]
# races before this one on the same day (start_min order); same race can't repeat for a jockey
grp = r.groupby(key, sort=False)
out['jky_today_n'] = (grp.rode.cumsum() - r.rode).values
out['jky_today_edge'] = (grp.t3.cumsum() - r.t3 - (grp.exp3.cumsum() - r.exp3)).values
out.loc[r.jockey_id.isna(), ['jky_today_n', 'jky_today_edge']] = np.nan

# ---- layoff ----
h = r.sort_values(['horse_id', 'date']).copy()
h['gap'] = h.groupby('horse_id').date.diff().dt.days
h['fresh'] = (h.gap.isna() | (h.gap >= 90)).astype(int)        # 初出走も「休み明け」扱い
h['block'] = h.groupby('horse_id').fresh.cumsum()
h['run_in_block'] = h.groupby(['horse_id', 'block']).cumcount() + 1   # 1 = 休み明け初戦
h['prev_fresh'] = h.groupby('horse_id').fresh.shift(1)
h['prev_gap'] = h.groupby('horse_id').gap.shift(1)
h['gap_change'] = h.gap - h.prev_gap
h = h.set_index(['race_id', 'horse_number'])
idx = list(zip(out.race_id, out.horse_number))
for c in ('run_in_block', 'prev_fresh', 'gap_change'):
    out[c] = h.loc[idx, c].values
out.to_parquet(f'{D}/features_x2.parquet')
print(out.describe().T[['count', 'mean', 'std']].round(3).to_string())
