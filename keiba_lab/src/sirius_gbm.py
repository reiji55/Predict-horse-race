"""Sirius S 2026 through the odds-free GBM: P(win), P(top3), and what pushed each horse up or down (stage-1 contributions,
grouped into families, relative to the race-average horse)."""
import json

import lightgbm as lgb
import numpy as np
import pandas as pd

from common import D, OUT

RID = '202609040811'
feats = json.load(open(f'{OUT}/pure_features.json'))
F = pd.read_parquet(f'{D}/features_v2.parquet', filters=[('race_id', '=', RID)])
F = F.sort_values('horse_number').reset_index(drop=True)
for c in ('handicap', 'female_only', 'listed'):
    F[c] = F[c].astype(int)
X = F[feats].values.astype(np.float32)
S = {}
for k in (1, 2, 3):
    bst = lgb.Booster(model_file=f'{OUT}/pure_final_v2_s{k}.txt')
    S[k] = bst.predict(X, raw_score=True)
    if k == 1:
        contrib = bst.predict(X, pred_contrib=True)[:, :-1]
n = len(F)
p1 = np.exp(S[1] - S[1].max()); p1 /= p1.sum()
e2 = np.exp(S[2] - S[2].max()); e3 = np.exp(S[3] - S[3].max())
P2 = p1[:, None] * e2[None, :] / (e2.sum() - e2[:, None]); np.fill_diagonal(P2, 0)
den = e3.sum() - e3[:, None, None] - e3[None, :, None]
P3 = P2[:, :, None] * e3[None, None, :] / np.where(den > 1e-300, den, np.nan)
ii = np.arange(n); P3[ii, ii, :] = 0; P3[ii, :, ii] = 0; P3[:, ii, ii] = 0; P3 = np.nan_to_num(P3)
F['p_win'] = p1
F['p_top3'] = p1 + P2.sum(axis=0) + P3.sum(axis=(0, 1))
F['rank'] = F.p_top3.rank(ascending=False).astype(int)


def family(c):
    if c.startswith('rating'):
        return '能力値'
    if c.startswith(('jky', 'trn', 'own')) or c == 'jockey_same':
        return '騎手・厩舎'
    if c.startswith('impost'):
        return '斤量'
    if c.startswith(('kick', 'style', 'pos4', 'l3_pct', 'last_style', 'last_kick', 'front_runners', 'fast_starters',
                     'pace_fit', 'day_')):
        return '上がり・脚質・展開'
    if c.startswith(('apt_', 'n_dist', 'best_dist', 'course_', 'vs_', 'dband_', 'dist_change', 'last_venue',
                     'surf_change', 'last_cond')):
        return 'コース・距離適性'
    if c in ('days_off', 'age', 'sex_code', 'weight_change', 'n_runs', 'n_surf') or c.startswith('horse_weight'):
        return '年齢・休養・馬体'
    if c in ('field', 'cls', 'surf_code', 'distance', 'cond', 'venue_code', 'course_code', 'month', 'race_num',
             'handicap', 'female_only', 'listed', 'gate_pct', 'gate_number'):
        return 'レース条件・枠'
    return '近走の成績・格'


C = pd.DataFrame(contrib, columns=feats, index=F.horse_number.values)
C = C - C.mean(axis=0)
fam = C.T.groupby([family(c) for c in feats]).sum().T
out = F[['horse_number', 'finish', 'popularity', 'p_win', 'p_top3', 'rank']].copy()
print(out.sort_values('finish').round(3).to_string(index=False))
print(fam.loc[[11, 9, 12, 13, 10, 6]].round(3).to_string())
top = {h: C.loc[h].sort_values() for h in (11, 9, 12, 13)}
for h, c in top.items():
    print(f'#{h} minus', c.head(4).round(3).to_dict(), '| plus', c.tail(4).round(3).to_dict())
out.to_csv(f'{OUT}/sirius_gbm.csv', index=False)
fam.to_csv(f'{OUT}/sirius_gbm_families.csv')
