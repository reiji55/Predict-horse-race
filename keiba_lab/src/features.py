"""Point-in-time feature engine.  One chronological pass over every race 2010-2026.

Everything a horse gets as a feature is computed from races that finished BEFORE this race
(earlier dates, or earlier races on the same day for same-day track-bias features).

Core idea ("the converging variable"): every horse carries an ability rating per surface that is
updated after each race from its beaten margin, like a Kalman filter:
    y_i  = margin performance (s per 1000m, + for the winner's lead, - for beaten horses, capped)
    b    = race offset = precision-weighted mean of (y_i - r_i)
    r_i <- r_i + K_i (y_i - r_i - b),   K_i = v_i / (v_i + s2),   v_i <- (1-K_i) v_i
    v_i grows with time off (ability drift).
The residual e_i = y_i - r_i - b (how much better/worse than expected) is stored and reused for
distance / course / going aptitude.
"""
import math
import sys
from collections import defaultdict, deque

import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'

# rating hyper-parameters (can be overridden from the command line for tuning)
P = dict(S2=0.12, V0=0.25, Q=0.004, CAP=2.0, WCAP=0.6, VMIN=0.01)
for a in sys.argv[1:]:
    k, v = a.split('=')
    P[k] = float(v)
OUTFILE = f'{D}/features.parquet' if len(sys.argv) == 1 else f'{D}/features_tune.parquet'

runs = pd.read_parquet(f'{D}/runs.parquet')
meta = pd.read_parquet(f'{D}/race_meta.parquet')
runs = runs.merge(meta[['race_id', 'venue_name', 'handicap', 'female_only', 'listed', 'month', 'pace_first3f',
                        'pace_last3f']], on='race_id', how='left')
runs = runs.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
runs['date_ord'] = runs.date.map(pd.Timestamp.toordinal)

# ---------------- state ----------------
H_r = defaultdict(dict)          # horse -> {surf: rating}
H_v = defaultdict(dict)          # horse -> {surf: variance}
H_rd = defaultdict(dict)         # horse -> {surf: date of last rated run}
H_hist = defaultdict(lambda: deque(maxlen=12))   # horse -> past run records (dicts)
H_n = defaultdict(int)
H_wins = defaultdict(int)
H_top3 = defaultdict(int)
H_prize = defaultdict(float)
H_maxcls = defaultdict(lambda: -1.0)


class Decayed:
    """Exponentially decayed sums keyed by entity (half-life in days)."""

    def __init__(self, half_life):
        self.lam = math.log(2) / half_life
        self.s = defaultdict(lambda: np.zeros(4))   # n, wins, sum_mkt_p, top3
        self.t = {}

    def get(self, k, now):
        if k not in self.t:
            return np.zeros(4)
        return self.s[k] * math.exp(-self.lam * (now - self.t[k]))

    def add(self, k, now, vec):
        cur = self.get(k, now)
        self.s[k] = cur + vec
        self.t[k] = now


JOCKEY = Decayed(730)
TRAINER = Decayed(730)
OWNER = Decayed(1095)
JOCKEY_SHORT = Decayed(90)

# same-day bias accumulators: (date, venue, surf) -> list of (style_adv, gate_adv)
DAY = defaultdict(list)

