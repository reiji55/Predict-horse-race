"""Shared helpers: per-race softmax, conditional-logit fitting, evaluation."""
import numpy as np
import pandas as pd
from scipy.optimize import minimize

D = '/home/claude/keiba_lab/data'
OUT = '/home/claude/keiba_lab/out'


def race_index(race_ids: np.ndarray):
    """Return (starts, inverse) for contiguous race blocks. race_ids must be grouped contiguously."""
    change = np.r_[True, race_ids[1:] != race_ids[:-1]]
    starts = np.flatnonzero(change)
    inv = np.cumsum(change) - 1
    return starts, inv


def group_softmax(score: np.ndarray, starts: np.ndarray, inv: np.ndarray) -> np.ndarray:
    mx = np.maximum.reduceat(score, starts)
    e = np.exp(score - mx[inv])
    s = np.add.reduceat(e, starts)
    return e / s[inv]


def race_logloss(p: np.ndarray, y: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """-log p(winner) per race (y is 1 for the winner, 0 otherwise; exactly one winner per race)."""
    lp = np.log(np.clip(p, 1e-15, 1)) * y
    return -np.add.reduceat(lp, starts)


def fit_clogit(X: np.ndarray, y: np.ndarray, starts, inv, l2: float = 0.0, w0=None, offset=None):
    """Conditional (multinomial) logit: p_i = exp(x_i.w + off_i) / sum_race exp(...). MLE with optional L2."""
    k = X.shape[1]
    off = np.zeros(len(y)) if offset is None else offset
    nr = len(starts)

    def f(w):
        s = X @ w + off
        p = group_softmax(s, starts, inv)
        ll = np.sum(np.log(np.clip(p, 1e-300, 1)) * y)
        # gradient: sum_i (y_i - p_i) x_i
        g = X.T @ (y - p)
        return -(ll - 0.5 * l2 * w @ w) / nr, -(g - l2 * w) / nr

    w = np.zeros(k) if w0 is None else w0
    r = minimize(f, w, jac=True, method='L-BFGS-B', options={'maxiter': 500})
    return r.x, r


def bootstrap_mean_ci(x: np.ndarray, n: int = 2000, seed: int = 0, alpha: float = 0.05):
    rng = np.random.default_rng(seed)
    m = len(x)
    idx = rng.integers(0, m, size=(n, m))
    bs = x[idx].mean(axis=1)
    return x.mean(), np.quantile(bs, alpha / 2), np.quantile(bs, 1 - alpha / 2)
