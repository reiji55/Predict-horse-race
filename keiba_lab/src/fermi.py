"""重要指数の判定と、フェルミ式（基準の確率 × 指数ごとの倍率）。オッズ・人気は使わない。

1. 指数ごとの「単独の当てる力」：その指数だけでレース内の順位をつけたときの
   1位の3着内率・上位5頭に3頭そろう率・レース内AUC（2024–26、8頭立て以上）
2. フェルミ式：3着以内のオッズ = 基準オッズ 3/(N−3) × Π 倍率_k(その馬の指数kのレース内順位の段)
   倍率は 2011–2021 で同時に推定（ロジスティック回帰、段のダミー）。指数は 2022–23 の対数損失で前向きに選ぶ。
   最後に 2011–2023 で倍率を決め直し、2024–26 で測る。確率はレースごとに合計が3になるよう調整。
   比較用に「指数ごとに独立に作った倍率の掛け算（素朴版）」も測る。
usage: python fermi.py [MAXK]
"""
import json
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from scipy import sparse
from scipy.optimize import minimize

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
MAXK = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 12

# (列, 表示名, 種類)  種類: 'rank'＝レース内順位の段, それ以外＝値そのものの段（区切りのリスト）
INDEX = [
    ('rating', '能力指数（着差から更新する強さ）', 'rank'),
    ('fig_best5', 'タイム指数（近5走ベスト）', 'rank'),
    ('fig_mean3', 'タイム指数（近3走平均）', 'rank'),
    ('fig_best_band', 'タイム指数（同じ距離帯ベスト）', 'rank'),
    ('margin_mean5', '近5走の着差の平均', 'rank'),
    ('fin_pct_mean5', '近5走の着順の平均', 'rank'),
    ('last_finish_pct', '前走の着順', 'rank'),
    ('resid_mean5', '近5走の内容（相手の強さを考慮）', 'rank'),
    ('tr_resid_mean3', '展開を差し引いた近3走の成績', 'rank'),
    ('top3_cnt5', '近5走の3着内回数', 'rank'),
    ('cls_form_mean5', '格つきの近走成績', 'rank'),
    ('cls_drop5', 'クラスの下がり幅（近5走の平均クラス−今回）', 'rank'),
    ('career_top3_rate', '通算の3着内率', 'rank'),
    ('prize_per_run', '1走あたり賞金', 'rank'),
    ('l3_pct_mean5', '上がり順位（近5走）', 'rank'),
    ('l3_adj_mean3', '上がり指数（近3走）', 'rank'),
    ('jky_top3', '騎手の3着内率', 'rank'),
    ('trn_top3', '調教師の3着内率', 'rank'),
    ('own_top3', '馬主の3着内率', 'rank'),
    ('dband_top3_edge', '距離適性', 'rank'),
    ('course_top3_edge', 'コース適性', 'rank'),
    ('pos4_pct_mean5', '脚質（4角位置の平均）', 'rank'),
    ('ten_mean3', 'テン（前半の速さ）', 'rank'),
    ('tr_luck_mean3', '近走の展開の不利', 'rank'),
    ('lvl_now', '前走のレースレベル', 'rank'),
    ('impost', '斤量', 'rank'),
    ('horse_weight', '馬体重', 'rank'),
    ('meet_front_fit', '開催の馬場傾向との相性', 'rank'),
    ('sub_margin', '（参考）合成：予測着差', 'rank'),
    ('age', '年齢', [2.5, 3.5, 4.5, 5.5, 6.5]),
    ('days_off', '休養日数', [14.5, 28.5, 63.5, 120.5, 240.5]),
    ('n_runs', 'キャリア（出走数）', [0.5, 2.5, 5.5, 10.5, 20.5]),
    ('gate_pct', '馬番の位置（内〜外）', [0.2, 0.4, 0.6, 0.8]),
    ('weight_change', '馬体重の増減', [-9.5, -3.5, 3.5, 9.5]),
    ('sex_code', '性別', [0.5, 1.5]),
]
EXTRA_11R = [
    ('trn_grade', '調教評価（A〜D）', [0.5, 1.5, 2.5]),
    ('trn_grade_vs_own', '調教評価：いつもとの差', [-0.75, -0.25, 0.25, 0.75]),
    ('trn_critic_te', '調教の短評', 'rank'),
    ('sire_surf_edge', '父：芝ダ適性', 'rank'),
    ('sire_dist_edge', '父：距離適性', 'rank'),
    ('sire_all', '父：産駒の成績', 'rank'),
    ('dsire_surf_edge', '母父：芝ダ適性', 'rank'),
    ('dsire_dist_edge', '母父：距離適性', 'rank'),
]
RANK_EDGES = [1.5, 2.5, 3.5, 5.5, 8.5, 12.5]
RANK_LABELS = ['1位', '2位', '3位', '4〜5位', '6〜8位', '9〜12位', '13位以下', 'データなし']


