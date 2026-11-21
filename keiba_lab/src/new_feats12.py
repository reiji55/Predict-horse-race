"""エリザベス女王杯（2026-11-15）の反省から足した材料。すべてレース前に分かる過去の結果だけ。

騎手の上のクラスでの成績（過去3年、当日より前）
  jky_hi_n     : オープン・重賞での騎乗数
  jky_hi_t3    : オープン・重賞での3着内率（20走ぶんの平均で縮める）
  jky_hi_edge  : 上の値 − その騎手の全レースでの3着内率（上のクラスで普段より来るか）
連続好走
  t3_streak    : 直前まで続いている3着内の連続数（10で頭打ち）
  t3_streak_all: 1 = デビューから一度も3着を外していない（1走以上）
世代
  age_f        : 年齢＋月（3歳の11月 = 3.83）
  young_vs_old : 1 = 3歳馬が古馬（4歳以上）のいるレースに出る
  older_share  : 出走馬のうち5歳以上の割合
同じ条件での直近の勝ち
  win_same_days: 同じ芝ダ・距離±200m・同じ馬場の区分（良／稍重／重・不良）で最後に勝ってからの日数（無ければ空）
  win_surf_days: 同じ芝ダで最後に勝ってからの日数
  win_same_n   : 同じ条件での勝ち数（過去3年）
writes data/features_x12.parquet
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'horse_id', 'horse_number', 'jockey_id', 'surf', 'distance',
                                                  'cond', 'cls', 'finish', 'age', 'field'])
r['race_id'] = r.race_id.astype(str)
r['date'] = pd.to_datetime(r.date)
r = r.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
r['ord'] = (r.date - pd.Timestamp('2000-01-03')).dt.days
r['done'] = r.finish.notna()
r['t3'] = (r.finish <= 3).astype(float)
r['hi'] = (r.cls >= 5).astype(float)
out = r[['race_id', 'horse_number']].copy()

# ---- 騎手：オープン・重賞での成績（過去3年、当日より前） ----
d = r[r.done].assign(n=1.0, n_hi=lambda x: x.hi, t_hi=lambda x: x.t3 * x.hi).groupby(['jockey_id', 'ord'])[['n', 't3', 'n_hi', 't_hi']].sum().reset_index()
d = d.sort_values(['jockey_id', 'ord'])
c = d.groupby('jockey_id')[['n', 't3', 'n_hi', 't_hi']].cumsum()
d[['cn', 'ct', 'cnh', 'cth']] = c.values
d = d.sort_values('ord')
key = r[['jockey_id', 'ord']].drop_duplicates().sort_values('ord')
a = pd.merge_asof(key, d[['jockey_id', 'ord', 'cn', 'ct', 'cnh', 'cth']], on='ord', by='jockey_id', allow_exact_matches=False)
key2 = key.assign(ord=key.ord - 1095).sort_values('ord')
b = pd.merge_asof(key2, d[['jockey_id', 'ord', 'cn', 'ct', 'cnh', 'cth']], on='ord', by='jockey_id', allow_exact_matches=True)
b['ord'] = b.ord + 1095
m = a.merge(b, on=['jockey_id', 'ord'], suffixes=('', '_old')).fillna(0)
for k in ('cn', 'ct', 'cnh', 'cth'):
    m[k] = m[k] - m[k + '_old']
g_hi = r.loc[r.done & (r.hi == 1), 't3'].mean()
g_all = r.loc[r.done, 't3'].mean()
m['jky_hi_n'] = m.cnh
m['jky_hi_t3'] = (m.cth + 20 * g_hi) / (m.cnh + 20)
m['jky_hi_edge'] = m.jky_hi_t3 - (m.ct + 20 * g_all) / (m.cn + 20) - (g_hi - g_all)
out = out.join(r[['jockey_id', 'ord']].merge(m[['jockey_id', 'ord', 'jky_hi_n', 'jky_hi_t3', 'jky_hi_edge']], on=['jockey_id', 'ord'], how='left')[['jky_hi_n', 'jky_hi_t3', 'jky_hi_edge']])

# ---- 連続好走 ----
h = r.sort_values(['horse_id', 'date']).copy()
h['miss'] = (~(h.t3 == 1) & h.done).astype(int)
h['blk'] = h.groupby('horse_id').miss.cumsum()
h['run_ok'] = (h.done & (h.t3 == 1)).astype(int)
h['st_after'] = h.groupby(['horse_id', 'blk']).run_ok.cumsum()
h['t3_streak'] = h.groupby('horse_id').st_after.shift(1).fillna(0).clip(upper=10)
h['miss_before'] = h.groupby('horse_id').miss.cumsum() - h.miss
h['n_before'] = h.groupby('horse_id').done.cumsum() - h.done
h['t3_streak_all'] = ((h.miss_before == 0) & (h.n_before >= 1)).astype(float)
out = out.join(h[['t3_streak', 't3_streak_all']])

# ---- 世代 ----
out['age_f'] = r.age + (r.date.dt.month - 1) / 12.0
gr = r.groupby('race_id')
out['young_vs_old'] = ((r.age == 3) & (gr.age.transform('max') >= 4)).astype(float)
out['older_share'] = gr.age.transform(lambda s: (s >= 5).mean())

# ---- 同じ条件での直近の勝ち ----
r['gg'] = r.cond.clip(upper=2)
W = r.loc[r.finish == 1, ['horse_id', 'ord', 'surf', 'distance', 'gg']].rename(columns={'ord': 'ord_w', 'surf': 'surf_w', 'distance': 'dist_w', 'gg': 'gg_w'})
T = r[['race_id', 'horse_number', 'horse_id', 'ord', 'surf', 'distance', 'gg']]
res = []
for lo in range(0, len(T), 200000):
    j = T.iloc[lo:lo + 200000].merge(W, on='horse_id')
    j = j[(j.ord_w < j.ord) & (j.surf_w == j.surf)]
    j['days'] = j.ord - j.ord_w
    same = j[((j.dist_w - j.distance).abs() <= 200) & (j.gg_w == j.gg)]
    x = j.groupby(['race_id', 'horse_number']).days.min().rename('win_surf_days').to_frame()
    x = x.join(same.groupby(['race_id', 'horse_number']).days.min().rename('win_same_days'))
    x = x.join(same[same.days <= 1095].groupby(['race_id', 'horse_number']).size().rename('win_same_n'))
    res.append(x)
x = pd.concat(res).reset_index()
out = out.merge(x, on=['race_id', 'horse_number'], how='left')
out['win_same_n'] = out.win_same_n.fillna(0)
out.to_parquet(f'{D}/features_x12.parquet')
print(len(out), len(r))
print(out.describe().T[['count', 'mean', 'std', 'min', 'max']].round(3).to_string())
