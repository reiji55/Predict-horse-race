"""今週のメインレースについて、フェルミ式13項目の入力をそろえ、3着内確率・堅さ指数・3連複5点を出す。

入力：アプリの raw/{週}.json（出走馬・過去5走）、検証用データベース（keiba_core、2026-10-04 まで）、
      アプリの data/training/training_eval.csv（調教評価）。オッズ・人気は使わない。
データベースより後の走（10/5以降）は raw の過去走から足す。その走の「上がり順位」は分からないので平均から外し、
賞金は同じクラス・着順の中央値で見積もる。当日の馬体重が未発表なら前走の馬体重で代用する（どちらも表に印を付ける）。
usage: python today.py RAW.json TRAINING.csv            （今週分）
       python today.py --check RACE_ID                    （データベース内のレースで、特徴量ファイルと一致するか確かめる）
"""
import json
import math
import re
import sys

import numpy as np
import pandas as pd

from trio_lib import trio_table

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
GUIDE = '/home/claude/keiba_lab/share/fermi_formula_guide.md'
ns = {}
exec(re.search(r"```python\n(.*?)```", open(GUIDE).read(), re.S).group(1), ns)
CLS = {'new': 0, 'shinba': 0, 'mi': 1, 'maiden': 1, '1win': 2, '2win': 3, '3win': 4, 'op': 5, 'l': 5, 'g3': 6, 'g2': 7, 'g1': 8}
JRA = {'札幌', '函館', '福島', '新潟', '東京', '中山', '中京', '京都', '阪神', '小倉'}
LAM = math.log(2) / 730

R = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'cls', 'horse_id', 'horse_number', 'jockey_id', 'trainer_id',
                                                  'finish', 'completed', 'margin_sec', 'last_3f', 'horse_weight',
                                                  'prize_money', 'field', 'age'])
R['race_id'] = R.race_id.astype(str)
R['date'] = pd.to_datetime(R.date)
R['l3_pct'] = R.groupby('race_id').last_3f.rank(pct=True, method='average')
R['fin_pct'] = np.where(R.completed.astype(bool) & R.finish.notna(), (R.finish - 1) / (R.field - 1).clip(lower=1), 1.0)
R['mg'] = np.where(R.finish == 1, 0.0, R.margin_sec.clip(0, 3))
R['t3'] = (R.completed.astype(bool) & (R.finish <= 3)).astype(float)
recent = R[(R.date >= '2025-01-01') & (R.finish <= 5)]
PRIZE = recent.groupby(['cls', 'finish']).prize_money.median().to_dict()


def person_rate(col, pid, asof):
    d = R[(R[col] == pid) & (R.date < asof)]
    if len(d) == 0:
        return None
    w = np.exp(-LAM * (asof - d.date).dt.days.values)
    return float((np.sum(w * d.t3.values) + 0.21 * 20) / (np.sum(w) + 20))