rows = []
chunks = []
race_groups = runs.groupby('race_id', sort=False)
n_races = runs.race_id.nunique()
for ri, (rid, g) in enumerate(race_groups):
    if ri % 5000 == 0:
        print(f'{ri}/{n_races}', flush=True)
    surf = g.surf.iat[0]
    dist = int(g.distance.iat[0])
    now = int(g.date_ord.iat[0])
    venue = g.venue_name.iat[0]
    cls = float(g.cls.iat[0])
    cond = g.cond.iat[0]
    field = len(g)
    day_key = (now, venue, surf)

    hids = g.horse_id.values
    # ---------- pre-race features ----------
    m = np.zeros(field)
    v = np.zeros(field)
    feats = []
    day_list = DAY.get(day_key, [])
    if day_list:
        arr = np.array(day_list)
        day_style_adv = arr[:, 0].mean()
        day_gate_adv = arr[:, 1].mean()
        day_n = len(day_list)
    else:
        day_style_adv = np.nan
        day_gate_adv = np.nan
        day_n = 0

    for j, (hid, row) in enumerate(zip(hids, g.itertuples(index=False))):
        r_s = H_r[hid].get(surf)
        if r_s is None:
            m[j] = 0.0
            v[j] = P['V0']
        else:
            days = now - H_rd[hid][surf]
            m[j] = r_s
            v[j] = min(H_v[hid][surf] + P['Q'] * days / 30.0, P['V0'])
        other = 'D' if surf == 'T' else 'T'
        r_o = H_r[hid].get(other, np.nan)
        hist = H_hist[hid]
        f = {
            'race_id': rid, 'horse_number': row.horse_number,
            'rating': m[j] if r_s is not None else np.nan,
            'rating_sd': math.sqrt(v[j]),
            'rating_other': r_o,
            'n_runs': H_n[hid], 'n_surf': sum(1 for h in hist if h['surf'] == surf),
            'career_win_rate': H_wins[hid] / H_n[hid] if H_n[hid] else np.nan,
            'career_top3_rate': H_top3[hid] / H_n[hid] if H_n[hid] else np.nan,
            'prize_per_run': H_prize[hid] / H_n[hid] if H_n[hid] else np.nan,
            'max_cls': H_maxcls[hid] if H_n[hid] else np.nan,
        }
        if hist:
            last = hist[-1]
            f.update({
                'days_off': now - last['date'], 'last_finish_pct': last['fin_pct'], 'last_y': last['y'],
                'last_resid': last['resid'], 'last_style': last['style'], 'last_kick': last['kick'],
                'last_logodds': last['logodds'], 'last_pop_pct': last['pop_pct'], 'last_mkt_beat': last['mkt_beat'],
                'dist_change': dist - last['dist'], 'surf_change': int(last['surf'] != surf),
                'cls_change': cls - last['cls'], 'jockey_same': int(last['jockey'] == row.jockey_id),
                'impost_change': row.impost - last['impost'], 'last_cond': last['cond'],
                'last_venue_same': int(last['venue'] == venue),
            })
            ys = [h['y'] for h in hist][-5:]
            res_ = [h['resid'] for h in hist if not np.isnan(h['resid'])][-5:]
            styles = [h['style'] for h in hist if not np.isnan(h['style'])][-5:]
            kicks = [h['kick'] for h in hist if h['surf'] == surf and not np.isnan(h['kick'])][-5:]
            beats = [h['mkt_beat'] for h in hist][-5:]
            f.update({
                'y_mean5': np.nanmean(ys) if ys else np.nan, 'y_best5': np.nanmax(ys) if ys else np.nan,
                'resid_mean5': np.mean(res_) if res_ else np.nan,
                'style_mean5': np.mean(styles) if styles else np.nan,
                'style_min5': np.min(styles) if styles else np.nan,
                'kick_mean5': np.mean(kicks) if kicks else np.nan,
                'mkt_beat_mean5': np.mean(beats) if beats else np.nan,
                'logodds_mean5': np.mean([h['logodds'] for h in hist][-5:]),
                'fin_pct_mean5': np.mean([h['fin_pct'] for h in hist][-5:]),
            })
            # aptitude from residuals (Benter-style): similar distance / same venue / same going
            near = [h['resid'] for h in hist if h['surf'] == surf and abs(h['dist'] - dist) <= 200 and not np.isnan(h['resid'])]
            far = [h['resid'] for h in hist if h['surf'] == surf and abs(h['dist'] - dist) > 200 and not np.isnan(h['resid'])]
            ven = [h['resid'] for h in hist if h['venue'] == venue and h['surf'] == surf and not np.isnan(h['resid'])]
            wet = [h['resid'] for h in hist if h['surf'] == surf and h['cond'] >= 2 and not np.isnan(h['resid'])]
            f.update({
                'apt_dist': np.mean(near) if near else np.nan, 'n_dist': len(near),
                'apt_dist_gap': (np.mean(near) - np.mean(far)) if near and far else np.nan,
                'apt_venue': np.mean(ven) if ven else np.nan,
                'apt_wet': np.mean(wet) if wet else np.nan,
                'best_dist_fin': min([h['fin_pct'] for h in hist if h['surf'] == surf and abs(h['dist'] - dist) <= 200], default=np.nan),
            })
        # connections (decayed): n, wins, sum_mkt_p, top3
        for name, obj, key in (('jky', JOCKEY, row.jockey_id), ('trn', TRAINER, row.trainer_id),
                               ('own', OWNER, row.owner_id), ('jky90', JOCKEY_SHORT, row.jockey_id)):
            s = obj.get(key, now)
            f[f'{name}_n'] = s[0]
            f[f'{name}_win'] = (s[1] + 0.07 * 20) / (s[0] + 20)
            f[f'{name}_top3'] = (s[3] + 0.21 * 20) / (s[0] + 20)
            f[f'{name}_mratio'] = (s[1] + 5) / (s[2] + 5)          # actual wins / market-expected wins
        f['day_style_adv'] = day_style_adv
        f['day_gate_adv'] = day_gate_adv
        f['day_n'] = day_n
        feats.append(f)

    # field-relative features (rank within race), done after loop
    # ---------- results & updates ----------
    fin = g.finish.values
    completed = g.completed.values
    marg = g.margin_sec.values
    sec = g.second_time.values
    wt = g.win_time.values
    y = np.full(field, np.nan)
    ok = completed & ~np.isnan(marg)
    lead = (sec[0] - wt[0]) if not np.isnan(sec[0]) else 0.0
    for j in range(field):
        if not ok[j]:
            continue
        if fin[j] == 1:
            y[j] = min(lead * 1000.0 / dist, P['WCAP'])
        else:
            y[j] = max(-marg[j] * 1000.0 / dist, -P['CAP'])
    obs = ~np.isnan(y)
    resid = np.full(field, np.nan)
    if obs.sum() >= 3:
        w = 1.0 / (v[obs] + P['S2'])
        b = np.sum(w * (y[obs] - m[obs])) / w.sum()
        K = v / (v + P['S2'])
        resid[obs] = y[obs] - m[obs] - b
        for j in np.flatnonzero(obs):
            hid = hids[j]
            H_r[hid][surf] = m[j] + K[j] * resid[j]
            H_v[hid][surf] = max((1 - K[j]) * v[j], P['VMIN'])
            H_rd[hid][surf] = now

    style = g.first_corner.values / field
    l3 = g.last_3f.values
    med_l3 = np.nanmedian(l3) if np.any(~np.isnan(l3)) else np.nan
    pop = g.popularity.values
    gate_norm = (g.gate_number.values - 1) / 7.0
    # same-day bias record: top3 vs field average
    top3 = (fin <= 3) & completed
    if top3.sum() >= 3 and np.any(~np.isnan(style)):
        DAY[day_key].append((np.nanmean(style[top3]) - np.nanmean(style), gate_norm[top3].mean() - gate_norm.mean()))

    for j, (hid, row) in enumerate(zip(hids, g.itertuples(index=False))):
        fp = (fin[j] - 1) / (field - 1) if completed[j] else 1.0
        H_hist[hid].append({
            'date': now, 'surf': surf, 'dist': dist, 'venue': venue, 'cond': cond, 'cls': cls, 'y': y[j],
            'resid': resid[j], 'fin_pct': fp, 'style': style[j], 'kick': (med_l3 - l3[j]) if not np.isnan(l3[j]) else np.nan,
            'logodds': math.log(row.odds), 'pop_pct': (pop[j] - 1) / (field - 1),
            'mkt_beat': ((pop[j] - (fin[j] if completed[j] else field)) / (field - 1)),
            'jockey': row.jockey_id, 'impost': row.impost,
        })
        H_n[hid] += 1
        H_wins[hid] += int(fin[j] == 1)
        H_top3[hid] += int(completed[j] and fin[j] <= 3)
        H_prize[hid] += row.prize_money if not np.isnan(row.prize_money) else 0.0
        H_maxcls[hid] = max(H_maxcls[hid], cls)
        vec = np.array([1.0, float(fin[j] == 1), row.mkt_p, float(completed[j] and fin[j] <= 3)])
        JOCKEY.add(row.jockey_id, now, vec)
        TRAINER.add(row.trainer_id, now, vec)
        OWNER.add(row.owner_id, now, vec)
        JOCKEY_SHORT.add(row.jockey_id, now, vec)
    rows.extend(feats)
    if len(rows) >= 50000:                     # 辞書のまま全部ためるとメモリが足りない（8GBの環境）ので、5万行ごとに表にする
        chunks.append(pd.DataFrame(rows))
        rows = []

