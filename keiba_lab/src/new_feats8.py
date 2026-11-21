"""JRAの含水率・クッション値（レース当日の朝の測定＝発走前に分かる）。2018-07〜（クッション値は2020-09〜）。
レース単位：
  trk_cushion      その日の芝のクッション値（芝のレースだけ）
  trk_moist_goal   その日の、そのレースの芝ダのゴール前の含水率
  trk_moist_turn4  同じく4コーナー
  trk_course       使用コース（A=0, B=1, C=2, D=3）
  trk_cushion_dev  クッション値 − その競馬場の平均（2018–2022）
  trk_moist_dev    含水率 − その競馬場・芝ダの平均
馬単位（point-in-time）：
  trk_best_cushion_gap : 今日のクッション値 − その馬が3着以内だった芝の走のクッション値の平均
  trk_best_moist_gap   : 今日の含水率 − その馬が3着以内だった（同じ芝ダの）走の含水率の平均
  trk_soft_edge        : 含水率が高め（その場の平均より上）の日の着順率 − 低めの日の着順率（その馬の過去、件数で縮める）
writes data/features_x8.parquet
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
VEN = {'01': '札幌', '02': '函館', '03': '福島', '04': '新潟', '05': '東京', '06': '中山', '07': '中京', '08': '京都',
       '09': '阪神', '10': '小倉'}
T = pd.read_csv(f'{D}/jra_track_archive.csv')
T['date'] = pd.to_datetime(T.date)
T = T.drop_duplicates(['date', 'venue'], keep='last')
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'horse_id', 'horse_number', 'venue_code', 'surf',
                                                  'finish', 'field'])
r['race_id'] = r.race_id.astype(str)
r['date'] = pd.to_datetime(r.date)
r['venue'] = r.venue_code.map(VEN)
r = r.merge(T[['date', 'venue', 'course', 'cushion', 'turf_goal', 'turf_turn4', 'dirt_goal', 'dirt_turn4']],
            on=['date', 'venue'], how='left')
r = r.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
turf = r.surf == 'T'
r['trk_cushion'] = r.cushion.where(turf)
r['trk_moist_goal'] = np.where(turf, r.turf_goal, r.dirt_goal)
r['trk_moist_turn4'] = np.where(turf, r.turf_turn4, r.dirt_turn4)
r['trk_course'] = r.course.map({'A': 0, 'B': 1, 'C': 2, 'D': 3})
base = r[r.date.dt.year.between(2018, 2022)]
cm = base.groupby('venue').trk_cushion.mean()
mm = base.groupby(['venue', 'surf']).trk_moist_goal.mean()
r['trk_cushion_dev'] = r.trk_cushion - r.venue.map(cm)
r['trk_moist_dev'] = r.trk_moist_goal - pd.Series(list(zip(r.venue, r.surf))).map(mm).values
ok = r.finish.notna()
r['fin_pct'] = np.where(ok, (r.finish - 1) / (r.field - 1).clip(lower=1), np.nan)
r['t3'] = (r.finish <= 3) & ok
h = r.groupby('horse_id', sort=False)


def prior_mean(vals):
    s = pd.Series(vals, index=r.index)
    c = s.fillna(0).groupby(r.horse_id).cumsum() - s.fillna(0)
    n = s.notna().astype(int).groupby(r.horse_id).cumsum() - s.notna().astype(int)
    return c / n.replace(0, np.nan)


r['cush_t3'] = r.trk_cushion.where(r.t3)
r['moist_t3'] = r.trk_moist_goal.where(r.t3)
r['trk_best_cushion_gap'] = r.trk_cushion - prior_mean(r.cush_t3.values)
# 含水率は芝とダートで水準が違うので、その場の平均との差（dev）で比べる
r['moist_dev_t3'] = r.trk_moist_dev.where(r.t3)
r['trk_best_moist_gap'] = r.trk_moist_dev - prior_mean(r.moist_dev_t3.values)
soft = r.trk_moist_dev > 0
hard = r.trk_moist_dev <= 0
k = 3
soft_mean = prior_mean(r.fin_pct.where(soft).values)
hard_mean = prior_mean(r.fin_pct.where(hard).values)
n_soft = (soft & ok).astype(int).groupby(r.horse_id).cumsum() - (soft & ok).astype(int)
n_hard = (hard & ok).astype(int).groupby(r.horse_id).cumsum() - (hard & ok).astype(int)
w = (n_soft / (n_soft + k)) * (n_hard / (n_hard + k))
r['trk_soft_edge'] = ((hard_mean - soft_mean) * w).where(r.trk_moist_dev.notna())   # プラス＝高めの日の方が良い
cols = ['trk_cushion', 'trk_moist_goal', 'trk_moist_turn4', 'trk_course', 'trk_cushion_dev', 'trk_moist_dev',
        'trk_best_cushion_gap', 'trk_best_moist_gap', 'trk_soft_edge']
r[['race_id', 'horse_number'] + cols].to_parquet(f'{D}/features_x8.parquet')
print(r[cols].describe().T[['count', 'mean', 'std']].round(3).to_string())
print('coverage by year:', r.groupby(r.date.dt.year).trk_moist_goal.apply(lambda s: round(s.notna().mean(), 3)).to_dict())
