"""Readable odds-free formula v2 (curated features, no missing flags: a missing value counts as average, z = 0).

strength       s_i = sum_k beta_k * z_ik
P(1st)             = softmax(s)                 over all runners
P(2nd | 1st)       = softmax(g * s)             over the rest
P(3rd | 1st, 2nd)  = softmax(d * s)             over the rest
P(top 3)           = sum over the orderings (Plackett-Luce)
usage: python formula2.py TRAIN_END TEST_YEARS TAG [RACE_ID_TO_EXPLAIN]
"""
import json
import sys

import numpy as np
import pandas as pd

from common import D, OUT, race_index, fit_clogit, group_softmax, race_logloss

train_end = int(sys.argv[1])
test_years = [int(x) for x in sys.argv[2].split(',')]
tag = sys.argv[3]
explain = sys.argv[4] if len(sys.argv) > 4 else None

F = pd.read_parquet(f'{D}/features_v2.parquet')
F = F[~F.dead_heat_win & (F.year >= 2011)].sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
F['fin'] = F.finish.fillna(99)
g_ = F.groupby('race_id')
F['rating_fill_rel'] = F.rating_fill - g_.rating_fill.transform('mean')
F['log_days_off'] = np.log1p(F.days_off)
F['long_rest'] = (F.days_off >= 180).astype(float)
F['dist_change_abs'] = F.dist_change.abs()
F['weight_change_abs'] = F.weight_change.abs()

FEATS = {
    'rating_fill_rel': '能力値（レース平均との差）',
    'rating_sd': '能力値の不確かさ',
    'last_finish_pct': '前走の着順（0=1着、1=最下位）',
    'top3_cnt5': '直近5走の3着以内の回数',
    'cls_drop5': '直近5走の平均クラス − 今回のクラス',
    'l3_pct_mean5': '直近5走の上がり順位（0=最速）',
    'pos4_pct_mean5': '直近5走の4コーナー位置（0=先頭）',
    'pace_fit': '4コーナー位置 × 先行馬の数',
    'course_top3_edge': '同じコースの3着内率の上乗せ',
    'dband_top3_edge': '同じ距離帯の3着内率の上乗せ',
    'jky_top3': '騎手の3着内率（直近2年）',
    'trn_top3': '調教師の3着内率（直近2年）',
    'impost_rel': '斤量（レース平均との差）',
    'impost_vs_own': '斤量（その馬の直近5走平均との差）',
    'log_days_off': '休養日数（対数）',
    'long_rest': '半年以上の休み明け',
    'age': '年齢',
    'dist_change_abs': '距離変更の大きさ',
    'weight_change_abs': '馬体重の増減の大きさ',
    'prize_per_run_rel': '1走あたり賞金（レース平均との差）',
}
names = list(FEATS)
tr_mask = F.year.between(2011, train_end).values
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
tab = pd.DataFrame({'feature': names, '意味': [FEATS[n] for n in names], 'beta(1SDあたり)': beta,
                    '平均': [stats[n][0] for n in names], '1SD': [stats[n][1] for n in names]})
tab['abs'] = tab['beta(1SDあたり)'].abs()
print('g (2nd) =', round(scales[2], 4), ' d (3rd) =', round(scales[3], 4))
print(tab.sort_values('abs', ascending=False).drop(columns='abs').round(4).to_string(index=False))

te = F[F.year.isin(test_years)]
out = te[['race_id', 'horse_number', 'year']].copy()
out['s1'] = S[te.index]
out['s2'] = scales[2] * S[te.index]
out['s3'] = scales[3] * S[te.index]
out.to_parquet(f'{OUT}/formula2_scores_{tag}.parquet')
json.dump({'beta': dict(zip(names, map(float, beta))), 'scales': scales, 'stats': stats, 'labels': FEATS},
          open(f'{OUT}/formula2_{tag}.json', 'w'), ensure_ascii=False, indent=1)

if explain:
    r = F[F.race_id == explain].copy()
    zr = Z[r.index]
    contrib = zr * beta
    C = pd.DataFrame(contrib, columns=names, index=r.horse_number.values)
    C = C - C.mean(axis=0)                      # relative to the race average horse
    r['s'] = S[r.index]
    # P(top3) by Plackett-Luce
    s1 = r.s.values
    p1 = np.exp(s1 - s1.max()); p1 /= p1.sum()
    e2 = np.exp(scales[2] * (s1 - s1.max())); e3 = np.exp(scales[3] * (s1 - s1.max()))
    n = len(s1)
    P2 = p1[:, None] * e2[None, :] / (e2.sum() - e2[:, None]); np.fill_diagonal(P2, 0)
    den = e3.sum() - e3[:, None, None] - e3[None, :, None]
    P3 = P2[:, :, None] * e3[None, None, :] / np.where(den > 1e-300, den, np.nan)
    ii = np.arange(n); P3[ii, ii, :] = 0; P3[ii, :, ii] = 0; P3[:, ii, ii] = 0; P3 = np.nan_to_num(P3)
    r['p_win'] = p1
    r['p_top3'] = p1 + P2.sum(axis=0) + P3.sum(axis=(0, 1))
    r['rank_top3'] = r.p_top3.rank(ascending=False).astype(int)
    print(r[['horse_number', 'finish', 'popularity', 's', 'p_win', 'p_top3', 'rank_top3']].sort_values('finish').round(3).to_string(index=False))
    C.to_csv(f'{OUT}/formula2_contrib_{explain}.csv')
    for h in (11, 9, 12, 13):
        c = C.loc[h].sort_values()
        print(f'--- #{h}: biggest minus', c.head(4).round(3).to_dict(), ' biggest plus', c.tail(5).round(3).to_dict())
    raw = pd.DataFrame({n: r[n].values for n in names}, index=r.horse_number.values)
    raw.to_csv(f'{OUT}/formula2_raw_{explain}.csv')
