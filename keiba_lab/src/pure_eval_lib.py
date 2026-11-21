import numpy as np

GQ, DQ = 0.838, 0.737


def top3_probs(p1, e2, e3):
    n = len(p1)
    P2 = p1[:, None] * e2[None, :] / (e2.sum() - e2[:, None])
    np.fill_diagonal(P2, 0)
    den = e3.sum() - e3[:, None, None] - e3[None, :, None]
    P3 = P2[:, :, None] * e3[None, None, :] / np.where(den > 1e-300, den, np.nan)
    ix = np.arange(n)
    P3[ix, ix, :] = 0
    P3[ix, :, ix] = 0
    P3[:, ix, ix] = 0
    P3 = np.nan_to_num(P3)
    return p1 + P2.sum(axis=0) + P3.sum(axis=(0, 1))


