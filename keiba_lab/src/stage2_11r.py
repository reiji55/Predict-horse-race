"""メインレース（11R）で、調教評価・血統を本命の式に足すと当たり方が良くなるか。
基礎スコア s は「その年を含まない年で学習した式」の出力（out/oof_base.parquet）。
2段目：logit = a·s + b + Σ γ·(追加の材料)。Y年のレースは、Y年より前の11Rだけで係数を決める（2018〜2026年を順に）。
確率はレースごとに合計3へ調整。差の確からしさはレース単位のブートストラップ。"""
import json

import numpy as np
import pandas as pd
from scipy import sparse

from fermi import fit, normalize

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
B = pd.read_parquet(f'{OUT}/oof_base.parquet')
B['race_id'] = B.race_id.astype(str)
X9 = pd.read_parquet(f'{D}/features_x9.parquet')
pop = pd.read_parquet(f'{D}/features_v3.parquet', columns=['race_id', 'horse_number', 'popularity', 'cls'])
pop['race_id'] = pop.race_id.astype(str)
F = B.merge(X9, on=['race_id', 'horse_number'], how='left').merge(pop, on=['race_id', 'horse_number'], how='left')
F = F[(F.race_num == 11) & (F.field >= 8) & F.finish.notna() & F.s.notna()].copy()
has = F.groupby('race_id').trn_grade.transform(lambda x: x.notna().mean())
F = F[has >= 0.8].reset_index(drop=True)          # 調教評価がほぼ全頭にあるレースだけ
F['rc'] = pd.factorize(F.race_id)[0]
for g, v in (('A', 3), ('C', 1), ('D', 0)):
    F[f'g{g}'] = (F.trn_grade == v).astype(float)
F['g_none'] = F.trn_grade.isna().astype(float)
F['vs_own'] = F.trn_grade_vs_own.fillna(0)
F['critic'] = F.trn_critic_te.fillna(0) * 10
F['nA_x_A'] = F.gA * (F.trn_n_A.fillna(0) - 1).clip(0, 4)          # A が何頭もいるレースでは A の価値が薄まるか
F['sA'] = F.gA * F.s
ped = ['sire_all', 'sire_surf_edge', 'sire_dist_edge', 'sire_cond_edge', 'dsire_all', 'dsire_surf_edge', 'dsire_dist_edge']
for c in ped:
    F[c + '_z'] = ((F[c] - F.groupby('rc')[c].transform('mean')) * 20).fillna(0)
VARIANTS = {
    '基礎スコアだけ': [],
    '＋調教の記号（A・C・D）': ['gA', 'gC', 'gD', 'g_none'],
    '＋記号＋短評＋いつもとの差': ['gA', 'gC', 'gD', 'g_none', 'critic', 'vs_own', 'nA_x_A'],
    '＋血統（父・母父）だけ': [c + '_z' for c in ped],
    '＋調教＋血統（全部）': ['gA', 'gC', 'gD', 'g_none', 'critic', 'vs_own', 'nA_x_A'] + [c + '_z' for c in ped],
}
y = F.top3.values
out = {}
P = {}
coefs = {}
for name, cols in VARIANTS.items():
    p_all = np.full(len(F), np.nan)
    for Y in range(2018, 2027):
        tr, te = (F.year < Y).values, (F.year == Y).values
        X = sparse.csr_matrix(np.column_stack([F.s.values, np.ones(len(F))] + [F[c].values for c in cols]))
        beta = fit(X[tr], y[tr], np.zeros(tr.sum()), l2=0.5)
        p_all[te] = normalize(X[te] @ beta, pd.factorize(F.rc.values[te])[0], None)
        if Y == 2026:
            coefs[name] = dict(zip(['s', '定数'] + cols, np.round(beta, 3).tolist()))
    P[name] = p_all
T = F[F.year >= 2018].copy()
idx = T.index.values


def per_race(score):
    q = pd.DataFrame({'p': score, 't': T.top3.values, 'r': T.rc.values})
    q['rk'] = q.groupby('r').p.rank(ascending=False, method='first')
    pc = np.clip(q.p, 1e-6, 1 - 1e-6)
    q['ll'] = -(q.t * np.log(pc) + (1 - q.t) * np.log(1 - pc))
    g = q.groupby('r')
    return pd.DataFrame({'ll': g.ll.sum(), 'n': g.size(), 'h1': g.apply(lambda x: x.loc[x.rk == 1, 't'].sum(), include_groups=False),
                         'in5': g.apply(lambda x: x.loc[x.rk <= 5, 't'].sum(), include_groups=False),
                         'in6': g.apply(lambda x: x.loc[x.rk <= 6, 't'].sum(), include_groups=False)})


R = {k: per_race(v[idx]) for k, v in P.items()}
R['（参考）人気順'] = per_race(1 / T.popularity.fillna(99).values / 20)
rng = np.random.default_rng(0)
base = R['基礎スコアだけ']
boot = rng.integers(0, len(base), (2000, len(base)))
print(f'メインレース {len(base):,} レース（2018〜2026年）')
for k, d in R.items():
    line = (f"{k:18s} 1位の3着内率 {d.h1.mean():.1%}  上位5頭に{d.in5.mean():.2f}頭  5頭に3頭 {(d.in5 == 3).mean():.1%}  "
            f"6頭に3頭 {(d.in6 == 3).mean():.1%}")
    if '人気' not in k:
        line += f"  対数損失 {d.ll.sum() / d.n.sum():.5f}"
    if k not in ('基礎スコアだけ',):
        for lab, x in (('1位', (d.h1 - base.h1).values), ('5頭に3頭', ((d.in5 == 3).astype(int) - (base.in5 == 3).astype(int)).values),
                       ('上位5頭の頭数', (d.in5 - base.in5).values)):
            bs = x[boot].mean(axis=1)
            line += f"  | {lab}の差 {x.mean() * (100 if lab != '上位5頭の頭数' else 1):+.2f} [{np.percentile(bs, 2.5) * (100 if lab != '上位5頭の頭数' else 1):+.2f}, {np.percentile(bs, 97.5) * (100 if lab != '上位5頭の頭数' else 1):+.2f}]"
    print(line)
print('係数（2025年までの11Rで決めた値）')
print(json.dumps(coefs, ensure_ascii=False, indent=1))
# 年ごと
best = '＋記号＋短評＋いつもとの差'
yr = T.groupby('rc').year.first()
tab = pd.DataFrame({'年': yr, '基礎_1位': base.h1, '調教つき_1位': R[best].h1, '人気_1位': R['（参考）人気順'].h1,
                    '基礎_5頭に3頭': base.in5 == 3, '調教つき_5頭に3頭': R[best].in5 == 3, '人気_5頭に3頭': R['（参考）人気順'].in5 == 3})
print(tab.groupby('年').mean().round(3).to_string())
# 記号ごとの見立てと実際
T['p_base'], T['p_best'] = P['基礎スコアだけ'][idx], P[best][idx]
print(T.groupby('trn_grade').agg(n=('top3', 'size'), 実際=('top3', 'mean'), 基礎の見立て=('p_base', 'mean'), 調教つきの見立て=('p_best', 'mean')).round(3).to_string())
T[['race_id', 'horse_number', 'year', 'finish', 'top3', 'popularity', 'p_base', 'p_best']].to_parquet(f'{OUT}/stage2_11r.parquet')
