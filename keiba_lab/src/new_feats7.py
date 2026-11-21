"""馬体重まわり（当日の馬体重は発走の約1時間前に発表される＝発走前に分かる）。point-in-time。
  bw_best_gap     : 当日の馬体重 − その馬が過去に3着以内だった走の馬体重の平均（3着内が無ければ空）
  bw_best_gap_abs : その絶対値
  bw_win_gap      : 当日の馬体重 − 過去の1着時の馬体重の平均
  bw_vs_mean      : 当日の馬体重 − 過去全走の平均
  bw_trend3       : 直近3走の馬体重の傾き（kg/走）
  bw_max_gap      : 当日の馬体重 − 過去最高体重
  bw_rel_field    : 当日の馬体重 − このレースの平均（大きい馬の有利不利）
writes data/features_x7.parquet
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'horse_id', 'horse_number', 'horse_weight',
                                                  'finish'])
r['race_id'] = r.race_id.astype(str)
r['date'] = pd.to_datetime(r.date)
r = r.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
w = r.horse_weight.where(r.horse_weight.between(300, 700))
r['w'] = w
r['w_t3'] = w.where(r.finish <= 3)
r['w_win'] = w.where(r.finish == 1)
h = r.groupby('horse_id', sort=False)


def prior_mean(col):
    s = r[col]
    csum = h[col].transform(lambda x: x.fillna(0).cumsum().shift(1))
    cnt = h[col].transform(lambda x: x.notna().astype(int).cumsum().shift(1))
    return (csum / cnt.replace(0, np.nan)).values


out = r[['race_id', 'horse_number']].copy()
out['bw_best_gap'] = w.values - prior_mean('w_t3')
out['bw_best_gap_abs'] = np.abs(out.bw_best_gap)
out['bw_win_gap'] = w.values - prior_mean('w_win')
out['bw_vs_mean'] = w.values - prior_mean('w')
out['bw_max_gap'] = w.values - h.w.transform(lambda x: x.shift(1).cummax()).values


def slope3(x):
    s = x.shift(1)
    a, b, c = s.shift(2), s.shift(1), s
    return (c - a) / 2


out['bw_trend3'] = h.w.transform(slope3).values
out['bw_rel_field'] = w.values - r.groupby('race_id').w.transform('mean').values
out.to_parquet(f'{D}/features_x7.parquet')
print(out.describe().T[['count', 'mean', 'std']].round(3).to_string())
