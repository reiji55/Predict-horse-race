"""A readable odds-free formula for finishing order (exploded / rank-ordered logit, depth 3).

strength  s_i = sum_k beta_k * z_ik          (z = standardised features, missing -> 0 with a flag)
P(1st)          = exp(s_i) / sum_j exp(s_j)
P(2nd | 1st)    = exp(g*s_i) / sum_{j not placed} exp(g*s_j)
P(3rd | 1st,2nd)= exp(d*s_i) / sum_{j not placed} exp(d*s_j)
beta fitted jointly on the 1st/2nd/3rd choices (same beta), g and d fitted afterwards on stages 2 and 3.
usage: python formula.py TRAIN_END TEST_YEARS TAG
"""
import json
import sys

import numpy as np
import pandas as pd

from common import D, OUT, race_index, fit_clogit, group_softmax, race_logloss

train_end = int(sys.argv[1])
test_years = [int(x) for x in sys.argv[2].split(',')]
tag = sys.argv[3]

F = pd.read_parquet(f'{D}/features.parquet')
F = F[~F.dead_heat_win & (F.year >= 2011)].sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
F['fin'] = F.finish.fillna(99)
F['log_days_off'] = np.log1p(F.days_off)
F['long_rest'] = (F.days_off >= 180).astype(float).where(F.days_off.notna())
F['log_runs'] = np.log1p(F.n_runs)
F['fr_z'] = F.front_runners - F.groupby('year').front_runners.transform('mean')
F['style_x_pace'] = F.style_mean5 * F.front_runners          # closers benefit when many front-runners
F['abs_wchg'] = F.weight_change.abs()

FEATS = {
    'rating_rel': '能力値（レース内の相対）',
    'rating_sd': '能力値の不確かさ',
    'y_mean5_rel': '直近5走の走り（相対）',
    'y_best5': '直近5走のベスト',
    'fin_pct_mean5': '直近5走の着順率（小さいほど上位）',
    'last_finish_pct': '前走の着順率',
    'kick_mean5_rel': '上がりの速さ（相対）',
    'style_mean5': '脚質（平均の位置取り、小さいほど前）',
    'style_x_pace': '脚質×逃げ・先行馬の数',
    'front_runners': '逃げ・先行馬の数',
    'apt_dist': '距離適性',
    'apt_venue': 'コース適性',
    'apt_wet': '道悪適性',
    'career_top3_rate': '通算3着内率',
    'prize_per_run_rel': '1走あたり賞金（相対）',
    'jky_top3': '騎手の3着内率',
    'trn_top3': '調教師の3着内率',
    'impost_rel': '斤量（レース内の相対）',
    'log_days_off': '休養日数（対数）',
    'long_rest': '半年以上の休み明け',
    'cls_change': 'クラス替わり（＋は昇級）',
    'dist_change': '距離変更（m）',
    'log_runs': '出走数（対数）',
    'age': '年齢',
    'abs_wchg': '馬体重の増減の大きさ',
    'gate_pct': '枠の位置（外ほど大）',
    'day_style_adv': '当日の前残り傾向',
}


def design(df, stats):
    cols, names = [], []
    for c in FEATS:
        x = df[c].values.astype(float)
        mu, sd = stats[c]
        z = np.clip(np.nan_to_num((x - mu) / sd), -4, 4)
        cols.append(z)
        names.append(c)
        if stats[c + '_miss']:
            cols.append(np.isnan(x).astype(float))
            names.append(c + '_missing')
    return np.column_stack(cols), names


tr_mask = F.year.between(2011, train_end)
stats = {}
for c in FEATS:
    x = F.loc[tr_mask, c].astype(float)
    stats[c] = (np.nanmean(x), np.nanstd(x) + 1e-9)
    stats[c + '_miss'] = bool(x.isna().mean() > 0.01)
X, names = design(F, stats)


def stage_rows(mask, place):
    d = F[mask]
    if place == 1:
        return d.index.values
    better = d.groupby('race_id').fin.transform(lambda s: (s < place).sum())
    one = d.groupby('race_id').fin.transform(lambda s: (s == place).sum())
    return d[(better == place - 1) & (one == 1) & (d.fin >= place)].index.values


# joint exploded fit: stack the three stages with distinct race keys
idx_list, y_list, key_list = [], [], []
for place in (1, 2, 3):
    ix = stage_rows(tr_mask, place)
    idx_list.append(ix)
    y_list.append((F.fin.values[ix] == place).astype(float))
    key_list.append(F.race_id.values[ix] + f'_{place}')
ix = np.concatenate(idx_list)
yy = np.concatenate(y_list)
kk = np.concatenate(key_list)
order = np.argsort(kk, kind='stable')
ix, yy, kk = ix[order], yy[order], kk[order]
s_, i_ = race_index(kk)
beta, res = fit_clogit(X[ix], yy, s_, i_, l2=1e-4)
s_all = X @ beta
# stage scale factors g (2nd) and d (3rd)
scales = {}
for place in (2, 3):
    ixp = stage_rows(tr_mask, place)
    sp, ip = race_index(F.race_id.values[ixp])
    w, _ = fit_clogit(s_all[ixp][:, None], (F.fin.values[ixp] == place).astype(float), sp, ip)
    scales[place] = float(w[0])
print('scales g, d =', scales, flush=True)

coef = pd.Series(beta, index=names)
table = pd.DataFrame({'feature': names, 'label': [FEATS.get(n, FEATS.get(n.replace('_missing', ''), '') + '（欠損）') for n in names],
                      'beta': coef.values, 'sd': [stats[n][1] if n in stats else np.nan for n in names]})
table['abs'] = table.beta.abs()
print(table.sort_values('abs', ascending=False).round(4).to_string(index=False))

te = F[F.year.isin(test_years)]
out = te[['race_id', 'horse_number', 'year']].copy()
out['s1'] = s_all[te.index]
out['s2'] = scales[2] * s_all[te.index]
out['s3'] = scales[3] * s_all[te.index]
out.to_parquet(f'{OUT}/formula_scores_{tag}.parquet')
# stage-1 log loss on test
st, it = race_index(te.race_id.values)
p1 = group_softmax(out.s1.values, st, it)
print('test win log loss', race_logloss(p1, (te.fin.values == 1).astype(float), st).mean(),
      'uniform', np.log(np.diff(np.r_[st, len(te)])).mean())
with open(f'{OUT}/formula_{tag}.json', 'w') as f:
    json.dump({'beta': coef.round(5).to_dict(), 'scales': scales,
               'stats': {k: v for k, v in stats.items() if not k.endswith('_miss')}}, f, ensure_ascii=False, indent=1)
