"""メインレース（11R）では手のフェルミ式が強気すぎないかを確かめ、弱める係数を決める。
13項目の倍率を 2011–23 で決め、同じ期間の 11R だけで logit = a × (式のlogit) + b の a を求める（a<1 なら強気すぎ）。
2024–26 の 11R で、弱める前後の 確率の合い方・3連複5点の見立てと実際 を比べる。"""
import json
import numpy as np
import pandas as pd
from scipy import sparse
import fermi as fm
from trio_lib import trio_table

OUT = fm.OUT
H = json.load(open(f'{OUT}/fermi_hand.json'))
cols = H['selected'] + ['trn_grade']
F = fm.load(extra=fm.EXTRA_11R[:1])
off = np.log(3 / (F.field - 3).clip(lower=1)).values
kinds = {c: k for c, _, k in fm.INDEX + fm.EXTRA_11R}
signs = {c: (fm.direction(F, c) if kinds[c] == 'rank' else 1) for c in cols}
B = {c: fm.buckets(F, c, kinds[c], signs[c]) for c in cols}
X, _ = fm.design(B, cols)
y = F.top3.values
trf = (F.year <= 2023).values
beta = fm.fit(X[trf], y[trf], off[trf])
z = off + X @ beta
F['z'] = z
res = {}
for name, mask in (('11R', (F.race_num == 11).values), ('11R以外', (F.race_num != 11).values)):
    m = trf & mask & (F.field >= 8).values
    ab = fm.fit(sparse.csr_matrix(np.column_stack([z[m], np.ones(m.sum())])), y[m], np.zeros(m.sum()), l2=0.0)
    res[name] = [round(float(ab[0]), 3), round(float(ab[1]), 3)]
print('logit の傾き a（1 より小さい＝強気すぎ）と定数 b：', res)
a = res['11R'][0]
T = F[(F.year >= 2024) & (F.race_num == 11) & (F.field >= 8)].copy()
T['rc2'] = pd.factorize(T.race_id)[0]
out = {}
for lab, k in (('そのまま', 1.0), (f'{a:.2f}乗に弱める', a)):
    T['p'] = fm.normalize(k * T.z.values, T.rc2.values, None)
    T['rk'] = T.groupby('rc2').p.rank(ascending=False, method='first')
    top = T[T.rk <= 3]
    rows = []
    for rid, g in T.groupby('race_id', sort=False):
        g = g.sort_values('horse_number')
        win = frozenset(np.flatnonzero(g.finish.values <= 3))
        if len(win) != 3:
            continue
        tab = trio_table(g.p.values, 'exact')
        rows.append((any(frozenset(t) == win for _, t in tab[:5]), sum(pr for pr, _ in tab[:5]),
                     g.loc[g.rk <= 5, 'p'].sum(), g.loc[g.rk <= 5, 'top3'].sum()))
    R = pd.DataFrame(rows, columns=['hit', 'pred', 'S5', 'in5'])
    pc = np.clip(T.p, 1e-6, 1 - 1e-6)
    out[lab] = {'対数損失': round(float(-np.mean(T.top3 * np.log(pc) + (1 - T.top3) * np.log(1 - pc))), 5),
                '上位3頭の見立て': round(float(top.p.mean()), 3), '上位3頭の実際': round(float(top.top3.mean()), 3),
                '5点の見立て': round(float(R.pred.mean()), 3), '5点の実際': round(float(R.hit.mean()), 3),
                '堅さ指数の平均': round(float(R.S5.mean()), 3), '上位5頭に入った頭数': round(float(R.in5.mean()), 3)}
    R['bin'] = pd.cut(R.pred, [0, .1, .15, .2, .3, 1])
    print(lab, out[lab])
    print(R.groupby('bin', observed=True).agg(n=('hit', 'size'), 見立て=('pred', 'mean'), 実際=('hit', 'mean'), 堅さ=('S5', 'mean')).round(3).to_string())
json.dump({'slope': res, 'eval': out}, open(f'{OUT}/fermi_11r_temp.json', 'w'), ensure_ascii=False, indent=1)
