"""3連複5点の検証：各馬の3着内確率 → 同時確率 → 確率の高い5組。2024–26、8頭立て以上。
比べるもの：同時確率の作り方（exact / odds / prob）、式（手のフェルミ式13項目・6項目・本命の式）、
参考として「上位5頭ボックス（10点）」と人気順から作った5組。確率の合い方（5組の見立ての合計 と 実際の的中率）も見る。
usage: python trio_eval.py [N_RACES]"""
import sys

import numpy as np
import pandas as pd

import fermi as fm
from trio_lib import trio_table

OUT = fm.OUT
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else None


def load_models():
    M = {}
    h = pd.read_parquet(f'{OUT}/fermi_hand_test.parquet')
    M['手のフェルミ式（13項目）'] = h[h.field >= 8][['race_id', 'horse_number', 'finish', 'race_num', 'popularity', 'p']]
    try:
        s = pd.read_parquet(f'{OUT}/fermi_fixed_six.parquet')
        M['6項目版'] = s[s.field >= 8][['race_id', 'horse_number', 'finish', 'race_num', 'popularity', 'p']]
    except FileNotFoundError:
        pass
    tags = ['chain2', 'chain2_s2', 'chain3', 'chain3_s2', 'chain4', 'chain4_s2', 'cat1']
    fr = [pd.read_parquet(f'{OUT}/exp_{t}.parquet').set_index(['race_id', 'horse_number']).sort_index() for t in tags]
    b = fr[0].reset_index()[['race_id', 'horse_number', 'finish', 'race_num']].copy()
    b['race_id'] = b.race_id.astype(str)
    b['p'] = fm.normalize(np.mean([f.s.values for f in fr], axis=0), pd.factorize(b.race_id)[0], None)
    b = b.merge(h[['race_id', 'horse_number', 'popularity']], on=['race_id', 'horse_number'], how='left')
    M['本命の式'] = b
    return M


def evaluate(df, method, k=5, use='p'):
    rows = []
    for rid, g in df.groupby('race_id', sort=False):
        g = g.sort_values('horse_number')
        fin = g.finish.values
        win = frozenset(np.flatnonzero(fin <= 3))
        if len(win) != 3:
            continue
        p = g[use].values
        tab = trio_table(p, method)
        top = tab[:k]
        hit = any(frozenset(t) == win for _, t in top)
        pw = next(pr for pr, t in tab if frozenset(t) == win)
        rk = next(i for i, (_, t) in enumerate(tab) if frozenset(t) == win) + 1
        rows.append((rid, hit, sum(pr for pr, _ in top), pw, rk, g.race_num.iloc[0], len(g)))
    return pd.DataFrame(rows, columns=['race_id', 'hit', 'pred', 'p_win', 'rank', 'rn', 'n'])


if __name__ == '__main__':
    M = load_models()
    ids = None
    for name, df in M.items():
        if LIMIT:
            ids = ids if ids is not None else pd.Series(df.race_id.unique()).sample(LIMIT, random_state=0).values
            df = df[df.race_id.isin(ids)]
        for method in (('exact', 'odds', 'prob') if '13' in name else ('exact',)):
            R = evaluate(df, method)
            m = R[R.rn == 11]
            R['bin'] = pd.cut(R.pred, [0, .1, .2, .3, .4, .6, 1])
            cal = R.groupby('bin', observed=True).agg(n=('hit', 'size'), 見立て=('pred', 'mean'), 実際=('hit', 'mean')).round(3)
            print(f"{name} [{method}] {len(R)}レース：5組の的中 {R.hit.mean():.1%}（見立ての平均 {R.pred.mean():.1%}）、"
                  f"当たりの組の順位の中央値 {R['rank'].median():.0f}、−log P {(-np.log(R.p_win)).mean():.4f} | "
                  f"11R {len(m)}レース：的中 {m.hit.mean():.1%}（見立て {m.pred.mean():.1%}）", flush=True)
            if method == 'exact':
                print(cal.to_string(), flush=True)
                R.drop(columns='bin').to_parquet(f"{OUT}/trio_eval_{'hand' if '13' in name else 'six' if '6' in name else 'main'}.parquet")
        if '13' in name:
            # 参考：人気順の確率（順位の逆数に比例）で作った5組、上位5頭ボックス10点
            d2 = df.copy()
            d2['pp'] = 1 / d2.popularity.fillna(18)
            d2['pp'] = d2.pp / d2.groupby('race_id').pp.transform('sum') * 3
            R = evaluate(d2.assign(pp=d2.pp.clip(upper=0.95)), 'odds', use='pp')
            print(f"（参考）人気順から作った5組：的中 {R.hit.mean():.1%} | 11R {R[R.rn == 11].hit.mean():.1%}", flush=True)
