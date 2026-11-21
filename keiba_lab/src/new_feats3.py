"""テン（前半の速さ）の近似値。JRA-VANのテン3Fの代わりに、手元のデータだけで作る。

各過去走について：
  ten_est = レースの前半3F + 1角の通過順位に応じた遅れ（1頭あたり 0.12秒）
  ten_z   = ten_est を、同じ芝ダ・距離の中央値と標準偏差で標準化（マイナス＝速い）
各馬について、その日より前の走だけで：前走、直近3走平均、直近5走のベスト、走数
レースの中での順位・平均との差と、レース全体の「速い馬の数」も作る。
writes data/features_x3.parquet
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'horse_id', 'horse_number', 'first_corner',
                                                  'field', 'surf', 'distance', 'finish'])
races = pd.read_parquet(f'{D}/races.parquet', columns=['race_id', 'pace_first3f'])
races['race_id'] = races.race_id.astype(str)
r['race_id'] = r.race_id.astype(str)
r = r.merge(races, on='race_id', how='left')
r['date'] = pd.to_datetime(r.date)
r = r.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
r['ten_est'] = r.pace_first3f + 0.12 * (r.first_corner - 1)
g = r.groupby(['surf', 'distance']).ten_est
r['ten_z'] = ((r.ten_est - g.transform('median')) / g.transform('std')).clip(-4, 4)
r['pos1'] = (r.first_corner - 1) / (r.field - 1).clip(lower=1)
h = r.groupby('horse_id', sort=False)
out = r[['race_id', 'horse_number']].copy()
out['ten_last'] = h.ten_z.shift(1)
out['ten_mean3'] = h.ten_z.transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
out['ten_best5'] = h.ten_z.transform(lambda s: s.shift(1).rolling(5, min_periods=1).min())
out['pos1_mean3'] = h.pos1.transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
out['ten_n'] = h.ten_z.transform(lambda s: s.shift(1).notna().cumsum())
gr = out.groupby('race_id')
out['ten_mean3_rk'] = gr.ten_mean3.rank(pct=True)
out['ten_mean3_rel'] = out.ten_mean3 - gr.ten_mean3.transform('mean')
out['ten_best5_rk'] = gr.ten_best5.rank(pct=True)
out['race_n_fast'] = gr.ten_mean3.transform(lambda s: (s < -0.5).sum())
out['race_ten_min'] = gr.ten_mean3.transform('min')
out['ten_gap_to_fastest'] = out.ten_mean3 - out.race_ten_min
out.to_parquet(f'{D}/features_x3.parquet')
print(out.describe().T[['count', 'mean', 'std']].round(3).to_string())
