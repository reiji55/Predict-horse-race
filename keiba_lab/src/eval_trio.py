"""式の組み合わせごとに、3連複5点の的中と「1日3レース選び」の成績を出す。
usage: eval_trio.py NAME tag1,tag2,...   → out/trio_{NAME}.parquet（レースごと）と要約
検証期間（〜2026-10-04）と、式づくりに使っていない期間（2026-10-10〜）を分けて表示する。"""
import sys
import numpy as np
import pandas as pd
import fermi as fm
from trio_lib import trio_table
OUT, D = '/home/claude/keiba_lab/out', '/home/claude/keiba_lab/data'
name, tags = sys.argv[1], sys.argv[2].split(',')
fr = [pd.read_parquet(f'{OUT}/exp_{t}.parquet').assign(race_id=lambda d: d.race_id.astype(str)).set_index(['race_id', 'horse_number']).sort_index() for t in tags]
b = fr[0].reset_index()[['race_id', 'horse_number', 'top3', 'race_num']]
b['s'] = np.mean([f.s.reindex(fr[0].index).values for f in fr], axis=0)
c = pd.read_parquet(f'{D}/features_v3.parquet', columns=['race_id', 'horse_number', 'cls', 'date'])
c['race_id'] = c.race_id.astype(str); c['date'] = c.date.astype(str).str[:10]
b = b.merge(c, on=['race_id', 'horse_number'])
b['p'] = fm.normalize(b.s.values, pd.factorize(b.race_id)[0], None)
rows = []
for rid, g in b.groupby('race_id', sort=False):
    g = g.sort_values('horse_number')
    tab = trio_table(g.p.values, 'exact')[:5]
    win = frozenset(np.where(g.top3.values == 1)[0]) if g.top3.sum() == 3 else None
    o = np.argsort(-g.p.values)
    rows.append(dict(race_id=rid, date=g.date.iloc[0], rn=int(g.race_num.iloc[0]), cls=g.cls.iloc[0], n=len(g), sum5=sum(p for p, _ in tab),
                     hit=int(any(frozenset(t) == win for _, t in tab)), no1=int(g.top3.values[o[0]]),
                     in5=int(g.top3.values[o[:5]].sum()), in3=int(g.top3.values[o[:3]].sum())))
n = pd.DataFrame(rows)
n['rk'] = n.groupby('date').sum5.rank(ascending=False, method='first')
n.to_parquet(f'{OUT}/trio_{name}.parquet')
for lab, q in (('検証期間', n[n.date < '2026-10-05']), ('10/10以降', n[n.date >= '2026-10-10'])):
    t = q[q.rk <= 3]
    print(f"{name} {lab}: races {len(q)} 5点的中 {q.hit.mean():.4f} (見立て {q.sum5.mean():.4f}) | 1番手 {q.no1.mean():.4f} | 上位5頭に入る数 {q.in5.mean():.4f} 3頭とも {(q.in5 == 3).mean():.4f} | 上位3頭に入る数 {q.in3.mean():.4f}"
          f" | 1日3レース: {len(t)}R 的中 {t.hit.mean():.4f} (見立て {t.sum5.mean():.4f}) | 11R {q[q.rn == 11].hit.mean():.4f} | OP以上 {q[q.cls >= 5].hit.mean():.4f}", flush=True)