def horse_inputs(hid, asof, race_cls, extra_runs=()):
    h = R[(R.horse_id == hid) & (R.date < asof)].sort_values('date')[['date', 'cls', 'fin_pct', 'mg', 'l3_pct', 'prize_money', 'horse_weight']]
    rows = h.to_dict('records')
    est = 0
    for p in extra_runs:                      # データベースより後の走（raw から）
        fin, heads = p.get('finish'), p.get('heads')
        c = CLS.get(p.get('class'))
        if p.get('class') == 'mi':            # raw は新馬・未勝利が同じ 'mi'。その馬の最初の走なら新馬(0)、そうでなければ未勝利(1)
            first = len(h) == 0 and p['date'] == min(q['date'] for q in extra_runs)
            c = 0 if first else 1
        rows.append({'date': pd.Timestamp(p['date']), 'cls': c, 'fin_pct': (fin - 1) / max(heads - 1, 1) if fin and heads else 1.0,
                     'mg': 0.0 if fin == 1 else (min(max(p['margin_sec'], 0), 3) if p.get('margin_sec') is not None else np.nan),
                     'l3_pct': np.nan, 'prize_money': PRIZE.get((c, fin), 0.0) if fin and fin <= 5 else 0.0,
                     'horse_weight': (p.get('body_weight') or {}).get('value')})
        est += 1
    rows.sort(key=lambda x: x['date'])
    out = {'career': len(rows), 'extra_runs': est}
    if not rows:
        return out
    last5 = rows[-5:]
    nm = lambda v: float(np.nanmean(v)) if np.any(~np.isnan(np.array(v, float))) else None
    out.update({'fin5': nm([r['fin_pct'] for r in last5]), 'last': rows[-1]['fin_pct'],
                'margin5': nm([r['mg'] for r in last5]), 'last3f': nm([r['l3_pct'] for r in last5]),
                'class': nm([np.nan if r['cls'] is None else r['cls'] for r in last5]),
                'prize': float(np.nansum([r['prize_money'] for r in rows])) / len(rows),
                'rest': (asof - rows[-1]['date']).days, 'prev_weight': rows[-1]['horse_weight']})
    if out['class'] is not None:
        out['class'] -= race_cls
    return out


DIRS = {'fin5': 1, 'jockey': -1, 'prize': -1, 'last': 1, 'class': -1, 'trainer': -1, 'margin5': 1, 'weight': -1, 'last3f': 1}   # 1＝小さいほど上位


def race_table(entries, asof, race_cls, power=1.0):
    """entries: [{'number','name','horse_id','jockey_id','trainer_id','age','weight','train','extra_runs'}]"""
    rows = []
    for e in entries:
        x = horse_inputs(e['horse_id'], asof, race_cls, e.get('extra_runs', ()))
        x.update({'number': e['number'], 'name': e.get('name', ''), 'age': e['age'], 'train': e.get('train'),
                  'jockey': person_rate('jockey_id', e['jockey_id'], asof), 'trainer': person_rate('trainer_id', e['trainer_id'], asof),
                  'weight': e.get('weight') if e.get('weight') else x.get('prev_weight'), 'weight_is_prev': not e.get('weight')})
        rows.append(x)
    T = pd.DataFrame(rows)
    for k, s in DIRS.items():
        v = pd.to_numeric(T.get(k), errors='coerce')
        T[f'r_{k}'] = (v * s).rank(method='min')
    horses = [{'number': int(t.number), 'ranks': {k: (None if pd.isna(t[f'r_{k}']) else int(t[f'r_{k}'])) for k in DIRS},
               'career': int(t.career), 'rest': (None if pd.isna(t.get('rest')) else float(t.rest)), 'age': int(t.age), 'train': t.train}
              for _, t in T.iterrows()]
    p = ns['top3_probabilities'](horses, power)
    T['p'] = T.number.map(p)
    return T


def report(T, title):
    T = T.sort_values('p', ascending=False).reset_index(drop=True)
    print(f'\n=== {title} ===')
    show = T[['number', 'name', 'p'] + [f'r_{k}' for k in DIRS] + ['career', 'rest', 'age', 'train', 'weight_is_prev', 'extra_runs']].copy()
    show['p'] = (show.p * 100).round(1)
    print(show.to_string(index=False))
    S5 = T.p.head(5).sum()
    order = T.sort_values('number')
    tab = trio_table(order.p.values, 'exact')
    nums = order.number.values
    print(f'堅さ指数（上位5頭の確率の合計）= {S5:.2f}')
    tot = 0
    for pr, t in tab[:5]:
        tot += pr
        print(f"  3連複 {'-'.join(str(int(nums[i])) for i in t)}  確率 {pr:.1%}")
    print(f'  5点の的中確率の合計 {tot:.1%}')
    return {'title': title, 'S5': round(float(S5), 2), 'top5': [(int(a), round(float(b), 3)) for a, b in zip(T.number.head(5), T.p.head(5))],
            'trios': [['-'.join(str(int(nums[i])) for i in t), round(float(pr), 4)] for pr, t in tab[:5]], 'hit5': round(float(tot), 3)}


