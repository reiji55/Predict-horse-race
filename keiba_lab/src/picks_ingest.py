"""取ったレース（アプリ側 data/picks の JSON）を、研究用データベース（keiba_core の形）に足す。

入力：PICKS_DIR（既定 /home/claude/picks_data/data/picks）の
      days/{日付}.json   … picks/fetch_day.py が書く。レースごとに status が result（結果）か entry（出馬表）
      verify/{日付}.json … picks/fetch_verify.py が書く。各レースの result を使う
出力：data/races.parquet, data/results.parquet を「元のデータ（*_base.parquet）＋取ったレース」で作り直す。
      元のデータは初回に data/races_base.parquet, data/results_base.parquet として残し、以後はそこから作る。
      out/pick_ids.txt … まだ走っていないレース（entry）の race_id。exp_harness / exp_catboost がこの見立てを書く。

結果のレース：着順・タイム・着差・上がり・通過順・馬体重・単勝オッズ・人気・ラップ・コーナー通過順。
  通過順の欄が空のときは、コーナー通過順の表から各馬の位置を数える。
  賞金は本賞金（1〜5着）。同着は該当の着順の賞金を平均する。馬主は同じ馬の過去の記録から引き継ぐ。
まだ走っていないレース：着順は空。単勝オッズは使わないので仮の 10.0、人気は馬番順の仮の値
  （build_base が単勝オッズの無い馬を落とすため。式はオッズ・人気を使わない）。
  馬体重が未発表なら、その馬の直近の馬体重（増減 0）で仮置き。初出走の馬は空。
  馬場状態は 出馬表の値 → 出馬表の「予報」→ その日の同じ競馬場・芝ダで最後に分かっている状態 の順で入れる。
2026-11-21 に作り直し（元の版は環境ごと消えた）。
"""
import glob
import json
import os
import re
import unicodedata

import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
OUT = '/home/claude/keiba_lab/out'
PICKS = os.environ.get('PICKS_DIR', '/home/claude/picks_data/data/picks')
GOING = {'良': '良', '稍': '稍重', '稍重': '稍重', '重': '重', '不': '不良', '不良': '不良'}
GRADE = {'g1': '(GI)', 'g2': '(GII)', 'g3': '(GIII)', 'jg1': '(GI)', 'jg2': '(GII)', 'jg3': '(GIII)', 'l': '(L)'}
VENUE_CODE = {'札幌': '01', '函館': '02', '福島': '03', '新潟': '04', '東京': '05', '中山': '06', '中京': '07', '京都': '08',
              '阪神': '09', '小倉': '10'}


def nk(s):
    return unicodedata.normalize('NFKC', s or '')


def tsec(s):
    m = re.match(r'^(\d+):(\d+(?:\.\d+)?)$', (s or '').strip())
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    try:
        return float(s)
    except (TypeError, ValueError):
        return np.nan


def fnum(s):
    try:
        return float(str(s).replace(',', '').strip())
    except (TypeError, ValueError):
        return np.nan


def corner_order(text):
    """コーナー通過順の文字列 → {馬番: 位置}。( ) の中は同じ位置、続く馬はその頭数ぶん後ろ。"""
    s = re.sub(r'\s+', '', nk(text)).replace('*', '')
    pos, k, out = 1, 0, {}
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '(':
            j = s.index(')', i)
            grp = [int(x) for x in re.findall(r'\d+', s[i + 1:j])]
            for h in grp:
                out[h] = pos
            pos += len(grp)
            i = j + 1
        elif ch.isdigit():
            m = re.match(r'\d+', s[i:])
            out[int(m.group())] = pos
            pos += 1
            i += len(m.group())
        else:
            i += 1
    return out


