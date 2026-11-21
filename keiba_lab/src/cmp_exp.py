"""2組の式（タグの平均どうし）を、2026-10-04までの検証レースで比べる。usage: cmp_exp.py base1,base2 new1,new2"""
import sys
import numpy as np
import pandas as pd
OUT, D = '/home/claude/keiba_lab/out', '/home/claude/keiba_lab/data'
def load(tags):
    fr = [pd.read_parquet(f'{OUT}/exp_{t}.parquet').assign(race_id=lambda d: d.race_id.astype(str)).set_index(['race_id', 'horse_number']).sort_index() for t in tags]
    e = fr[0].copy(); e['s'] = np.mean([f.s.reindex(e.index).values for f in fr], axis=0)
    return e.reset_index()
a, b = load(sys.argv[1].split(',')), load(sys.argv[2].split(','))
c = pd.read_parquet(f'{D}/features_v3.parquet', columns=['race_id', 'horse_number', 'cls', 'date', 'cond'])
c['race_id'] = c.race_id.astype(str)
m = a.merge(b[['race_id', 'horse_number', 's']], on=['race_id', 'horse_number'], suffixes=('_a', '_b')).merge(c, on=['race_id', 'horse_number'])
m = m[m.date.astype(str) < '2026-10-05']
m['grp'] = np.select([m.cls <= 1, m.cls <= 3, m.cls == 4, m.cls == 5], ['1 新馬・未勝利', '2 1-2勝', '3 3勝', '4 OP'], '5 重賞')
def stats(g, k):
    p = 1 / (1 + np.exp(-g[k])); ll = -(g.top3 * np.log(p) + (1 - g.top3) * np.log(1 - p))
    rk = g.groupby('race_id')[k].rank(ascending=False, method='first')
    in5 = g.assign(h=(rk <= 5) * g.top3).groupby('race_id').h.sum()
    return ll.groupby(g.race_id).mean(), g.loc[rk == 1].set_index('race_id').top3, (in5 == 3).astype(float), in5
rng = np.random.default_rng(0)
for name, g in [('全体', m), ('11R', m[m.race_num == 11]), ('良', m[m.cond == 0]), ('稍重', m[m.cond == 1]), ('重・不良', m[m.cond >= 2])] + list(m.groupby('grp')):
    A, B = stats(g, 's_a'), stats(g, 's_b')
    d = (B[0] - A[0]).values; bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(400)]
    print(f"{name:12s} races {len(d):5d} | logloss {A[0].mean():.5f} -> {B[0].mean():.5f} (diff {d.mean():+.5f}, 95% {np.percentile(bs, 2.5):+.5f}..{np.percentile(bs, 97.5):+.5f})"
          f" | no1 {A[1].mean():.3f} -> {B[1].mean():.3f} | all3in5 {A[2].mean():.3f} -> {B[2].mean():.3f} | top3in5 {A[3].mean():.3f} -> {B[3].mean():.3f}")