if __name__ == '__main__':
    if sys.argv[1] == '--check':
        rid = sys.argv[2]
        g = R[R.race_id == rid]
        asof = g.date.iloc[0]
        meta = pd.read_parquet(f'{D}/race_meta.parquet', columns=['race_id', 'cls'])
        cls = float(meta[meta.race_id.astype(str) == rid].cls.iloc[0])
        tr = pd.read_csv(f'{D}/training_eval.csv', dtype=str)
        tr = tr[tr.race_id == rid].set_index('horse_ref').grade.to_dict()
        ent = [{'number': int(x.horse_number), 'horse_id': x.horse_id, 'jockey_id': x.jockey_id, 'trainer_id': x.trainer_id,
                'age': int(x.age), 'weight': x.horse_weight, 'train': tr.get(str(x.horse_id))} for x in g.itertuples()]
        T = race_table(ent, asof, cls)
        import fermi as fm
        F = fm.load(extra=fm.EXTRA_11R[:1])
        f = F[F.race_id == rid].set_index('horse_number')
        cmp = {'fin5': 'fin_pct_mean5', 'jockey': 'jky_top3', 'prize': 'prize_per_run', 'last': 'last_finish_pct', 'class': 'cls_drop5',
               'trainer': 'trn_top3', 'margin5': 'margin_mean5', 'last3f': 'l3_pct_mean5', 'career': 'n_runs', 'rest': 'days_off'}
        T = T.set_index('number')
        for a, b in cmp.items():
            d = (pd.to_numeric(T[a], errors='coerce') - f[b]).abs()
            print(f'{a:8s} 最大の差 {d.max():.4f}  順位の一致 {(pd.to_numeric(T[a], errors="coerce").rank() == f[b].rank()).mean():.2f}')
        ref = pd.read_parquet(f'{OUT}/fermi_hand_test.parquet')
        ref = ref[ref.race_id == rid].set_index('horse_number').p
        print('確率の最大の差', float((T.p - ref).abs().max()))
    else:
        raw = json.load(open(sys.argv[1]))
        tr = pd.read_csv(sys.argv[2], dtype=str)
        asof = None
        res = []
        for race in raw['races']:
            rid = race['source_refs']['netkeiba']
            asof = pd.Timestamp(race['date'])
            grades = tr[tr.race_id == rid].set_index('horse_ref').grade.to_dict()
            ent = []
            for e in race['entries']:
                hid = e['horse_ref']['netkeiba']
                extra = [p for p in e['past_runs'] if p['date'] > '2026-10-04' and p.get('venue') in JRA]
                bw = e.get('body_weight')
                ent.append({'number': e['num'], 'name': e['name'], 'horse_id': hid, 'jockey_id': e['jockey']['ref']['netkeiba'],
                            'trainer_id': e['trainer']['ref']['netkeiba'], 'age': e['age'],
                            'weight': (bw or {}).get('value') if isinstance(bw, dict) else bw, 'train': grades.get(hid), 'extra_runs': extra})
                # 照合：raw の過去走（10/4以前・JRA）がデータベースにあるか
                dbdates = set(R[R.horse_id == hid].date.dt.strftime('%Y-%m-%d'))
                miss = [p['date'] for p in e['past_runs'] if p['date'] <= '2026-10-04' and p.get('venue') in JRA and p['date'] not in dbdates]
                if miss:
                    print('注意：データベースに無い過去走', e['name'], miss)
            T = race_table(ent, asof, CLS[race['grade']], power=0.79)      # メインレースの補正
            res.append(report(T, f"{race['venue']}11R {race['name']}（{race['course']['surface']}{race['course']['dist']}m・{len(ent)}頭）"))
            T.to_csv(f"{OUT}/today_{rid}.csv", index=False)
        json.dump(res, open(f'{OUT}/today.json', 'w'), ensure_ascii=False, indent=1)