def race_row(rid, r, date):
    d1, d2 = nk(r.get('race_data01')), nk(r.get('race_data02'))
    meta = r.get('meta') or {}
    surface = '芝' if re.search(r'芝\d', d1) else ('ダート' if re.search(r'ダ\d', d1) else ('障害' if '障' in d1 else None))
    if surface is None:
        s = (meta.get('course') or {}).get('surface')
        surface = {'芝': '芝', 'ダ': 'ダート', '障': '障害'}.get(s)
    dist = re.search(r'(?:芝|ダ|障)\D*?(\d{3,4})m', d1)
    dist = int(dist.group(1)) if dist else int((meta.get('course') or {}).get('dist') or 0)
    dm = re.search(r'\d{3,4}m\s*\(?\s*(右|左|直線)', d1)
    direction = dm.group(1) if dm else ''
    if direction == '直線':
        direction, ctype = '', '直線'
    else:
        cm = re.search(r'\d{3,4}m\s*\(?\s*(?:右|左)\s*(外-内|内-外|内2周|外)', d1)
        ctype = cm.group(1) if cm else ''
    toks = [t.strip() for t in re.split(r'/', d1)]
    _cl = lambda t: re.sub(r'\s+', '', re.sub(r'^(馬場|芝|ダート|ダ):', '', t).replace('予報', ''))
    cond = next((GOING[_cl(t)] for t in toks if _cl(t) in GOING), None)
    if cond is None and meta.get('going'):
        cond = GOING.get(nk(meta['going']).replace('予報', '').strip())
    wm = re.search(r'天候:(\S+)', d1)
    weather = wm.group(1) if wm else (meta.get('weather') or None)
    st = re.match(r'\s*(\d{1,2}:\d{2})', d1)
    start = st.group(1) if st else (meta.get('post_time') or '')
    # class_info を keiba_core と同じ形に（build_base が クラス・ハンデ・牝馬限定・年齢条件 を読む）
    kai = re.search(r'(\d+)回', d2)
    nichi = re.search(r'(\d+)日目', d2)
    age_cls = ''
    am = re.search(r'サラ系\s*(\S+?)\s+(\S+)', d2)
    if am:
        age_cls = am.group(1) + am.group(2)
        if '障害' in d2 and '障害' not in age_cls:
            age_cls = '障害' + age_cls
    conds = re.findall(r'\(国際\)|\(混\)|\(特指\)|\[指\]|\(指\)|牝\(指\)|牝\[指\]|\(牝\)', d2)
    wt = re.search(r'(定量|ハンデ|別定|馬齢)', d2)
    y, mo, dd = (int(x) for x in date.split('-'))
    venue = meta.get('venue') or ''
    ci = (f"{y}年{mo}月{dd}日 {kai.group(1) if kai else ''}回{venue}{nichi.group(1) if nichi else ''}日目 {age_cls}"
          f"\xa0\xa0{''.join(conds)}{'(' + wt.group(1) + ')' if wt else ''}")
    pm = re.search(r'本賞金:([\d.,]+)', d2)
    prizes = [fnum(x) for x in pm.group(1).split(',')] if pm else []
    name = meta.get('name') or ''
    suf = GRADE.get((meta.get('grade') or '').lower(), '')
    if suf and not name.endswith(suf):
        name += suf
    laps, c = None, {}
    for k, v in (r.get('tables') or {}).items():
        if 'HaronTime' in k and v:
            laps = [fnum(x) for x in v[-1]]
            laps = [x for x in laps if not np.isnan(x)]
        if 'Corner' in k:
            for row in v:
                m = re.match(r'(\d)\s*コーナー', nk(row[0]))
                if m and len(row) > 1:
                    c[int(m.group(1))] = re.sub(r'\s+', '', nk(row[1]))
    n = len(r.get('rows') or r.get('entries') or [])
    return {'race_id': rid, 'venue_code': rid[4:6], 'kaisai': rid[6:8], 'day': rid[8:10], 'race_num': rid[10:12],
            'venue_name': venue, 'race_name': name, 'surface': surface, 'direction': direction, 'course_type': ctype,
            'distance': dist, 'weather': weather, 'track_condition': cond, 'start_time': start, 'date': date,
            'year': y, 'month': mo, 'class_info': ci, 'first_prize': prizes[0] if prizes else np.nan,
            'race_level': None, 'level_score': 0, 'horse_count': n,
            'lap_times': np.array(laps) if laps else None,
            'pace_first3f': round(sum(laps[:3]), 1) if laps and len(laps) >= 3 else np.nan,
            'pace_last3f': round(sum(laps[-3:]), 1) if laps and len(laps) >= 3 else np.nan,
            'corner1': c.get(1), 'corner2': c.get(2), 'corner3': c.get(3), 'corner4': c.get(4)}, prizes


