"""上がりの指数（終盤の伸び）。point-in-time。

各過去走で
  l3_adj     = 上がり3F − E[上がり3F | 競馬場, 芝ダ, 距離, 馬場, 前半ペースの段]      （マイナス＝速い）
  l3_adj_pos = 上がり3F − E[上がり3F | 芝ダ, 距離帯, 馬場, 前半ペースの段, 4角位置の段] （同じ位置の馬と比べて）
  l3_vs_race = 上がり3F − そのレースの上がり3F（レース全体の終い）
期待値の表は 2010–2014 だけで作る（足りない組み合わせは粗い表で補う）。
各馬について、その日より前の走で：前走、直近3走平均、直近5走ベスト、レース内の順位と平均との差。
writes data/features_x5.parquet
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'year', 'horse_id', 'horse_number', 'venue_code',
                                                  'surf', 'distance', 'cond', 'last_3f', 'last_corner', 'field', 'finish'])
races = pd.read_parquet(f'{D}/races.parquet', columns=['race_id', 'pace_first3f', 'pace_last3f'])
races['race_id'] = races.race_id.astype(str)
r['race_id'] = r.race_id.astype(str)
r = r.merge(races, on='race_id', how='left')
r['date'] = pd.to_datetime(r.date)
r = r.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
ok = r.last_3f.between(30, 45) & r.finish.notna()
r.loc[~ok, 'last_3f'] = np.nan
g = r.groupby(['surf', 'distance']).pace_first3f
r['pace_bin'] = pd.cut(((r.pace_first3f - g.transform('median')) / g.transform('std')),
                       [-9, -1, -0.35, 0.35, 1, 9], labels=False)
r['dband'] = pd.cut(r.distance, [0, 1300, 1700, 2100, 9999], labels=False)
r['pos_bin'] = np.clip((r.last_corner / r.field * 5).fillna(-1).astype(int), -1, 4)
fit = r[r.year.between(2010, 2014) & r.last_3f.notna()]


def expected(keys_fine, keys_coarse, min_n=30):
    fine = fit.groupby(keys_fine).last_3f.agg(['mean', 'size'])
    coarse = fit.groupby(keys_coarse).last_3f.mean()
    e_f = fine['mean'].where(fine['size'] >= min_n).reindex(pd.MultiIndex.from_frame(r[keys_fine])).values
    e_c = coarse.reindex(pd.MultiIndex.from_frame(r[keys_coarse])).values
    return np.where(np.isnan(e_f), e_c, e_f)


r['l3_adj'] = r.last_3f - expected(['venue_code', 'surf', 'distance', 'cond', 'pace_bin'], ['surf', 'dband', 'cond'])
r['l3_adj_pos'] = r.last_3f - expected(['surf', 'dband', 'cond', 'pace_bin', 'pos_bin'], ['surf', 'dband', 'pos_bin'])
r['l3_vs_race'] = r.last_3f - r.pace_last3f
h = r.groupby('horse_id', sort=False)
out = r[['race_id', 'horse_number']].copy()
for c in ('l3_adj', 'l3_adj_pos', 'l3_vs_race'):
    out[f'{c}_last'] = h[c].shift(1)
    out[f'{c}_mean3'] = h[c].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    out[f'{c}_best5'] = h[c].transform(lambda s: s.shift(1).rolling(5, min_periods=1).min())
gr = out.groupby('race_id')
for c in ('l3_adj_mean3', 'l3_adj_best5', 'l3_adj_pos_mean3', 'l3_adj_pos_best5'):
    out[f'{c}_rk'] = gr[c].rank(pct=True)
    out[f'{c}_rel'] = out[c] - gr[c].transform('mean')
out.to_parquet(f'{D}/features_x5.parquet')
print(out.describe().T[['count', 'mean', 'std']].round(3).to_string())
