"""指数を決め打ちしたフェルミ式を作って測る（2011–23で倍率、2024–26で検証）。
usage: python fermi_fixed.py TAG col1,col2,...   [DROP1=1 で「1つ抜いたら」の検証も行う]
出力：out/fermi_fixed_{TAG}.json（倍率表・当たり方・堅さ指数・条件別の確率の合い方）、out/fermi_fixed_{TAG}.parquet（2024–26の確率）"""
import json
import os
import sys

import numpy as np
import pandas as pd

import fermi as fm

OUT, D = fm.OUT, fm.D
tag, cols = sys.argv[1], sys.argv[2].split(',')
F = fm.load(extra=fm.EXTRA_11R[:1])
meta = pd.read_parquet(f'{D}/features_v3.parquet', columns=['race_id', 'horse_number', 'cls', 'surf_code', 'distance'])
meta['race_id'] = meta.race_id.astype(str)
F = F.merge(meta, on=['race_id', 'horse_number'], how='left')
off = np.log(3 / (F.field - 3).clip(lower=1)).values
kinds = {c: k for c, _, k in fm.INDEX + fm.EXTRA_11R}
names = {c: n for c, n, _ in fm.INDEX + fm.EXTRA_11R}
signs = {c: (fm.direction(F, c) if kinds[c] == 'rank' else 1) for c in cols}
B = {c: fm.buckets(F, c, kinds[c], signs[c]) for c in cols}
y = F.top3.values
trf, te = (F.year <= 2023).values, (F.year >= 2024).values
dte = F[te]
rc_te = pd.factorize(F.rc.values[te])[0]


def run(sel):
    X, pos = fm.design(B, sel)
    beta = fm.fit(X[trf], y[trf], off[trf])
    return fm.normalize(off[te] + X[te] @ beta, rc_te, None), beta, pos


p, beta, pos = run(cols)
res = {'tag': tag, 'indices': [names[c] for c in cols], 'metrics': fm.metrics(p, dte)}
res['table'] = {names[c]: {lab: round(float(np.exp(b)), 2) for lab, b in zip(B[c][1], beta[pos[c][0]:pos[c][1]])} for c in cols}
out = dte[['race_id', 'horse_number', 'year', 'finish', 'top3', 'field', 'race_num', 'popularity', 'age', 'cls',
           'surf_code', 'distance']].copy()
out['p'] = p
out.to_parquet(f'{OUT}/fermi_fixed_{tag}.parquet')
q = out[out.field >= 8].copy()
q['rk'] = q.groupby('race_id').p.rank(ascending=False, method='first')
res['rank_ladder'] = q.groupby(q.rk.clip(upper=10)).top3.mean().round(3).to_dict()
q['bin'] = pd.cut(q.p, [0, .05, .1, .2, .3, .4, .5, .6, 1])
res['calibration'] = {str(k): [round(a, 3), round(b, 3), int(n)] for k, a, b, n in
                      q.groupby('bin', observed=True).agg(a=('p', 'mean'), b=('top3', 'mean'), n=('p', 'size')).itertuples()}
g = q.groupby('race_id')
R = pd.DataFrame({'S5': g.apply(lambda x: x.loc[x.rk <= 5, 'p'].sum(), include_groups=False),
                  'hit5': g.apply(lambda x: x.loc[x.rk <= 5, 'top3'].sum() == 3, include_groups=False),
                  'h1': g.apply(lambda x: x.loc[x.rk == 1, 'top3'].sum(), include_groups=False), 'rn': g.race_num.first()})
R['bin'] = pd.cut(R.S5, [0, 1.9, 2.1, 2.3, 3.01])
res['solidity_all'] = {str(k): [int(n), round(h, 3), round(a, 3)] for k, n, h, a in
                       R.groupby('bin', observed=True).agg(n=('hit5', 'size'), h=('hit5', 'mean'), a=('h1', 'mean')).itertuples()}
res['solidity_11R'] = {str(k): [int(n), round(h, 3), round(a, 3)] for k, n, h, a in
                       R[R.rn == 11].groupby('bin', observed=True).agg(n=('hit5', 'size'), h=('hit5', 'mean'), a=('h1', 'mean')).itertuples()}
# 条件別：見立ての平均と実際（上位の馬で過信していないか）
seg = {'2歳戦': q.groupby('race_id').age.transform('max') == 2, '重賞': q.cls >= 6, 'ダート1400m以下': (q.surf_code == 1) & (q.distance <= 1400),
       '芝2000m以上': (q.surf_code == 0) & (q.distance >= 2000), 'メインレース': q.race_num == 11}
res['segments'] = {}
for name, m in seg.items():
    s = q[m]
    top = s[s.rk <= 3]
    gg = s.groupby('race_id')
    in5 = gg.apply(lambda x: x.loc[x.rk <= 5, 'top3'].sum(), include_groups=False)
    res['segments'][name] = {'races': int(s.race_id.nunique()), '上位3頭の見立て': round(float(top.p.mean()), 3),
                             '上位3頭の実際': round(float(top.top3.mean()), 3),
                             '1位の3着内率': round(float(gg.apply(lambda x: x.loc[x.rk == 1, 'top3'].sum(), include_groups=False).mean()), 3),
                             '5頭に3頭': round(float((in5 == 3).mean()), 3)}
    for a in sorted(s.age.clip(upper=7).dropna().unique()):
        t = s[s.age.clip(upper=7) == a]
        if len(t) >= 400:
            res['segments'][name][f'{int(a)}歳 見立て/実際'] = [round(float(t.p.mean()), 3), round(float(t.top3.mean()), 3)]
if os.environ.get('DROP1'):
    res['drop_one'] = {}
    for c in cols:
        pp, _, _ = run([x for x in cols if x != c])
        m = fm.metrics(pp, dte)
        res['drop_one'][names[c]] = {k: round(m[k] - res['metrics'][k], 4) for k in ('logloss', 'no1_top3', 'all3_in5')}
        print('drop', names[c], res['drop_one'][names[c]], flush=True)
json.dump(res, open(f'{OUT}/fermi_fixed_{tag}.json', 'w'), ensure_ascii=False, indent=1)
print(json.dumps({k: v for k, v in res.items() if k != 'drop_one'}, ensure_ascii=False, indent=1))