def result_rows(rid, r, prizes, race):
    corners = [corner_order(race[f'corner{i}']) for i in (1, 2, 3, 4) if race.get(f'corner{i}')]
    out = []
    for row in r['rows']:
        c = [nk(x) for x in row['cells']]
        fin_txt = c[0]
        fin = float(fin_txt) if fin_txt.isdigit() else np.nan
        dnf = None if fin_txt.isdigit() else (fin_txt or None)
        sa = re.match(r'(牡|牝|セ)(\d+)', c[4])
        num = int(c[2])
        po = c[12].replace(' ', '') if len(c) > 12 else ''
        if not po and corners:
            ps = [cr.get(num) for cr in corners]
            po = '-'.join(str(p) for p in ps if p) if any(ps) else ''
        pos = [int(x) for x in po.split('-') if x.isdigit()]
        tr = c[13].split() if len(c) > 13 else []
        region = {'美浦': '東', '栗東': '西'}.get(tr[0]) if tr else None
        tname = ' '.join(tr[1:]) if len(tr) > 1 else (tr[0] if tr else None)
        bw = re.match(r'(\d+)\s*\(([+\-]?\d+)\)', c[14]) if len(c) > 14 else None
        out.append({'race_id': rid, 'finish_position': fin, 'is_finished': not np.isnan(fin), 'dnf_reason': dnf,
                    'gate_number': int(c[1]) if c[1].isdigit() else 0, 'horse_number': num, 'horse_name': c[3],
                    'horse_id': row.get('horse_id'), 'sex': sa.group(1) if sa else None,
                    'age': int(sa.group(2)) if sa else 0, 'impost': fnum(c[5]), 'jockey_name': c[6],
                    'jockey_id': row.get('jockey_id'), 'time_str': c[7], 'time_seconds': tsec(c[7]), 'margin': c[8],
                    'passing_order': po, 'first_corner': float(pos[0]) if pos else np.nan,
                    'last_corner': float(pos[-1]) if pos else np.nan, 'last_3f': fnum(c[11]),
                    'popularity': int(c[9]) if c[9].isdigit() else 0, 'odds': fnum(c[10]),
                    'horse_weight': float(bw.group(1)) if bw else np.nan, 'weight_change': float(bw.group(2)) if bw else np.nan,
                    'trainer_name': tname, 'trainer_id': row.get('trainer_id'), 'trainer_region': region,
                    'owner_name': None, 'owner_id': None, 'prize_money': 0.0})
    # 本賞金：同着は該当着順の賞金を平均
    df = pd.DataFrame(out)
    if prizes and len(df):
        for f, g in df[df.finish_position.notna()].groupby('finish_position'):
            k = int(f)
            share = [prizes[i - 1] if i - 1 < len(prizes) else 0.0 for i in range(k, k + len(g))]
            df.loc[g.index, 'prize_money'] = float(np.mean(share))
    return df


def entry_rows(rid, r):
    out = []
    for i, e in enumerate([e for e in r['entries'] if e.get('num') is not None]):
        bw = e.get('body_weight') or {}
        tr = (e.get('trainer') or {})
        tname = nk(tr.get('name') or '')
        out.append({'race_id': rid, 'finish_position': np.nan, 'is_finished': False, 'dnf_reason': None,
                    'gate_number': int(e['waku']) if e.get('waku') else 0, 'horse_number': int(e['num']),
                    'horse_name': e.get('name'), 'horse_id': (e.get('horse_ref') or {}).get('netkeiba'),
                    'sex': e.get('sex'), 'age': int(e.get('age') or 0), 'impost': e.get('impost'),
                    'jockey_name': (e.get('jockey') or {}).get('name'),
                    'jockey_id': ((e.get('jockey') or {}).get('ref') or {}).get('netkeiba'),
                    'time_str': '', 'time_seconds': np.nan, 'margin': '', 'passing_order': '', 'first_corner': np.nan,
                    'last_corner': np.nan, 'last_3f': np.nan, 'popularity': i + 1, 'odds': 10.0,
                    'horse_weight': float(bw['value']) if bw.get('value') else np.nan,
                    'weight_change': float(bw['diff']) if bw.get('value') else np.nan,
                    'trainer_name': tname or None, 'trainer_id': (tr.get('ref') or {}).get('netkeiba'),
                    'trainer_region': None, 'owner_name': None, 'owner_id': None, 'prize_money': 0.0})
    return pd.DataFrame(out)


# ---- 元のデータ ----
if not os.path.exists(f'{D}/races_base.parquet'):
    pd.read_parquet(f'{D}/races.parquet').to_parquet(f'{D}/races_base.parquet')
    pd.read_parquet(f'{D}/results.parquet').to_parquet(f'{D}/results_base.parquet')
rb = pd.read_parquet(f'{D}/races_base.parquet')
sb = pd.read_parquet(f'{D}/results_base.parquet')

# ---- 取ったレース ----
recs = {}
for f in sorted(glob.glob(f'{PICKS}/days/*.json')):
    day = json.load(open(f))
    for rid, r in day['races'].items():
        recs[rid] = (day['date'], r)
