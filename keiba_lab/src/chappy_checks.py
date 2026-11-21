"""チャッピーの指摘に対する追加の確認。
(1) フェルミ式（手の13項目）は本命の式に上乗せになるか：2024年で係数を合わせ、2025–26年で比べる。
(2) 「軸が飛んだ場合の保険」を入れた5点は、確率の高い順の5点より当たるか。
(3) 2つの式の5点の当たり方の重なり。
usage: python chappy_checks.py"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize

import fermi as fm
from trio_eval import load_models
from trio_lib import trio_table

M = load_models()
main, hand = M['本命の式'], M['手のフェルミ式（13項目）']
d = main.merge(hand[['race_id', 'horse_number', 'p']].rename(columns={'p': 'ph'}), on=['race_id', 'horse_number'])
d['y'] = (d.finish <= 3).astype(float)
d['yr'] = d.race_id.str[:4].astype(int)
lg = lambda p: np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6)))
d['lm'], d['lh'] = lg(d.p), lg(d.ph)
tr, te = d[d.yr == 2024], d[d.yr >= 2025].copy()


def fitted(cols, df):
    rc = pd.factorize(df.race_id)[0]
    def loss(w):
        p = fm.normalize((df[cols].values @ w), rc, None)
        return -np.mean(df.y.values * np.log(p) + (1 - df.y.values) * np.log(1 - p))
    return minimize(loss, np.r_[1.0, np.zeros(len(cols) - 1)], method='Nelder-Mead', options={'xatol': 1e-4, 'fatol': 1e-7}).x


def score(cols, w, df):
    df = df.copy()
    df['q'] = fm.normalize(df[cols].values @ w, pd.factorize(df.race_id)[0], None)
    ll = -np.mean(df.y * np.log(df.q) + (1 - df.y) * np.log(1 - df.q))
    g = df.sort_values(['race_id', 'q'], ascending=[True, False])
    g['rk'] = g.groupby('race_id').cumcount() + 1
    no1 = g[g.rk == 1].y.mean()
    in5 = (g[g.rk <= 5].groupby('race_id').y.sum() == 3).mean()
    return ll, no1, in5


print('(1) 上乗せ：2024年で係数、2025–26年で評価', te.race_id.nunique(), 'レース')
for name, cols in (('本命だけ', ['lm']), ('本命＋フェルミ', ['lm', 'lh']), ('フェルミだけ', ['lh'])):
    w = fitted(cols, tr)
    ll, no1, in5 = score(cols, w, te)
    print(f'  {name}: 係数 {np.round(w, 3)} 対数損失 {ll:.5f} 1位 {no1:.1%} 5頭に3頭 {in5:.1%}')
print('  2つの式のロジットの相関（レース内）', round(float((d.lm - d.groupby("race_id").lm.transform("mean")).corr(d.lh - d.groupby("race_id").lh.transform("mean"))), 3))


def tickets(df, col):
    rows = []
    for rid, g in df.groupby('race_id', sort=False):
        g = g.sort_values('horse_number')
        win = frozenset(np.flatnonzero(g.finish.values <= 3))
        if len(win) != 3:
            continue
        p = g[col].values
        tab = trio_table(p, 'exact')
        top = int(np.argmax(p))
        A = [frozenset(t) for _, t in tab[:5]]
        noax = [frozenset(t) for _, t in tab if top not in t]
        B = [frozenset(t) for _, t in tab[:3]] + [t for t in noax if t not in [frozenset(x) for _, x in tab[:3]]][:2]
        C = [frozenset(t) for _, t in tab[:4]] + [t for t in noax if t not in [frozenset(x) for _, x in tab[:4]]][:1]
        nA = sum(top not in t for t in A)
        rows.append((rid, win in A, win in B, win in C, nA, g.race_num.iloc[0], top in win))
    return pd.DataFrame(rows, columns=['race_id', 'A', 'B', 'C', 'nA', 'rn', 'axis_in'])


print('(2) 5点の作り方：A=確率の高い順5点、B=上位3点＋1位の馬を含まない組から2点、C=上位4点＋同1点')
T = {}
for name, df, col in (('本命の式', main, 'p'), ('フェルミ13項目', hand, 'p')):
    R = tickets(df, col)
    T[name] = R
    m = R[R.rn == 11]
    print(f'  {name}: A {R.A.mean():.1%} / B {R.B.mean():.1%} / C {R.C.mean():.1%} | 11R A {m.A.mean():.1%} / B {m.B.mean():.1%} / C {m.C.mean():.1%}'
          f' | Aの5点のうち1位の馬を含まない組の平均 {R.nA.mean():.2f} | 1位の馬が3着内 {R.axis_in.mean():.1%}、そのときのA {R[R.axis_in].A.mean():.1%}、外れたときのA {R[~R.axis_in].A.mean():.1%}')
a, b = T['本命の式'].set_index('race_id').A, T['フェルミ13項目'].set_index('race_id').A
j = pd.concat([a.rename('m'), b.rename('h')], axis=1).dropna().astype(bool)
print(f'(3) 重なり：両方的中 {(j.m & j.h).mean():.1%}、本命だけ {(j.m & ~j.h).mean():.1%}、フェルミだけ {(~j.m & j.h).mean():.1%}、どちらか {(j.m | j.h).mean():.1%}')
