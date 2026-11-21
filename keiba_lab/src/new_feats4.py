"""序盤の位置取りを読むための材料（point-in-time）。
  pos1_sd5        : 直近5走の1角位置のばらつき
  late_start_rate : 過去の走のうち、1角位置がその馬の直前までの平均より 0.35 以上後ろだった割合（出遅れ癖の近似）
  jky_pos_bias    : 騎手ごとの「馬をいつもより前に行かせる傾向」＝ 騎手が乗った過去の走での
                    （その馬の1角位置 − その馬の直前までの平均位置）の平均（マイナス＝前に行かせる）。件数で縮める
  trn_pos_bias    : 調教師で同じもの
  jky_pos_bias_x  : 騎手の傾向 × その馬の直前までの平均位置（逃げ馬に前に行く騎手、など）
writes data/features_x4.parquet
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'start_min', 'horse_id', 'horse_number',
                                                  'jockey_id', 'trainer_id', 'first_corner', 'field'])
r['race_id'] = r.race_id.astype(str)
r['date'] = pd.to_datetime(r.date)
r = r.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
r['pos1'] = (r.first_corner - 1) / (r.field - 1).clip(lower=1)
h = r.groupby('horse_id', sort=False).pos1
r['prior_mean'] = h.transform(lambda s: s.shift(1).expanding().mean())
r['dev'] = r.pos1 - r.prior_mean                      # この走で、いつもよりどれだけ後ろだったか
out = r[['race_id', 'horse_number']].copy()
out['pos1_sd5'] = h.transform(lambda s: s.shift(1).rolling(5, min_periods=2).std())
late = (r.dev > 0.35).astype(float).where(r.dev.notna())
r['late'] = late
out['late_start_rate'] = r.groupby('horse_id', sort=False).late.transform(
    lambda s: (s.shift(1).fillna(0).cumsum() + 0.1) / (s.shift(1).notna().cumsum() + 1))


def person_bias(col):
    """その日より前の騎乗（騎手・調教師）での dev の平均。同じ日のレースは使わない。"""
    d = r[[col, 'date', 'dev']].copy()
    d['has'] = d.dev.notna().astype(float)
    d['dv'] = d.dev.fillna(0)
    day = d.groupby([col, 'date'], sort=False)[['dv', 'has']].sum().reset_index().sort_values([col, 'date'])
    day[['cdv', 'chas']] = day.groupby(col)[['dv', 'has']].cumsum().values
    day['cdv'] -= day.dv
    day['chas'] -= day.has
    m = d[[col, 'date']].merge(day[[col, 'date', 'cdv', 'chas']], on=[col, 'date'], how='left')
    return (m.cdv / (m.chas + 30)).values      # 30件分の0で縮める


out['jky_pos_bias'] = person_bias('jockey_id')
out['trn_pos_bias'] = person_bias('trainer_id')
out['prior_pos1_mean'] = r.prior_mean.values
out['jky_pos_bias_x'] = out.jky_pos_bias * (1 - out.prior_pos1_mean.fillna(0.5))
out.to_parquet(f'{D}/features_x4.parquet')
print(out.describe().T[['count', 'mean', 'std']].round(4).to_string())