for f in sorted(glob.glob(f'{PICKS}/verify/*.json')):
    if f.endswith('_payout.json'):
        continue
    day = json.load(open(f))
    for rid, x in day['races'].items():
        r = x.get('result')
        if r and r.get('status') == 'result' and rid not in recs:
            recs[rid] = (day['date'], r)
races, results, picks = [], [], []
for rid, (date, r) in sorted(recs.items()):
    if rid in set(rb.race_id):
        continue
    if r.get('status') == 'result' and r.get('rows'):
        race, prizes = race_row(rid, r, date)
        races.append(race)
        results.append(result_rows(rid, r, prizes, race))
    elif r.get('status') == 'entry' and r.get('entries'):
        race, _ = race_row(rid, r, date)
        races.append(race)
        results.append(entry_rows(rid, r))
        picks.append(rid)
nr = pd.DataFrame(races)
ns = pd.concat(results, ignore_index=True) if results else sb.iloc[:0].copy()

# 調教師の東西：結果ページの「美浦／栗東」、無ければ過去の記録から
reg = pd.concat([sb[['trainer_id', 'trainer_region']], ns[['trainer_id', 'trainer_region']]]).dropna().drop_duplicates('trainer_id', keep='last')
ns['trainer_region'] = ns.trainer_region.fillna(ns.trainer_id.map(reg.set_index('trainer_id').trainer_region))
# 馬主：同じ馬の過去の記録から引き継ぐ
allr = pd.concat([sb, ns], ignore_index=True)
own = allr.dropna(subset=['owner_id']).drop_duplicates('horse_id', keep='last').set_index('horse_id')
ns['owner_id'] = ns.owner_id.fillna(ns.horse_id.map(own.owner_id))
ns['owner_name'] = ns.owner_name.fillna(ns.horse_id.map(own.owner_name))

# まだ走っていないレース：馬体重と馬場状態の仮置き
if picks:
    nr['date_'] = pd.to_datetime(nr.date)
    known = pd.concat([sb.merge(rb[['race_id', 'date']], on='race_id'), ns.merge(nr[['race_id', 'date']], on='race_id')])
    known = known[known.horse_weight.notna()].copy()
    known['date'] = pd.to_datetime(known.date)
    last_w = known.sort_values('date').drop_duplicates('horse_id', keep='last').set_index('horse_id').horse_weight
    m = ns.race_id.isin(picks) & ns.horse_weight.isna()
    ns.loc[m, 'horse_weight'] = ns.loc[m, 'horse_id'].map(last_w)
    ns.loc[m & ns.horse_weight.notna(), 'weight_change'] = 0.0
    print('馬体重を仮置き', int(m.sum()), '頭（過去の記録なし', int((m & ns.horse_weight.isna()).sum()), '頭）')
    done = nr[nr.track_condition.notna() & ~nr.race_id.isin(picks)].sort_values(['date_', 'start_time'])
    lastc = done.groupby(['venue_name', 'surface']).track_condition.last()
    for i in nr.index[nr.race_id.isin(picks) & nr.track_condition.isna()]:
        nr.loc[i, 'track_condition'] = lastc.get((nr.loc[i, 'venue_name'], nr.loc[i, 'surface']), '良')
    nr = nr.drop(columns='date_')

races_all = pd.concat([rb, nr[rb.columns]], ignore_index=True)
for c in ('distance', 'year', 'month', 'level_score', 'horse_count'):
    races_all[c] = races_all[c].astype('int64')
res_all = pd.concat([sb, ns[sb.columns]], ignore_index=True)
for c in ('gate_number', 'horse_number', 'age', 'popularity'):
    res_all[c] = res_all[c].astype('int64')
res_all['is_finished'] = res_all.is_finished.astype(bool)
races_all.to_parquet(f'{D}/races.parquet')
res_all.to_parquet(f'{D}/results.parquet')
os.makedirs(OUT, exist_ok=True)
open(f'{OUT}/pick_ids.txt', 'w').write('\n'.join(picks) + ('\n' if picks else ''))
print('races', len(rb), '->', len(races_all), '| results', len(sb), '->', len(res_all), '| 取ったレース', len(nr),
      '（結果', len(nr) - len(picks), '／出馬表', len(picks), '）')
print('馬場状態', nr.track_condition.value_counts(dropna=False).to_dict(), '| 欠け：通過順',
      int((ns.passing_order == '').sum()), '上がり', int(ns.last_3f.isna().sum()), 'ラップ', int(nr.lap_times.isna().sum()))