def load(extra=()):
    cols = [c for c, _, _ in INDEX + list(extra)]
    F = pd.read_parquet(f'{D}/features_v3.parquet', columns=['race_id', 'horse_number', 'year', 'finish', 'field',
                                                             'race_num', 'popularity', 'dead_heat_win'] +
                        [c for c in cols if c in pq.read_schema(f'{D}/features_v3.parquet').names])
    F['race_id'] = F.race_id.astype(str)
    for fn in ('features_x', 'features_x3', 'features_x5', 'features_x6', 'features_sub', 'features_x9'):
        have = [c for c in cols if c in pq.read_schema(f'{D}/{fn}.parquet').names and c not in F.columns]
        if have:
            X = pd.read_parquet(f'{D}/{fn}.parquet', columns=['race_id', 'horse_number'] + have)
            X['race_id'] = X.race_id.astype(str)
            F = F.merge(X, on=['race_id', 'horse_number'], how='left')
    # 近5走の着差の平均（勝ちは0、3秒で頭打ち）
    r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'horse_id', 'horse_number', 'margin_sec', 'finish'])
    r['race_id'] = r.race_id.astype(str)
    r = r.sort_values(['date', 'race_id', 'horse_number'])
    r['mg'] = np.where(r.finish == 1, 0.0, r.margin_sec.clip(0, 3))
    r['margin_mean5'] = r.groupby('horse_id', sort=False).mg.transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
    F = F.merge(r[['race_id', 'horse_number', 'margin_mean5']], on=['race_id', 'horse_number'], how='left')
    F = F[(F.year >= 2011) & F.finish.notna() & (F.field >= 6) & ~F.dead_heat_win].reset_index(drop=True)
    F['top3'] = (F.finish <= 3).astype(float)
    F['rc'] = pd.factorize(F.race_id)[0]
    return F


def direction(F, col):
    """学習年で、レース平均より上の馬の3着内率が高ければ +1（大きいほど良い）。"""
    d = F[F.year <= 2021]
    rel = d[col] - d.groupby('rc')[col].transform('mean')
    hi, lo = d.top3[rel > 0].mean(), d.top3[rel < 0].mean()
    return 1 if hi >= lo else -1


def buckets(F, col, kind, sign):
    """各馬の段（整数）と段の名前。欠損は最後の段。"""
    if kind == 'rank':
        rk = (F[col] * -sign).groupby(F.rc).rank(method='min')
        b = np.digitize(rk.fillna(0).values, RANK_EDGES)
        b = np.where(rk.isna().values, len(RANK_EDGES) + 1, b)
        return b, RANK_LABELS
    v = F[col].values
    b = np.digitize(np.nan_to_num(v), kind)
    b = np.where(np.isnan(v), len(kind) + 1, b)
    labels = [f'〜{kind[0]}'] + [f'{kind[i]}〜{kind[i + 1]}' for i in range(len(kind) - 1)] + [f'{kind[-1]}〜', 'データなし']
    return b, labels


def normalize(logit, rc, field, iters=40):
    """レースごとに、確率の合計が min(3, 頭数) になるよう定数を足す。"""
    n_r = rc.max() + 1
    target = np.minimum(3, np.bincount(rc, minlength=n_r)).astype(float)
    c = np.zeros(n_r)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(logit + c[rc])))
        c -= (np.bincount(rc, p, n_r) - target) / np.maximum(np.bincount(rc, p * (1 - p), n_r), 1e-9)
        c = np.clip(c, -30, 30)
    return 1 / (1 + np.exp(-(logit + c[rc])))


