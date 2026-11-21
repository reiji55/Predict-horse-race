"""Combination-bet probabilities from a win-probability vector, and evaluation against real payouts.

Ordering model (Benter-style discounted Harville):
  P(i 1st) = p_i
  P(j 2nd | i 1st) = s_j / (1 - s_i),      s = normalise(p^gamma)
  P(k 3rd | i,j)   = t_k / (1 - t_i - t_j), t = normalise(p^delta)
"""
import itertools
import numpy as np
import pandas as pd

RATE = {'馬連': 0.775, 'ワイド': 0.775, '馬単': 0.75, '三連複': 0.75, '三連単': 0.725, '単勝': 0.80}


def norm_pow(p, g):
    q = np.power(p, g)
    return q / q.sum()


def tri_tensor(p, gamma, delta):
    """Return P[i,j,k] (ordered 1-2-3) as an (n,n,n) array with zeros on repeated indices."""
    n = len(p)
    s = norm_pow(p, gamma)
    t = norm_pow(p, delta)
    P2 = p[:, None] * s[None, :] / (1 - s[:, None])
    np.fill_diagonal(P2, 0.0)
    denom = 1 - t[:, None, None] - t[None, :, None]
    P3 = P2[:, :, None] * t[None, None, :] / np.where(denom > 1e-12, denom, np.nan)
    idx = np.arange(n)
    P3[idx, idx, :] = 0
    P3[idx, :, idx] = 0
    P3[:, idx, idx] = 0
    P3 = np.nan_to_num(P3)
    return P2, P3


def combo_tables(p, gamma, delta):
    """Return dicts of combination probabilities keyed by tuples of horse indices (0-based)."""
    n = len(p)
    P2, P3 = tri_tensor(p, gamma, delta)
    umaren = P2 + P2.T                              # unordered pair 1-2
    sanrenpuku = (P3 + P3.transpose(0, 2, 1) + P3.transpose(1, 0, 2) + P3.transpose(1, 2, 0)
                  + P3.transpose(2, 0, 1) + P3.transpose(2, 1, 0))   # symmetric
    wide = sanrenpuku.sum(axis=2)                   # pair both in top3 (each unordered pair counted once per k)
    return P2, P3, umaren, sanrenpuku, wide
