"""部分ごとの式を作り、その予測値を本命の式に代入するための材料を作る（複数の式の連結）。

部分の式（どれも発走前の材料だけ。学習年の予測は、その年を含まない年で作った式で出す＝自分の答えを見ない）
  pos1   : 1角の位置（頭数比）            … テンの近似値も材料に入れる
  pos4   : 4角の位置
  l3     : 上がり3Fのレース内順位（0=最速）
  margin : 勝ち馬からの着差（1000mあたり秒、3秒で頭打ち）
  pace   : レースの前半3Fの速さ（同じ芝ダ・距離の中で標準化、マイナス＝速い）… レース単位の式。
           材料はレースの条件と、出走馬のテン・脚質の集計
代入・掛け合わせ
  sub_trip_fin  : E[着順率 | 予測4角位置, 予測ペース, 芝ダ]（2010–2014の実績表から引く）
  sub_trip_luck : 上の値 − E[着順率 | 予測4角位置, 芝ダ]（予測したペースが位置に対して有利か不利か）
  sub_pos4_x_pace, sub_l3_x_pace : 予測位置・予測上がり × 予測ペース
  それぞれのレース内順位
writes data/features_sub.parquet
"""
import json

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from models_lib import load_features
from pre_lib import FAST, keep, save_lgb, load_lgb

D = '/home/claude/keiba_lab/data'
ODDS = {'last_logodds', 'last_pop_pct', 'last_mkt_beat', 'logodds_mean5', 'mkt_beat_mean5', 'logodds_mean5_rel',
        'logodds_mean5_rk', 'mkt_beat_mean5_rel', 'mkt_beat_mean5_rk', 'jky_mratio', 'trn_mratio', 'own_mratio',
        'jky90_mratio', 'log_pi', 'pi_rank'}
F, FUND = load_features(f'{D}/features_v5.parquet')
F['race_id'] = F.race_id.astype(str)
X3 = pd.read_parquet(f'{D}/features_x3.parquet')
X3['race_id'] = X3.race_id.astype(str)
F = F.merge(X3, on=['race_id', 'horse_number'], how='left')
FEATS = [c for c in FUND if c not in ODDS and not c.startswith(('pred_', 'n_pred_front'))] + \
    [c for c in X3.columns if c not in ('race_id', 'horse_number')]
FEATS = keep(FEATS)
OLD = pd.read_parquet(f'{D}/features_sub.parquet') if FAST else None
if FAST:
    OLD['race_id'] = OLD.race_id.astype(str)
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'first_corner', 'last_corner',
                                                  'last_3f', 'margin_sec'])
r['race_id'] = r.race_id.astype(str)
F = F.merge(r, on=['race_id', 'horse_number'], how='left')
races = pd.read_parquet(f'{D}/races.parquet', columns=['race_id', 'pace_first3f'])
races['race_id'] = races.race_id.astype(str)
F = F.merge(races, on='race_id', how='left')

F['t_pos1'] = F.first_corner / F.field
F['t_pos4'] = F.last_corner / F.field
F['t_l3'] = F.groupby('race_id').last_3f.rank(pct=True)
F['t_margin'] = F.margin_sec.clip(0, 3) / F.distance * 1000
rk = F.drop_duplicates('race_id')[['race_id', 'surf', 'distance', 'pace_first3f']]
g = rk.groupby(['surf', 'distance']).pace_first3f
rk = rk.assign(t_pace=((rk.pace_first3f - g.transform('median')) / g.transform('std')).clip(-3, 3))
F = F.merge(rk[['race_id', 't_pace']], on='race_id', how='left')

pp = dict(objective='regression', learning_rate=0.08, num_leaves=63, min_data_in_leaf=300, feature_fraction=0.7,
          bagging_fraction=0.8, bagging_freq=1, lambda_l2=10.0, max_bin=127, num_threads=2, seed=1, verbosity=-1)
FOLDS = [(2011, 2013), (2014, 2016), (2017, 2019), (2020, 2022), (2023, 2026)]
report = {}