def design(B, sel):
    """選んだ指数の段ダミー（疎行列）と、列の位置。"""
    mats, pos, k = [], {}, 0
    n = len(next(iter(B.values()))[0])
    for name in sel:
        b, labels = B[name]
        m = sparse.csr_matrix((np.ones(n, dtype=np.float32), (np.arange(n), b)), shape=(n, len(labels)))
        mats.append(m)
        pos[name] = (k, k + len(labels))
        k += len(labels)
    return sparse.hstack(mats).tocsr(), pos


def fit(X, y, off, l2=1.0):
    def f(beta):
        z = off + X @ beta
        p = 1 / (1 + np.exp(-z))
        loss = -np.sum(y * np.log(p + 1e-12) + (1 - y) * np.log(1 - p + 1e-12)) + l2 * beta @ beta
        return loss, X.T @ (p - y) + 2 * l2 * beta
    return minimize(f, np.zeros(X.shape[1]), jac=True, method='L-BFGS-B', options={'maxiter': 300}).x


def metrics(p, d, min_field=8):
    m = (d.field >= min_field).values
    q = pd.DataFrame({'p': p[m], 't': d.top3.values[m], 'r': d.rc.values[m], 'rn': d.race_num.values[m]})
    pc = np.clip(q.p, 1e-6, 1 - 1e-6)
    q['rk'] = q.groupby('r').p.rank(ascending=False, method='first')
    g = q.groupby('r')
    in5 = g.apply(lambda x: x.loc[x.rk <= 5, 't'].sum(), include_groups=False)
    in6 = g.apply(lambda x: x.loc[x.rk <= 6, 't'].sum(), include_groups=False)
    h1 = g.apply(lambda x: x.loc[x.rk == 1, 't'].sum(), include_groups=False)
    m11 = g.rn.first() == 11
    return {'logloss': round(float(-np.mean(q.t * np.log(pc) + (1 - q.t) * np.log(1 - pc))), 5),
            'no1_top3': round(float(h1.mean()), 4), 'in5': round(float(in5.mean()), 3),
            'all3_in5': round(float((in5 == 3).mean()), 4), 'all3_in6': round(float((in6 == 3).mean()), 4),
            'no1_top3_11R': round(float(h1[m11].mean()), 4), 'all3_in5_11R': round(float((in5[m11] == 3).mean()), 4),
            'races': int(len(h1))}


def solo(F, col, sign):
    d = F[(F.year >= 2024) & (F.field >= 8)]
    s = (d[col] * sign)
    s = s.fillna(s.groupby(d.rc).transform('min') - 1)          # データなしは最下位扱い
    return metrics_rank(s.values, d)


def metrics_rank(s, d):
    q = pd.DataFrame({'s': s, 't': d.top3.values, 'r': d.rc.values})
    q['rk'] = q.groupby('r').s.rank(ascending=False, method='first')
    g = q.groupby('r')
    in5 = g.apply(lambda x: x.loc[x.rk <= 5, 't'].sum(), include_groups=False)
    h1 = g.apply(lambda x: x.loc[x.rk == 1, 't'].sum(), include_groups=False)
    # レース内AUC：3着内の馬が、それ以外の馬より上の順位にいる割合
    def auc(x):
        pos, neg = x.rk[x.t == 1].values, x.rk[x.t == 0].values
        return np.nan if len(pos) == 0 or len(neg) == 0 else (pos[:, None] < neg[None, :]).mean()
    return {'no1_top3': round(float(h1.mean()), 4), 'all3_in5': round(float((in5 == 3).mean()), 4),
            'auc': round(float(g.apply(auc, include_groups=False).mean()), 4)}


