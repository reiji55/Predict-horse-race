"""Build a clean run-level table from the public warehouse parquet files.

Output: data/runs.parquet (one row per horse that actually started / was in the betting field)
        data/race_meta.parquet (one row per race)
"""
import re
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'

races = pd.read_parquet(f'{D}/races.parquet')
res = pd.read_parquet(f'{D}/results.parquet')

# ---------- race meta ----------
ci = races.class_info.str.replace('\xa0', ' ', regex=False)
races = races[~ci.str.contains('障害')].copy()          # flat races only
ci = races.class_info.str.replace('\xa0', ' ', regex=False)


def class_group(row_ci: str, name: str) -> float:
    if re.search(r'\((G1|GI)\)\s*$', name):
        return 8
    if re.search(r'\((G2|GII)\)\s*$', name):
        return 7
    if re.search(r'\((G3|GIII)\)\s*$', name):
        return 6
    if 'オープン' in row_ci:
        return 5
    if '1600万下' in row_ci or '3勝クラス' in row_ci:
        return 4
    if '1000万下' in row_ci or '2勝クラス' in row_ci:
        return 3
    if '500万下' in row_ci or '1勝クラス' in row_ci:
        return 2
    if '未勝利' in row_ci:
        return 1
    if '新馬' in row_ci:
        return 0
    return np.nan


races['cls'] = [class_group(c, n) for c, n in zip(ci, races.race_name)]
races['listed'] = races.race_name.str.contains(r'\(L\)\s*$', regex=True)
races['handicap'] = ci.str.contains('ハンデ')
races['female_only'] = ci.str.contains(r'牝\s*\[|牝\(|\s牝', regex=True)
races['age_cond'] = ci.str.extract(r'(\d歳(?:以上)?)')[0]
races['date'] = pd.to_datetime(races.date)
races['race_num'] = races.race_num.astype(int)
races['start_min'] = races.start_time.str.split(':').apply(
    lambda x: int(x[0]) * 60 + int(x[1]) if isinstance(x, list) and len(x) == 2 and x[0].isdigit() else np.nan)
races['surf'] = races.surface.map({'芝': 'T', 'ダート': 'D'})
races['cond'] = races.track_condition.map({'良': 0, '稍重': 1, '稍': 1, '重': 2, '不良': 3, '不': 3})
meta_cols = ['race_id', 'date', 'year', 'month', 'venue_code', 'venue_name', 'race_num', 'start_min', 'race_name',
             'surf', 'distance', 'direction', 'course_type', 'cond', 'weather', 'cls', 'listed', 'handicap',
             'female_only', 'age_cond', 'first_prize', 'pace_first3f', 'pace_last3f', 'lap_times']
meta = races[meta_cols].copy()

# ---------- runs ----------
res = res[res.race_id.isin(meta.race_id)].copy()
dr = res.dnf_reason.fillna('')
scratched = dr.isin(['取', '除'])
res = res[~scratched].copy()
dr = res.dnf_reason.fillna('')
fin = res.finish_position.copy()
demoted = dr.str.contains(r'\((降|再)\)')
fin.loc[demoted] = dr[demoted].str.extract(r'(\d+)')[0].astype(float)
res['finish'] = fin
res['completed'] = res.finish.notna()          # 中止・失格は NaN（＝敗戦扱い）

# odds must exist for every horse in the betting field
res = res[res.odds.notna() & (res.odds > 0)].copy()

res = res.merge(meta[['race_id', 'date', 'year', 'race_num', 'start_min', 'venue_code', 'surf', 'distance', 'cond', 'cls']],
                on='race_id', how='left')

g = res.groupby('race_id')
res['field'] = g.horse_number.transform('size')
inv = 1.0 / res.odds
res['mkt_p'] = inv / inv.groupby(res.race_id).transform('sum')          # market win prob (normalised)
res['overround'] = inv.groupby(res.race_id).transform('sum')

# winner time & margins
win_time = res[res.finish == 1].groupby('race_id').time_seconds.min()
res['win_time'] = res.race_id.map(win_time)
res['margin_sec'] = res.time_seconds - res.win_time
second_time = res[res.finish == 2].groupby('race_id').time_seconds.min()
res['second_time'] = res.race_id.map(second_time)

nwin = res[res.finish == 1].groupby('race_id').size()
res['dead_heat_win'] = res.race_id.map(nwin).fillna(0) != 1
res.loc[~res.race_id.isin(res.loc[res.finish.notna(), 'race_id']), 'dead_heat_win'] = False   # まだ走っていないレースは除外しない

res['last_3f'] = res.last_3f.where((res.last_3f > 20) & (res.last_3f < 60))
res['time_seconds'] = res.time_seconds.where(res.time_seconds > 40)
res['first_corner'] = res.first_corner.where(res.first_corner > 0)
res['last_corner'] = res.last_corner.where(res.last_corner > 0)

res = res.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
keep = ['race_id', 'date', 'year', 'race_num', 'start_min', 'venue_code', 'surf', 'distance', 'cond', 'cls',
        'horse_id', 'horse_name', 'horse_number', 'gate_number', 'sex', 'age', 'impost', 'jockey_id', 'trainer_id',
        'owner_id', 'finish', 'completed', 'dnf_reason', 'time_seconds', 'margin_sec', 'win_time', 'second_time',
        'first_corner', 'last_corner', 'passing_order', 'last_3f', 'popularity', 'odds', 'mkt_p', 'overround',
        'horse_weight', 'weight_change', 'prize_money', 'field', 'dead_heat_win']
res[keep].to_parquet(f'{D}/runs.parquet')
meta.to_parquet(f'{D}/race_meta.parquet')
print('runs', res.shape, 'races', res.race_id.nunique(), 'meta', meta.shape)
print('cls NaN', meta.cls.isna().sum(), meta.cls.value_counts().sort_index().to_dict())
print('overround', res.groupby('race_id').overround.first().describe().round(4).to_dict())