def oof(frame, feats, target, rounds=400, params=pp):
    pred = pd.Series(np.nan, index=frame.index)
    ok = frame[target].notna()
    name = 'sub_' + target[2:]
    if FAST:                                 # 2022年までの行は前回の値、2023年以降は保存した式
        if 'horse_number' in frame.columns:
            pred[:] = frame[['race_id', 'horse_number']].merge(OLD[['race_id', 'horse_number', name]], on=['race_id', 'horse_number'], how='left')[name].values
        else:
            pred[:] = frame[['race_id']].merge(OLD.drop_duplicates('race_id')[['race_id', name]], on='race_id', how='left')[name].values
        test = frame.year >= 2023
        pred[test] = load_lgb(name, feats).predict(frame.loc[test, feats].values.astype(np.float32))
        assert pred[frame.year < 2023].notna().all()
    for lo, hi in ([] if FAST else FOLDS):
        test = frame.year.between(lo, hi)
        train = frame.year.between(2011, 2022) & ~test & ok
        b = lgb.train(params, lgb.Dataset(frame.loc[train, feats].values.astype(np.float32),
                                          frame.loc[train, target].values), rounds)
        pred[test] = b.predict(frame.loc[test, feats].values.astype(np.float32))
        if lo == 2023:
            save_lgb(b, name, feats)
    m = (frame.year >= 2024) & ok
    report[target] = round(float(np.corrcoef(frame.loc[m, target], pred[m])[0, 1] ** 2), 3)
    print(target, 'R2(2024-26)=', report[target], flush=True)
    return pred


for t in ('t_pos1', 't_pos4', 't_l3', 't_margin'):
    F['sub_' + t[2:]] = oof(F, FEATS, t)

# race-level pace model
agg = F.groupby('race_id').agg(
    year=('year', 'first'), surf_code=('surf_code', 'first'), distance=('distance', 'first'),
    venue_code=('venue_code', 'first'), cls=('cls', 'first'), cond=('cond', 'first'), field=('field', 'first'),
    month=('month', 'first'), course_code=('course_code', 'first'),
    ten_min=('ten_mean3', 'min'), ten_mean=('ten_mean3', 'mean'), ten_q25=('ten_mean3', lambda s: s.quantile(0.25)),
    n_fast=('race_n_fast', 'first'), style_mean=('style_mean5', 'mean'), style_min=('style_mean5', 'min'),
    pos1_pred_min=('sub_pos1', 'min'), pos1_pred_mean=('sub_pos1', 'mean'),
    n_front_pred=('sub_pos1', lambda s: (s < 0.25).sum()), t_pace=('t_pace', 'first')).reset_index()
AGG_FEATS = [c for c in agg.columns if c not in ('race_id', 'year', 't_pace')]
pr = dict(pp, num_leaves=31, min_data_in_leaf=100)
agg['sub_pace'] = oof(agg, AGG_FEATS, 't_pace', rounds=300, params=pr)
F = F.merge(agg[['race_id', 'sub_pace']], on='race_id', how='left')

# substitute into the trip table (built from actual positions and actual pace, 2010–2014 only)
runs = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'year', 'finish', 'field', 'last_corner', 'surf'])
runs['race_id'] = runs.race_id.astype(str)
runs = runs.merge(rk[['race_id', 't_pace']], on='race_id', how='left')
runs = runs[runs.year.between(2010, 2014) & runs.finish.notna() & runs.last_corner.notna() & runs.t_pace.notna()]
runs['fin_pct'] = (runs.finish - 1) / (runs.field - 1).clip(lower=1)
PB = [-9, -1, -0.35, 0.35, 1, 9]
runs['pos_bin'] = np.clip((runs.last_corner / runs.field * 10).astype(int), 0, 9)
runs['pace_bin'] = pd.cut(runs.t_pace, PB, labels=False)
e_pp = runs.groupby(['surf', 'pos_bin', 'pace_bin']).fin_pct.mean()
e_p = runs.groupby(['surf', 'pos_bin']).fin_pct.mean()
pos_bin = np.clip((F.sub_pos4 * 10).fillna(5).astype(int), 0, 9)
pace_bin = pd.cut(F.sub_pace, PB, labels=False).fillna(2).astype(int)
F['sub_trip_fin'] = e_pp.reindex(list(zip(F.surf, pos_bin, pace_bin))).values
F['sub_trip_luck'] = F.sub_trip_fin - e_p.reindex(list(zip(F.surf, pos_bin))).values
F['sub_pos4_x_pace'] = F.sub_pos4 * F.sub_pace
F['sub_l3_x_pace'] = F.sub_l3 * F.sub_pace
gr = F.groupby('race_id')
for c in ('sub_pos1', 'sub_pos4', 'sub_l3', 'sub_margin', 'sub_trip_fin', 'sub_trip_luck'):
    F[c + '_rk'] = gr[c].rank(pct=True)
SUB = [c for c in F.columns if c.startswith('sub_')]
F[['race_id', 'horse_number'] + SUB].to_parquet(f'{D}/features_sub.parquet')
print(json.dumps({'r2': report, 'cols': SUB}, ensure_ascii=False))
