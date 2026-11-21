"""各馬の3着以内の確率（周辺確率）から、整合した3連複の同時確率を作る。

Plackett–Luce（強さ v に比例して1着を引き、残りから2着、3着を引く）を使う。
  - 全組合せの確率の合計は1、同じ馬は2回出ない
  - 'exact'：組から計算し直した各馬の3着内確率が、入力の確率に一致するよう v を反復で合わせる
  - 'odds' ：v = p/(1−p) をそのまま使う近似、'prob'：v = p をそのまま使う近似（Harville）
"""
import itertools

import numpy as np


def _ordered(v):
    S = v.sum()
    P1 = v / S
    P2 = P1[:, None] * v[None, :] / (S - v[:, None])
    np.fill_diagonal(P2, 0)
    den = S - v[:, None, None] - v[None, :, None]
    P3 = P2[:, :, None] * v[None, None, :] / np.where(den > 1e-12, den, np.inf)
    ix = np.arange(len(v))
    P3[ix, ix, :] = 0
    P3[ix, :, ix] = 0
    P3[:, ix, ix] = 0
    return P1, P2, P3


def marginals(v):
    P1, P2, P3 = _ordered(v)
    return P1 + P2.sum(axis=0) + P3.sum(axis=(0, 1))


def strengths(p, method='exact', iters=40):
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    if method == 'prob':
        return p / p.sum()
    v = p / (1 - p)
    if method == 'odds':
        return v / v.sum()
    lv = np.log(v)
    target = np.log(p / (1 - p))
    for _ in range(iters):
        m = np.clip(marginals(np.exp(lv - lv.max())), 1e-6, 1 - 1e-6)
        lv += 0.8 * (target - np.log(m / (1 - m)))
    v = np.exp(lv - lv.max())
    return v / v.sum()


def trio_table(p, method='exact'):
    """[(確率, (i,j,k))] を確率の大きい順に返す（i<j<k は 0 始まりの位置）。確率の合計は1。"""
    v = strengths(p, method)
    _, _, P3 = _ordered(v)
    T = (P3 + P3.transpose(0, 2, 1) + P3.transpose(1, 0, 2) + P3.transpose(1, 2, 0) + P3.transpose(2, 0, 1) + P3.transpose(2, 1, 0))
    n = len(v)
    out = [(T[i, j, k], (i, j, k)) for i, j, k in itertools.combinations(range(n), 3)]
    out.sort(key=lambda x: -x[0])
    return out
