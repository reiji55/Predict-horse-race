"""逆算：式の見立て（p_m0、どの年も式を作るのに使っていない予測）と実際の3着以内のずれが、
式に入っていない条件で偏っていないかを調べる。2023年で見つけ、2024–2026で同じ向きか確かめる。"""
import numpy as np
import pandas as pd

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
P = pd.concat([pd.read_parquet(f'{OUT}/top3_dev2023_v3.parquet'), pd.read_parquet(f'{OUT}/top3_final_v3_holdout.parquet')])
P = P[P.field >= 8 & P.finish.notna()] if False else P[(P.field >= 8) & P.finish.notna()]
X = pd.read_parquet(f'{D}/features_x.parquet')
X['race_id'] = X.race_id.astype(str)
P['race_id'] = P.race_id.astype(str)
B = pd.read_parquet(f'{D}/features_v3.parquet', columns=['race_id', 'horse_number', 'gate_pct', 'days_off', 'age',
                                                        'cond', 'month', 'sex_code', 'surf_code'])
B['race_id'] = B.race_id.astype(str)
P = P.merge(X, on=['race_id', 'horse_number'], how='left').merge(B, on=['race_id', 'horse_number'], how='left')
P['resid'] = P.top3 - P.p_m0
P['period'] = np.where(P.year == 2023, 'find(2023)', 'check(24-26)')
# interactions that the model might not capture well
P['late_meet_outer'] = np.where(P.meet_day >= 7, P.gate_pct, np.nan)
P['early_meet_outer'] = np.where(P.meet_day <= 2, P.gate_pct, np.nan)
cands = ['meet_day', 'away', 'hokkaido', 'tr_resid_last', 'tr_resid_mean3', 'tr_luck_last', 'tr_luck_mean3',
         'tr_resid_best5', 'dir_edge', 'h2h_net_per', 'h2h_rate', 'h2h_meet', 'late_meet_outer', 'early_meet_outer']
rows = []
for c in cands:
    s = P[c]
    if s.nunique() <= 3:
        bins = s
    else:
        try:
            bins = pd.qcut(s.rank(method='first'), 5, labels=False)
        except ValueError:
            continue
    t = P.assign(b=bins).groupby(['b', 'period']).agg(n=('resid', 'size'), resid=('resid', 'mean')).unstack('period')
    # extremes: lowest vs highest bin
    lo, hi = t.index.min(), t.index.max()
    f = t.loc[hi, ('resid', 'find(2023)')] - t.loc[lo, ('resid', 'find(2023)')]
    k = t.loc[hi, ('resid', 'check(24-26)')] - t.loc[lo, ('resid', 'check(24-26)')]
    nk = min(t.loc[hi, ('n', 'check(24-26)')], t.loc[lo, ('n', 'check(24-26)')])
    se = np.sqrt(2 * 0.17 / nk)
    rows.append({'cand': c, 'gap_2023': round(f * 100, 2), 'gap_24_26': round(k * 100, 2),
                 'z_24_26': round(k / se, 1), 'same_sign': np.sign(f) == np.sign(k)})
R = pd.DataFrame(rows).sort_values('z_24_26', key=abs, ascending=False)
print('ずれ（実際の3着内率 − 式の見立て, ポイント）：最上位の段 − 最下位の段')
print(R.to_string(index=False))
