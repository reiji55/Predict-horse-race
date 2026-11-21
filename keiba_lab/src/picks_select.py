"""まだ走っていないレースについて、本命の式（前日版の5本）の平均 → 各馬の3着内確率 → 3連複の同時確率 → 確率の高い順5点。
5点の確率の合計が大きい順にレースを並べる。買う条件は PICK_RULE（既定 th0.50＝5点の合計が50%以上のレースだけ。
top3＝開催日ごとの上位3レース）。残りは「見送り」。
8頭立て以上だけ（検証した範囲）。out/picks_{日付}.json と share/picks_{日付}.md を書く。"""
import json

import numpy as np
import pandas as pd

import fermi as fm
from trio_lib import trio_table

D, OUT, SHARE = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out', '/home/claude/keiba_lab/share'
import os
TAGS = os.environ.get('PICK_TAGS', 'p2,p2_s2,p1,p1_s2,pcat').split(',')
SUF = os.environ.get('PICK_SUFFIX', '')
RULE = os.environ.get('PICK_RULE', 'th0.50')
TH = float(RULE[2:]) if RULE.startswith('th') else None
TOPN = int(RULE[3:]) if RULE.startswith('top') else 99
if not all(os.path.exists(f'{OUT}/pick_{t}.parquet') for t in TAGS):
    print('まだ走っていないレースがありません（買い目は出しません）')
    raise SystemExit(0)
fr = [pd.read_parquet(f'{OUT}/pick_{t}.parquet').assign(race_id=lambda d: d.race_id.astype(str)).set_index(['race_id', 'horse_number']).sort_index() for t in TAGS]
assert all(f.index.equals(fr[0].index) for f in fr)
b = fr[0].reset_index()[['race_id', 'horse_number']].copy()
b['s'] = np.mean([f.s.values for f in fr], axis=0)
b['p'] = fm.normalize(b.s.values, pd.factorize(b.race_id)[0], None)
races = pd.read_parquet(f'{D}/races.parquet', columns=['race_id', 'date', 'venue_name', 'race_num', 'race_name', 'surface', 'distance', 'start_time'])
res = pd.read_parquet(f'{D}/results.parquet', columns=['race_id', 'horse_number', 'horse_name'])
b = b.merge(res, on=['race_id', 'horse_number'], how='left')
rows = []
for rid, g in b.groupby('race_id', sort=False):
    g = g.sort_values('horse_number')
    m = races[races.race_id == rid].iloc[0]
    rec = {'race_id': rid, 'date': str(m.date)[:10], 'venue': m.venue_name, 'race_no': int(m.race_num), 'name': m.race_name,
           'course': f"{'ダ' if m.surface == 'ダート' else m.surface}{int(m.distance)}m", 'post': m.start_time, 'n': len(g)}
    top = g.sort_values('p', ascending=False)
    rec['horses'] = [[int(a), c, round(float(p), 3)] for a, c, p in zip(top.horse_number, top.horse_name, top.p)]
    rec['S5'] = round(float(top.p.head(5).sum()), 2)
    if len(g) >= 8:
        tab = trio_table(g.p.values, 'exact')
        nums = g.horse_number.values
        rec['trios'] = [['-'.join(str(int(nums[i])) for i in t), round(float(pr), 4)] for pr, t in tab[:5]]
        rec['hit5'] = round(float(sum(pr for pr, _ in tab[:5])), 3)
    rows.append(rec)
for date in sorted({r['date'] for r in rows}):
    day = sorted([r for r in rows if r['date'] == date], key=lambda r: -r.get('hit5', -1))
    for i, r in enumerate(day):
        r['pick'] = bool('hit5' in r and (r['hit5'] >= TH if TH is not None else i < TOPN))
    buy = [r for r in day if r['pick']]
    rule_txt = f"5点の確率の合計が{TH:.0%}以上のレースだけ買う" if TH is not None else f"開催日ごとに上位{TOPN}レースを買う"
    json.dump(day, open(f'{OUT}/picks_{date}{SUF}.json', 'w'), ensure_ascii=False, indent=1)
    L = [f'# {date} 買うレース {len(buy)}つ（全{len(day)}レースから）', '',
         f'買う条件：{rule_txt}。オッズ・人気は使っていません。本命の式（{len(TAGS)}本の平均）で全レースの3着内確率を出し、3連複5点の確率の合計が大きい順に並べました。',
         '当日の馬体重が未発表のときは直近の馬体重、馬場状態が未発表のときは前日の同じ競馬場の最終状態で仮置きしています。障害レースと7頭以下のレースは対象外です。', '']
    if not buy:
        L += ['**この日は条件を満たすレースがありません（全レース見送り）。**', '']
    for r in buy:
        L += [f"## {r['venue']}{r['race_no']}R {r['name']}（{r['course']}・{r['n']}頭・{r['post']}発走）", '',
              f"5点の確率の合計 **{r['hit5']:.1%}**　／　堅さ指数 {r['S5']:.2f}", '', '| 3連複 | 確率 |', '|---|---|']
        L += [f'| {t} | {p:.1%} |' for t, p in r['trios']]
        L += ['', '| 馬番 | 馬名 | 3着内確率 |', '|---|---|---|'] + [f'| {a} | {c} | {p:.1%} |' for a, c, p in r['horses'][:6]] + ['']
        out5 = [f'{a} {c}（{p:.0%}）' for a, c, p in r['horses'] if p < 0.05]
        L += ['消せる馬（3着内確率5%未満。検証では実際に来たのは2%）：' + ('、'.join(out5) if out5 else 'なし'), '']
    L += ['## 全レースの一覧（5点の確率の合計の大きい順）', '', '| | レース | 頭数 | 発走 | 5点の確率の合計 | 3連複5点 |', '|---|---|---|---|---|---|']
    for r in day:
        if 'hit5' in r:
            L.append(f"| {'買う' if r['pick'] else '見送り'} | {r['venue']}{r['race_no']}R {r['name']} | {r['n']} | {r['post']} | {r['hit5']:.1%} | {' / '.join(t for t, _ in r['trios'])} |")
        else:
            L.append(f"| 対象外 | {r['venue']}{r['race_no']}R {r['name']} | {r['n']} | {r['post']} | － | 7頭以下 |")
    open(f'{SHARE}/picks_{date}{SUF}.md', 'w').write('\n'.join(L) + '\n')
    print(date, RULE, [(r['venue'], r['race_no'], r['hit5']) for r in buy])
