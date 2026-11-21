"""フェルミ式をメインレース（11R）で年を追って検証する。調教評価・血統の倍率も足す。
Y年の11Rは、Y年より前の全レースで決めた倍率で予測する（2018〜2026年）。
比べるもの：12指数（順位の段）／差の大きさの段（z）／＋調教の記号／＋短評・いつもとの差／＋血統"""
import json

import numpy as np
import pandas as pd

import fermi as fm

OUT = fm.OUT
sel = json.load(open(f'{OUT}/fermi_selection.json'))['selected']
F = fm.load(extra=fm.EXTRA_11R)
off = np.log(3 / (F.field - 3).clip(lower=1)).values
kinds = {c: k for c, _, k in fm.INDEX + fm.EXTRA_11R}
names = {c: n for c, n, _ in fm.INDEX + fm.EXTRA_11R}
signs = {c: (fm.direction(F, c) if kinds[c] == 'rank' else 1) for c in kinds if c in F.columns}
B = {c: fm.buckets(F, c, kinds[c], signs[c]) for c in sel + [c for c, _, _ in fm.EXTRA_11R]}

# 差の大きさの段：レース内の平均との差を、レース内の標準偏差で割る
Z_EDGES = [-1.5, -0.75, -0.25, 0.25, 0.75, 1.5]
Z_LABELS = ['大きく下', '下', 'やや下', '並', 'やや上', '上', '大きく上', 'データなし']
BZ = {}
for c in sel:
    if kinds[c] == 'rank':
        g = F.groupby('rc')[c]
        z = ((F[c] - g.transform('mean')) / g.transform('std').replace(0, np.nan)) * signs[c]
        b = np.digitize(z.fillna(0).values, Z_EDGES)
        BZ[c] = (np.where(z.isna().values, 7, b), Z_LABELS)
    else:
        BZ[c] = B[c]

m11 = ((F.race_num == 11) & (F.field >= 8)).values
grade_ok = F.groupby('rc').trn_grade.transform(lambda x: x.notna().mean()).values >= 0.8
VAR = {
    '12指数（順位の段）': (B, sel),
    '12指数（差の大きさの段）': (BZ, sel),
    '＋調教の記号': (B, sel + ['trn_grade']),
    '＋調教の記号・短評・いつもとの差': (B, sel + ['trn_grade', 'trn_critic_te', 'trn_grade_vs_own']),
    '＋調教＋血統': (B, sel + ['trn_grade', 'trn_critic_te', 'trn_grade_vs_own', 'sire_surf_edge', 'sire_dist_edge', 'sire_all',
                         'dsire_surf_edge', 'dsire_dist_edge']),
}
y = F.top3.values
P, tables = {}, {}
for name, (BB, cols) in VAR.items():
    X, pos = fm.design(BB, cols)
    p = np.full(len(F), np.nan)
    for Y in range(2018, 2027):
        tr = (F.year < Y).values
        te = (F.year == Y).values & m11 & grade_ok
        beta = fm.fit(X[tr], y[tr], off[tr])
        p[te] = fm.normalize(off[te] + X[te] @ beta, pd.factorize(F.rc.values[te])[0], None)
        if Y == 2026:
            tables[name] = {names[c]: {lab: round(float(np.exp(b)), 2) for lab, b in zip(BB[c][1], beta[pos[c][0]:pos[c][1]])}
                            for c in cols}
    P[name] = p
    print('done', name, flush=True)
T = F[(F.year >= 2018) & m11 & grade_ok]
idx = T.index.values


def per_race(score):
    q = pd.DataFrame({'p': score, 't': T.top3.values, 'r': T.rc.values})
    q['rk'] = q.groupby('r').p.rank(ascending=False, method='first')
    g = q.groupby('r')
    return pd.DataFrame({'h1': g.apply(lambda x: x.loc[x.rk == 1, 't'].sum(), include_groups=False),
                         'in5': g.apply(lambda x: x.loc[x.rk <= 5, 't'].sum(), include_groups=False),
                         'in6': g.apply(lambda x: x.loc[x.rk <= 6, 't'].sum(), include_groups=False)})


R = {k: per_race(v[idx]) for k, v in P.items()}
R['（参考）人気順'] = per_race(1 / T.popularity.fillna(99).values)
base = R['12指数（順位の段）']
rng = np.random.default_rng(0)
boot = rng.integers(0, len(base), (2000, len(base)))
print(f'メインレース {len(base):,} レース（2018〜2026年）')
for k, d in R.items():
    line = f"{k:22s} 1位の3着内率 {d.h1.mean():.1%}  上位5頭に{d.in5.mean():.2f}頭  5頭に3頭 {(d.in5 == 3).mean():.1%}  6頭に3頭 {(d.in6 == 3).mean():.1%}"
    if k != '12指数（順位の段）':
        x = (d.h1 - base.h1).values
        bs = x[boot].mean(axis=1)
        x5 = ((d.in5 == 3).astype(int) - (base.in5 == 3).astype(int)).values
        b5 = x5[boot].mean(axis=1)
        line += f"  | 1位の差 {x.mean() * 100:+.1f} [{np.percentile(bs, 2.5) * 100:+.1f}, {np.percentile(bs, 97.5) * 100:+.1f}]  5頭に3頭の差 {x5.mean() * 100:+.1f} [{np.percentile(b5, 2.5) * 100:+.1f}, {np.percentile(b5, 97.5) * 100:+.1f}]"
    print(line)
json.dump(tables, open(f'{OUT}/fermi_11r_tables.json', 'w'), ensure_ascii=False, indent=1)
best = '＋調教の記号・短評・いつもとの差'
print(json.dumps({k: v for k, v in tables[best].items() if '調教' in k}, ensure_ascii=False, indent=1))
out = T[['race_id', 'horse_number', 'year', 'finish', 'top3', 'popularity']].copy()
out['p_fermi'] = P[best][idx]
out.to_parquet(f'{OUT}/fermi_11r.parquet')
