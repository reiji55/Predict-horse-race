"""Candidate features v5 (all point-in-time: only races before the current race's date are used).

  meeting   : 開催日次 (day of the meeting, 1..12), 回次
  away      : 東西の輸送（美浦の馬が関西の競馬場、栗東の馬が関東の競馬場）
  direction : 右回り・左回りの巧拙（同じ回りでの3着内率 − 全体の3着内率、件数で縮める）
  trip      : 展開補正つきの過去走
              E[着順率 | 4角位置, 前半ペースの速さ, 芝ダ] を 2010–2014 だけで作り、
              tr_resid = 実際の着順率 − 期待値（マイナス＝展開のわりに好走）
              tr_luck  = 期待値（位置とペース）− 期待値（位置だけ）（プラス＝ペースが不利に働いた）
              → 前走、直近3走平均、直近5走のベスト
  h2h       : 今回の相手との直接対決（過去に同じレースで先着した数・された数）
writes data/features_x.parquet (race_id, horse_number, new columns)
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
runs = pd.read_parquet(f'{D}/runs.parquet', columns=[
    'race_id', 'date', 'year', 'horse_id', 'horse_number', 'finish', 'field', 'first_corner', 'last_corner', 'surf',
    'distance', 'venue_code'])
races = pd.read_parquet(f'{D}/races.parquet', columns=['race_id', 'direction', 'kaisai', 'day', 'pace_first3f'])
res = pd.read_parquet(f'{D}/results.parquet', columns=['race_id', 'horse_number', 'trainer_region'])
races['race_id'] = races.race_id.astype(str)
res['race_id'] = res.race_id.astype(str)
runs['race_id'] = runs.race_id.astype(str)
runs = runs.merge(races, on='race_id', how='left').merge(res, on=['race_id', 'horse_number'], how='left')
runs['date'] = pd.to_datetime(runs.date)
runs = runs.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
out = runs[['race_id', 'horse_number']].copy()

# ---- meeting / away ----
out['meet_day'] = pd.to_numeric(runs.day, errors='coerce')
out['meet_kai'] = pd.to_numeric(runs.kaisai, errors='coerce')
east, west = {'03', '04', '05', '06'}, {'07', '08', '09', '10'}
out['away'] = (((runs.trainer_region == '東') & runs.venue_code.isin(west)) |
               ((runs.trainer_region == '西') & runs.venue_code.isin(east))).astype(float)
out['hokkaido'] = runs.venue_code.isin({'01', '02'}).astype(float)

# ---- per-run measures ----
ok = runs.finish.notna() & (runs.field >= 2)
runs['fin_pct'] = np.where(ok, (runs.finish - 1) / (runs.field - 1).clip(lower=1), np.nan)
runs['top3'] = np.where(ok, (runs.finish <= 3).astype(float), np.nan)
runs['pos4'] = runs.last_corner / runs.field
# pace: deviation of the race's first 3F from the median for the same surface and distance (in SD units)
g = runs.drop_duplicates('race_id').groupby(['surf', 'distance']).pace_first3f
med = g.transform('median')
sd = g.transform('std')
pace_z = ((runs.drop_duplicates('race_id').pace_first3f - med) / sd.replace(0, np.nan)).clip(-3, 3)
pz = pd.Series(pace_z.values, index=runs.drop_duplicates('race_id').race_id.values)
runs['pace_z'] = runs.race_id.map(pz)          # + = slower than usual, - = faster
runs['pos_bin'] = np.clip((runs.pos4 * 10).fillna(-1).astype(int), -1, 9)
runs['pace_bin'] = pd.cut(runs.pace_z, [-9, -1, -0.35, 0.35, 1, 9], labels=False)
fit = runs[ok & runs.year.between(2010, 2014) & runs.pos4.notna() & runs.pace_bin.notna()]
e_pp = fit.groupby(['surf', 'pos_bin', 'pace_bin']).fin_pct.mean().rename('e_pp')
e_p = fit.groupby(['surf', 'pos_bin']).fin_pct.mean().rename('e_p')
runs = runs.merge(e_pp, on=['surf', 'pos_bin', 'pace_bin'], how='left').merge(e_p, on=['surf', 'pos_bin'], how='left')
runs = runs.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
out = out.set_index(['race_id', 'horse_number']).loc[list(zip(runs.race_id, runs.horse_number))].reset_index()
runs['tr_resid'] = runs.fin_pct - runs.e_pp
runs['tr_luck'] = runs.e_pp - runs.e_p

# ---- per-horse history (shift by one run = only earlier races) ----
h = runs.groupby('horse_id', sort=False)
for c in ('tr_resid', 'tr_luck'):
    prev = h[c].shift(1)
    out[f'{c}_last'] = prev.values
    out[f'{c}_mean3'] = h[c].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean()).values
out['tr_resid_best5'] = h['tr_resid'].transform(lambda s: s.shift(1).rolling(5, min_periods=1).min()).values

# direction aptitude
runs['dir_r'] = (runs.direction == '右').astype(int)
runs['dir_l'] = (runs.direction == '左').astype(int)
runs['t3'] = runs.top3.fillna(0)
runs['has'] = runs.top3.notna().astype(int)
for side in ('r', 'l'):
    m = runs[f'dir_{side}']
    runs[f'n_{side}'] = (runs.has * m)
    runs[f'k_{side}'] = (runs.t3 * m)
cum = runs.groupby('horse_id', sort=False)[['has', 't3', 'n_r', 'k_r', 'n_l', 'k_l']].cumsum() - \
    runs[['has', 't3', 'n_r', 'k_r', 'n_l', 'k_l']]
same_n = np.where(runs.direction == '右', cum.n_r, np.where(runs.direction == '左', cum.n_l, np.nan))
same_k = np.where(runs.direction == '右', cum.k_r, np.where(runs.direction == '左', cum.k_l, np.nan))
overall = (cum.t3 + 0.6) / (cum.has + 3)
out['dir_n'] = same_n
out['dir_edge'] = (same_k + 3 * overall) / (same_n + 3) - overall

# ---- head to head within today's field ----
print('h2h ...', flush=True)
rid_codes, rid_index = pd.factorize(runs.race_id)
horse_codes, _ = pd.factorize(runs.horse_id)
date_num = runs.date.values.astype('datetime64[D]').astype(np.int64)
fin = runs.finish.values
starts = np.flatnonzero(np.r_[True, rid_codes[1:] != rid_codes[:-1]])
ends = np.r_[starts[1:], len(runs)]
A, B, DT, AHEAD, VALID, ROWA = [], [], [], [], [], []
for s, e in zip(starts, ends):
    idx = np.arange(s, e)
    if len(idx) < 2:
        continue
    ia, ib = np.triu_indices(len(idx), 1)
    a, b = idx[ia], idx[ib]
    fa, fb = fin[a], fin[b]
    valid = ~np.isnan(fa) & ~np.isnan(fb)
    A.append(np.r_[a, b]); B.append(np.r_[b, a])
    ahead = np.where(valid, (fa < fb).astype(np.int8), 0)
    behind = np.where(valid, (fb < fa).astype(np.int8), 0)
    AHEAD.append(np.r_[ahead, behind]); VALID.append(np.r_[valid, valid])
A = np.concatenate(A); B = np.concatenate(B); AHEAD = np.concatenate(AHEAD); VALID = np.concatenate(VALID)
pairs = pd.DataFrame({'row': A, 'ha': horse_codes[A], 'hb': horse_codes[B], 'date': date_num[A],
                      'ahead': AHEAD, 'valid': VALID.astype(np.int8)})
pairs = pairs.sort_values(['ha', 'hb', 'date']).reset_index(drop=True)
gp = pairs.groupby(['ha', 'hb'], sort=False)
pairs['prior_ahead'] = gp.ahead.cumsum() - pairs.ahead
pairs['prior_meet'] = gp.valid.cumsum() - pairs.valid
agg = pairs.groupby('row')[['prior_ahead', 'prior_meet']].sum()
out['h2h_meet'] = 0.0
out['h2h_ahead'] = 0.0
out.loc[agg.index, 'h2h_meet'] = agg.prior_meet.values
out.loc[agg.index, 'h2h_ahead'] = agg.prior_ahead.values
out['h2h_rate'] = (out.h2h_ahead + 1) / (out.h2h_meet + 2)
out['h2h_net'] = (2 * out.h2h_ahead - out.h2h_meet)
fld = runs.groupby('race_id').horse_id.transform('size').values
out['h2h_net_per'] = out.h2h_net / np.maximum(fld - 1, 1)

out.to_parquet(f'{D}/features_x.parquet')
print(out.describe().T[['count', 'mean', 'std']].round(3).to_string())