if __name__ == '__main__':
    F = load()
    off = np.log(3 / (F.field - 3).clip(lower=1)).values
    signs = {c: (direction(F, c) if k == 'rank' else 1) for c, _, k in INDEX}
    names = {c: n for c, n, _ in INDEX}
    B = {c: buckets(F, c, k, signs[c]) for c, _, k in INDEX}

    print('=== 1. 指数ごとの単独の当てる力（2024–26、8頭立て以上）===')
    rows = []
    for c, n, k in INDEX:
        if k == 'rank':
            rows.append({'指数': n, '向き': '大きいほど良い' if signs[c] > 0 else '小さいほど良い', **solo(F, c, signs[c])})
    d = F[(F.year >= 2024) & (F.field >= 8)]
    rows.append({'指数': '（参考）人気順', '向き': '', **metrics_rank(-d.popularity.fillna(99).values, d)})
    S = pd.DataFrame(rows).sort_values('auc', ascending=False)
    print(S.to_string(index=False))
    S.to_csv(f'{OUT}/fermi_solo.csv', index=False)

    tr, va, te = (F.year <= 2021).values, F.year.between(2022, 2023).values, (F.year >= 2024).values
    y = F.top3.values

    def val_loss(sel):
        X, _ = design(B, sel)
        beta = fit(X[tr], y[tr], off[tr])
        p = normalize(off[va] + X[va] @ beta, pd.factorize(F.rc.values[va])[0], None)
        pc = np.clip(p, 1e-6, 1 - 1e-6)
        m = F.field.values[va] >= 8
        return float(-np.mean(y[va][m] * np.log(pc[m]) + (1 - y[va][m]) * np.log(1 - pc[m])))

    print('\n=== 2. 指数を1つずつ足す（2011–21で倍率、2022–23の対数損失で選ぶ）===')
    cands = [c for c, _, _ in INDEX if c != 'sub_margin']
    sel, hist = [], []
    base = val_loss([]) if False else None
    for step in range(MAXK):
        best = min(((val_loss(sel + [c]), c) for c in cands if c not in sel))
        sel.append(best[1])
        hist.append({'順': step + 1, '足した指数': names[best[1]], '対数損失(2022–23)': round(best[0], 5)})
        print(hist[-1], flush=True)
    json.dump({'selected': sel, 'history': hist}, open(f'{OUT}/fermi_selection.json', 'w'), ensure_ascii=False, indent=1)

    print('\n=== 3. 最終：2011–23で倍率を決め、2024–26で測る ===')
    trf = (F.year <= 2023).values
    dte = F[te]
    rc_te = pd.factorize(F.rc.values[te])[0]
    res = {}
    for k in (3, 5, 8, MAXK):
        s = sel[:k]
        X, pos = design(B, s)
        beta = fit(X[trf], y[trf], off[trf])
        p = normalize(off[te] + X[te] @ beta, rc_te, None)
        res[f'フェルミ式（上位{k}指数）'] = metrics(p, dte)
        if k == MAXK:
            np.save(f'{OUT}/fermi_p_test.npy', p)
            table = {names[c]: {'向き': ('大きいほど良い' if signs[c] > 0 else '小さいほど良い') if dict((a, t) for a, _, t in INDEX)[c] == 'rank' else '値',
                                '倍率': {lab: round(float(np.exp(b)), 2) for lab, b in zip(B[c][1], beta[pos[c][0]:pos[c][1]])}}
                     for c in s}
            json.dump(table, open(f'{OUT}/fermi_table.json', 'w'), ensure_ascii=False, indent=1)
    # 素朴版：指数ごとに独立に倍率を作り、掛け算（＋全体を1つの指数で弱める版）
    s = sel[:MAXK]
    ll = np.zeros(len(F))
    for c in s:
        b, labels = B[c]
        t3 = np.bincount(b[trf], y[trf], len(labels)) + 1
        nt = np.bincount(b[trf], 1 - y[trf], len(labels)) + 1
        ll += np.log((t3 / t3.sum()) / (nt / nt.sum()))[b]
    res['素朴な掛け算（独立に作った倍率）'] = metrics(normalize(off[te] + ll[te], rc_te, None), dte)
    a = fit(sparse.csr_matrix(ll[trf][:, None]), y[trf], off[trf], l2=0.0)[0]
    res[f'素朴な掛け算を {a:.2f} 乗で弱める'] = metrics(normalize(off[te] + a * ll[te], rc_te, None), dte)
    res['（参考）人気順'] = {**metrics_rank(-dte[dte.field >= 8].popularity.fillna(99).values, dte[dte.field >= 8])}
    for k, v in res.items():
        print(k, json.dumps(v, ensure_ascii=False))
    json.dump(res, open(f'{OUT}/fermi_results.json', 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(json.load(open(f'{OUT}/fermi_table.json')), ensure_ascii=False, indent=1))
