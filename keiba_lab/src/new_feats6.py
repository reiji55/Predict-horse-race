"""開催を通した馬場の傾向（発走前に分かる：同じ開催の、前の日までの同じ競馬場・同じ芝ダのレースだけ）。
  meet_front_adv : 4角で前（頭数比0.3未満）にいた馬の着順率の良さ − それ以外（プラス＝前が残る馬場）
  meet_inner_adv : 内枠（頭数比0.33未満）の着順率の良さ − 外枠（0.67超）（プラス＝内が有利）
  meet_n         : その計算に使った出走数
  そして、その馬に当てはめた値：
  meet_front_fit = meet_front_adv × (0.5 − その馬の予測4角位置)   （前に行きそうな馬ほど、前残り馬場の恩恵）
  meet_inner_fit = meet_inner_adv × (0.5 − 枠の頭数比)
writes data/features_x6.parquet
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'venue_code', 'surf', 'horse_number',
                                                  'gate_number', 'finish', 'field', 'last_corner'])
races = pd.read_parquet(f'{D}/races.parquet', columns=['race_id', 'kaisai'])
races['race_id'] = races.race_id.astype(str)
r['race_id'] = r.race_id.astype(str)
r = r.merge(races, on='race_id', how='left')
r['date'] = pd.to_datetime(r.date)
r['year'] = r.date.dt.year
ok = r.finish.notna() & (r.field >= 6)
r['fin_good'] = np.where(ok, 1 - (r.finish - 1) / (r.field - 1).clip(lower=1), np.nan)   # 1=1着
r['front'] = (r.last_corner / r.field < 0.3)
gate_pct = (r.horse_number - 1) / (r.field - 1).clip(lower=1)
r['inner'] = gate_pct < 0.33
r['outer'] = gate_pct > 0.67
key = ['year', 'venue_code', 'kaisai', 'surf']
# per day sums, then cumulative over earlier days of the same meeting
r['f_s'] = np.where(ok & r.front, r.fin_good, 0); r['f_n'] = (ok & r.front).astype(int)
r['b_s'] = np.where(ok & ~r.front, r.fin_good, 0); r['b_n'] = (ok & ~r.front).astype(int)
r['i_s'] = np.where(ok & r.inner, r.fin_good, 0); r['i_n'] = (ok & r.inner).astype(int)
r['o_s'] = np.where(ok & r.outer, r.fin_good, 0); r['o_n'] = (ok & r.outer).astype(int)
cols = ['f_s', 'f_n', 'b_s', 'b_n', 'i_s', 'i_n', 'o_s', 'o_n']
day = r.groupby(key + ['date'])[cols].sum().reset_index().sort_values(key + ['date'])
cum = day.groupby(key)[cols].cumsum() - day[cols]
day[[c + '_c' for c in cols]] = cum.values
r = r.merge(day[key + ['date'] + [c + '_c' for c in cols]], on=key + ['date'], how='left')
k = 20     # 縮め：20走分の「差なし」を足す
r['meet_front_adv'] = (r.f_s_c + 0.5 * k) / (r.f_n_c + k) - (r.b_s_c + 0.5 * k) / (r.b_n_c + k)
r['meet_inner_adv'] = (r.i_s_c + 0.5 * k) / (r.i_n_c + k) - (r.o_s_c + 0.5 * k) / (r.o_n_c + k)
r['meet_n'] = r.f_n_c + r.b_n_c
sub = pd.read_parquet(f'{D}/features_sub.parquet', columns=['race_id', 'horse_number', 'sub_pos4'])
sub['race_id'] = sub.race_id.astype(str)
r = r.merge(sub, on=['race_id', 'horse_number'], how='left')
r['meet_front_fit'] = r.meet_front_adv * (0.5 - r.sub_pos4)
r['meet_inner_fit'] = r.meet_inner_adv * (0.5 - (r.horse_number - 1) / (r.field - 1).clip(lower=1))
out = r[['race_id', 'horse_number', 'meet_front_adv', 'meet_inner_adv', 'meet_n', 'meet_front_fit', 'meet_inner_fit']]
out.to_parquet(f'{D}/features_x6.parquet')
print(out.describe().T[['count', 'mean', 'std']].round(4).to_string())
