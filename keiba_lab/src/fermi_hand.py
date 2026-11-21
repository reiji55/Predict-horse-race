"""手で使えるフェルミ式：netkeibaの出馬表・過去5走ページ・調教ページ（無料）で分かる指数だけを使う。
3着以内のオッズ = 3/(N−3) × Π 倍率（各指数のレース内順位、または値の段）。最後にレースごとに合計3へ調整。
指数は 2011–21 で倍率を決め、2022–23 の対数損失で前向きに選ぶ。最終の倍率は 2011–23、検証は 2024–26。
調教評価（A〜D）はメインレースにしか無いので、最後に倍率を足す（無いレースは「データなし」＝ほぼ1倍）。"""
import json

import numpy as np
import pandas as pd

import fermi as fm

OUT = fm.OUT
HAND = ['margin_mean5', 'fin_pct_mean5', 'last_finish_pct', 'top3_cnt5', 'cls_drop5', 'career_top3_rate',
        'prize_per_run', 'l3_pct_mean5', 'jky_top3', 'trn_top3', 'pos4_pct_mean5', 'impost', 'horse_weight',
        'age', 'days_off', 'n_runs', 'gate_pct', 'weight_change', 'sex_code']
F = fm.load(extra=fm.EXTRA_11R[:1])
off = np.log(3 / (F.field - 3).clip(lower=1)).values
kinds = {c: k for c, _, k in fm.INDEX + fm.EXTRA_11R}
names = {c: n for c, n, _ in fm.INDEX + fm.EXTRA_11R}
signs = {c: (fm.direction(F, c) if kinds[c] == 'rank' else 1) for c in HAND + ['trn_grade']}
B = {c: fm.buckets(F, c, kinds[c], signs[c]) for c in HAND + ['trn_grade']}
y = F.top3.values
tr, va, te = (F.year <= 2021).values, F.year.between(2022, 2023).values, (F.year >= 2024).values


def val_loss(sel):
    X, _ = fm.design(B, sel)
    beta = fm.fit(X[tr], y[tr], off[tr])
    p = np.clip(fm.normalize(off[va] + X[va] @ beta, pd.factorize(F.rc.values[va])[0], None), 1e-6, 1 - 1e-6)
    m = F.field.values[va] >= 8
    return float(-np.mean(y[va][m] * np.log(p[m]) + (1 - y[va][m]) * np.log(1 - p[m])))


sel, prev = [], 9.9
for step in range(12):
    loss, c = min((val_loss(sel + [c]), c) for c in HAND if c not in sel)
    if prev - loss < 0.0003:          # 足しても良くならなくなったら止める
        break
    sel.append(c)
    prev = loss
    print({'順': step + 1, '足した指数': names[c], '対数損失(2022–23)': round(loss, 5)}, flush=True)

trf = (F.year <= 2023).values
dte = F[te]
rc_te = pd.factorize(F.rc.values[te])[0]
res = {}
for label, cols in (('手で使えるフェルミ式', sel), ('＋調教評価（メインレース）', sel + ['trn_grade'])):
    X, pos = fm.design(B, cols)
    beta = fm.fit(X[trf], y[trf], off[trf])
    p = fm.normalize(off[te] + X[te] @ beta, rc_te, None)
    res[label] = fm.metrics(p, dte)
    table = {names[c]: {'向き': ('大きいほど上位' if signs[c] > 0 else '小さいほど上位') if kinds[c] == 'rank' else '値で分ける',
                        '倍率': {lab: round(float(np.exp(b)), 2) for lab, b in zip(B[c][1], beta[pos[c][0]:pos[c][1]])}}
             for c in cols}
    out = dte[['race_id', 'horse_number', 'year', 'finish', 'top3', 'field', 'race_num', 'popularity']].copy()
    out['p'] = p
res['（参考）人気順'] = fm.metrics_rank(-dte[dte.field >= 8].popularity.fillna(99).values, dte[dte.field >= 8])
for k, v in res.items():
    print(k, json.dumps(v, ensure_ascii=False))
out.to_parquet(f'{OUT}/fermi_hand_test.parquet')
json.dump({'selected': sel, 'table': table, 'results': res}, open(f'{OUT}/fermi_hand.json', 'w'), ensure_ascii=False, indent=1)
print(json.dumps(table, ensure_ascii=False, indent=1))
# 順位ごとの実際の3着内率と、確率の読みやすさ
q = out[out.field >= 8].copy()
q['rk'] = q.groupby('race_id').p.rank(ascending=False, method='first')
print('式の順位ごとの3着内率', q.groupby(q.rk.clip(upper=10)).top3.mean().round(3).to_dict())
q['bin'] = pd.cut(q.p, [0, .05, .1, .2, .3, .4, .5, .6, 1])
print(q.groupby('bin', observed=True).agg(n=('top3', 'size'), 見立て=('p', 'mean'), 実際=('top3', 'mean')).round(3).to_string())
