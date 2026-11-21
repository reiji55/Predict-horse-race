"""フェルミ推定：「馬の本当の強さを完全に知っていても、どこまで当たるか」の概算。

考え方（1走の出来 = その馬の強さ + その日のぶれ。ぶれの大きさを1とする）
  1. 同じ2頭が180日以内に再戦したとき、前回と同じ馬が先着する割合 A をデータから数える
  2. A から、レース内の「強さのばらつき」σ を逆算する（A=50%なら強さの差なし、100%ならぶれなし）
  3. σ を使って仮想レースを大量に走らせ、強さを完全に知っている人が選んだ場合の
     「1位の馬の3着内率」「上位5頭に3頭そろう率」を出す
  4. 逆に、人気順・今の式の当たり方が、σ いくつ分の「見えている強さ」に当たるかを出す
"""
import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'year', 'horse_id', 'finish', 'field'])
meta = pd.read_parquet(f'{D}/race_meta.parquet', columns=['race_id', 'cls'])
r['race_id'] = r.race_id.astype(str)
meta['race_id'] = meta.race_id.astype(str)
r = r.merge(meta, on='race_id')
r = r[(r.year >= 2016) & r.finish.notna()]
r['date'] = pd.to_datetime(r.date)
r['h'] = pd.factorize(r.horse_id)[0]
rows = []
for (rid, date, cls), g in r.groupby(['race_id', 'date', 'cls'], sort=False):
    h, f = g.h.values, g.finish.values
    ia, ib = np.triu_indices(len(h), 1)
    a, b = h[ia], h[ib]
    sw = a > b
    lo, hi = np.where(sw, b, a), np.where(sw, a, b)
    lo_ahead = np.where(sw, f[ib] < f[ia], f[ia] < f[ib])
    tie = f[ia] == f[ib]
    rows.append(pd.DataFrame({'lo': lo[~tie], 'hi': hi[~tie], 'date': date, 'cls': cls, 'lo_ahead': lo_ahead[~tie]}))
P = pd.concat(rows).sort_values(['lo', 'hi', 'date'])
g = P.groupby(['lo', 'hi'], sort=False)
P['prev_ahead'] = g.lo_ahead.shift(1)
P['gap'] = (P.date - g.date.shift(1)).dt.days
Q = P[P.prev_ahead.notna() & (P.gap <= 180)]
Q = Q.assign(agree=(Q.lo_ahead == Q.prev_ahead))


def agree_given_sigma(s, n=200001):
    d = np.random.default_rng(0).normal(0, np.sqrt(2) * s, n)
    q = norm.cdf(d / np.sqrt(2))
    return np.mean(q * q + (1 - q) * (1 - q))


def sigma_from_agree(a):
    return brentq(lambda s: agree_given_sigma(s) - a, 0.01, 5)


fields = r.groupby('race_id').field.first()
fields = fields[fields >= 8].values


def simulate(s, n=40000, seed=1):
    rng = np.random.default_rng(seed)
    N = rng.choice(fields, n)
    h1 = in5 = in6 = 0
    for k in np.unique(N):
        m = int((N == k).sum())
        abil = rng.normal(0, s, (m, k))
        perf = abil + rng.normal(0, 1, (m, k))
        top3 = np.argsort(-perf, axis=1)[:, :3]
        order = np.argsort(-abil, axis=1)
        rank_of = np.empty_like(order)
        rank_of[np.arange(m)[:, None], order] = np.arange(k)[None, :]
        rk3 = np.take_along_axis(rank_of, top3, axis=1)
        h1 += (rk3 == 0).any(axis=1).sum()
        in5 += (rk3 < 5).all(axis=1).sum()
        in6 += (rk3 < 6).all(axis=1).sum()
    return h1 / n, in5 / n, in6 / n


def sigma_for_rate(target, which):
    return brentq(lambda s: simulate(s, 12000)[which] - target, 0.05, 4)


print('再戦の一致率（前回と同じ馬が先着した割合）')
for name, sub in (('全クラス', Q), ('未勝利・1勝クラス', Q[Q.cls <= 2]), ('2勝・3勝クラス', Q[Q.cls.between(3, 4)]), ('オープン・重賞', Q[Q.cls >= 5])):
    a = sub.agree.mean()
    s = sigma_from_agree(a)
    c = simulate(s)
    print(f'  {name}: 再戦 {len(sub):,} 組、一致率 {a:.3f} → 強さのばらつき σ={s:.2f} → 強さを完全に知っていれば '
          f'1位の3着内率 {c[0]:.1%}、上位5頭に3頭 {c[1]:.1%}、上位6頭に3頭 {c[2]:.1%}')
print('当たり方から逆算した「見えている強さ」σ')
for name, h1, a5 in (('手で使える式', 0.572, 0.280), ('今の本命の式', 0.633, 0.332), ('人気順', 0.654, 0.361)):
    print(f'  {name}: 1位の3着内率 {h1:.1%} ⇔ σ={sigma_for_rate(h1, 0):.2f}、上位5頭に3頭 {a5:.1%} ⇔ σ={sigma_for_rate(a5, 1):.2f}')
print('σ ごとの早見表')
for s in (0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0):
    c = simulate(s)
    print(f'  σ={s:.1f}: 1位の3着内率 {c[0]:.1%}、上位5頭に3頭 {c[1]:.1%}、上位6頭 {c[2]:.1%}')