chunks.append(pd.DataFrame(rows))
F = pd.concat(chunks, ignore_index=True)
del chunks, rows
base = runs[['race_id', 'horse_number', 'date', 'year', 'race_num', 'start_min', 'venue_name', 'surf', 'distance', 'cond',
             'cls', 'handicap', 'female_only', 'listed', 'month', 'field', 'gate_number', 'sex', 'age', 'impost',
             'horse_weight', 'weight_change', 'jockey_id', 'trainer_id', 'finish', 'completed', 'odds', 'popularity',
             'mkt_p', 'dead_heat_win', 'horse_id']]
F = base.merge(F, on=['race_id', 'horse_number'], how='left')
# field-relative transforms
grp = F.groupby('race_id')
for c in ['rating', 'y_mean5', 'resid_mean5', 'kick_mean5', 'apt_dist', 'jky_win', 'trn_win', 'impost', 'style_mean5',
          'prize_per_run', 'career_top3_rate', 'logodds_mean5', 'mkt_beat_mean5', 'horse_weight']:
    F[f'{c}_rel'] = F[c] - grp[c].transform('mean')
    F[f'{c}_rk'] = grp[c].rank(pct=True)
F['rating_fill'] = F.rating.fillna(grp.rating.transform('min') - 0.1)
F['front_runners'] = grp.style_mean5.transform(lambda s: (s < 0.25).sum())
F['gate_pct'] = (F.gate_number - 1) / 7.0
F['sex_code'] = F.sex.map({'牡': 0, '牝': 1, 'セ': 2})
F['venue_code'] = F.venue_name.astype('category').cat.codes
F['surf_code'] = (F.surf == 'D').astype(int)
F['course_code'] = (F.venue_name + F.surf + F.distance.astype(str)).astype('category').cat.codes
F['y'] = (F.finish == 1).astype(int)
F.to_parquet(OUTFILE)
print('done', F.shape, OUTFILE)
