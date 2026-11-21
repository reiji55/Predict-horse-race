"""手で計算できる式（予想チャット用）。netkeibaの出馬表と「過去5走」ページに載っている情報だけを材料にする。
オッズ・人気は使わない。形は formula2 と同じ（強さスコア → 1・2・3着の確率 → 3着以内の確率）。
学習 2011–2023、検証 2024–2026。
usage: python formula3.py
"""
import json

import numpy as np
import pandas as pd

from common import D, OUT, race_index, fit_clogit

F = pd.read_parquet(f'{D}/features_v3.parquet')
F = F[~F.dead_heat_win & (F.year >= 2011)].copy()
F['race_id'] = F.race_id.astype(str)
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'horse_id', 'horse_number', 'margin_sec',
                                                  'finish', 'surf', 'distance'])
r['race_id'] = r.race_id.astype(str)
r['date'] = pd.to_datetime(r.date)
r = r.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
r['mg'] = np.where(r.finish == 1, 0.0, r.margin_sec.clip(0, 3))
r['t3'] = (r.finish <= 3).astype(float).where(r.finish.notna())
h = r.groupby('horse_id', sort=False)
r['margin_last'] = h.mg.shift(1)
r['margin_mean5'] = h.mg.transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
# 直近5走のうち、今回と同じ芝ダで距離差200m以内の走での3着以内の回数
cnt = np.zeros(len(r))
for k in range(1, 6):
    ps, pd_, pt = h.surf.shift(k), h.distance.shift(k), h.t3.shift(k)
    same = (ps == r.surf) & ((pd_ - r.distance).abs() <= 200)
    cnt += np.where(same & (pt == 1), 1, 0)
r['sameband_top3'] = cnt
F = F.merge(r[['race_id', 'horse_number', 'margin_last', 'margin_mean5', 'sameband_top3']],
            on=['race_id', 'horse_number'], how='left')
F = F.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
F['fin'] = F.finish.fillna(99)
F['log_days_off'] = np.log1p(F.days_off)
F['long_rest'] = (F.days_off >= 180).astype(float)
F['dist_change_abs'] = F.dist_change.abs()
F['weight_change_abs'] = F.weight_change.abs()
F['cls_drop5'] = F.cls_mean5 - F.cls

FEATS = {
    'margin_mean5': '直近5走の勝ち馬との着差（秒）の平均。勝った走は0、3秒以上は3',
    'margin_last': '前走の勝ち馬との着差（秒）。勝ちは0、3秒以上は3',
    'last_finish_pct': '前走の着順率 =（着順−1）÷（頭数−1）。0=1着、1=最下位',
    'fin_pct_mean5': '直近5走の着順率の平均',
    'top3_cnt5': '直近5走の3着以内の回数（0〜5）',
    'sameband_top3': '直近5走のうち、今回と同じ芝ダ・距離差200m以内で3着以内だった回数',
    'cls_drop5': '直近5走の平均クラス − 今回のクラス（新馬0・未勝利1・1勝2・2勝3・3勝4・OP/L5・G3 6・G2 7・G1 8）',
    'pos4_pct_mean5': '直近5走の4角位置率の平均 = 通過順の最後の数字 ÷ 頭数（0に近い＝前）',
    'jky_top3': '騎手の複勝率（3着内率、直近の年間成績でよい。0.25 なら25%）',
    'trn_top3': '調教師の複勝率（同上）',
    'age': '年齢',
    'impost_rel': '斤量 − このレースの出走馬の平均斤量（kg）',
    'impost_vs_own': '斤量 − その馬の直近5走の平均斤量（kg）',
    'log_days_off': 'ln(1 + 前走からの日数)',
    'long_rest': '前走から180日以上なら1、それ以外0',
    'dist_change_abs': '|今回の距離 − 前走の距離|（m）',
    'weight_change_abs': '|当日の馬体重の増減|（kg）',
    'gate_pct': '馬番の位置 =（馬番−1）÷（頭数−1）。0=最内',
}
names = list(FEATS)
tr_mask = F.year.between(2011, 2023).values
stats = {c: (float(np.nanmean(F.loc[tr_mask, c])), float(np.nanstd(F.loc[tr_mask, c]) + 1e-9)) for c in names}
Z = np.column_stack([np.clip(np.nan_to_num((F[c].values - stats[c][0]) / stats[c][1]), -4, 4) for c in names])


def stage_rows(mask, place):
    d = F[mask]
    if place == 1:
        return d.index.values
    better = d.groupby('race_id').fin.transform(lambda s: (s < place).sum())
    one = d.groupby('race_id').fin.transform(lambda s: (s == place).sum())
    return d[(better == place - 1) & (one == 1) & (d.fin >= place)].index.values


ix_l, y_l, k_l = [], [], []
for place in (1, 2, 3):
    ix = stage_rows(tr_mask, place)
    ix_l.append(ix)
    y_l.append((F.fin.values[ix] == place).astype(float))
    k_l.append(F.race_id.values[ix] + f'_{place}')
ix, yy, kk = np.concatenate(ix_l), np.concatenate(y_l), np.concatenate(k_l)
o = np.argsort(kk, kind='stable')
ix, yy, kk = ix[o], yy[o], kk[o]
s_, i_ = race_index(kk)
beta, _ = fit_clogit(Z[ix], yy, s_, i_, l2=1e-4)
S = Z @ beta
scales = {}
for place in (2, 3):
    ixp = stage_rows(tr_mask, place)
    sp, ip = race_index(F.race_id.values[ixp])
    w, _ = fit_clogit(S[ixp][:, None], (F.fin.values[ixp] == place).astype(float), sp, ip)
    scales[place] = float(w[0])
tab = pd.DataFrame({'feature': names, '意味': [FEATS[n] for n in names], 'beta': beta,
                    'mean': [stats[n][0] for n in names], 'sd': [stats[n][1] for n in names]})
tab['abs'] = tab.beta.abs()
print('g =', round(scales[2], 4), ' d =', round(scales[3], 4))
print(tab.sort_values('abs', ascending=False).drop(columns=['abs', '意味']).round(4).to_string(index=False))
te = F[F.year >= 2024]
out = te[['race_id', 'horse_number', 'year']].copy()
out['s1'] = S[te.index]
out['s2'] = scales[2] * S[te.index]
out['s3'] = scales[3] * S[te.index]
out.to_parquet(f'{OUT}/formula3_scores.parquet')
json.dump({'beta': dict(zip(names, map(float, beta))), 'scales': scales, 'stats': stats, 'labels': FEATS},
          open(f'{OUT}/formula3.json', 'w'), ensure_ascii=False, indent=1)
