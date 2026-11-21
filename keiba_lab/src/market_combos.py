"""Are high-payout combinations mispriced?  Uses only the win market + finishing orders + real payouts.

1. fit gamma/delta (discounted Harville) on finishing orders 2014-07..2019 (win-market probs only)
2. for every race 2014-07.. compute all combo probabilities from the win market
3. compare with the combination market via the realised payout:
     LLR = log P_model(realised) - log q_market(realised),  q_market = rate*100/payout
     Kelly-proportional growth per race = LLR + log(rate)
4. 'buy every combination in a band' ROI, bands defined pre-race by the model probability
"""
import sys
import numpy as np
import pandas as pd
from common import D, OUT, race_index, fit_clogit
from combos import combo_tables, RATE

PROB_COL = sys.argv[1] if len(sys.argv) > 1 else 'mkt_p'
TAG = sys.argv[2] if len(sys.argv) > 2 else 'market'

runs = pd.read_parquet(f'{D}/runs.parquet')
if PROB_COL != 'mkt_p':
    extra = pd.read_parquet(f'{OUT}/{PROB_COL}.parquet')        # columns race_id, horse_number, p
    runs = runs.merge(extra, on=['race_id', 'horse_number'], how='inner')
    runs['pwin'] = runs['p']
else:
    runs['pwin'] = runs['mkt_p']
runs = runs[(runs.date >= '2014-07-01')].copy()
runs = runs.sort_values(['date', 'start_min', 'race_id', 'horse_number']).reset_index(drop=True)
pay = pd.read_parquet(f'{D}/payouts.parquet')
pay = pay[pay.race_id.isin(runs.race_id.unique())]

# ---------- fit gamma, delta on 2014-07..2019 ----------
fit = runs[(runs.year <= 2019)]
fit = fit[fit.race_id.map(fit.groupby('race_id').finish.apply(lambda s: s.isin([1, 2, 3]).sum() == 3 and s.eq(1).sum() == 1 and s.eq(2).sum() == 1))]


def exploded(df, place):
    # candidates = horses not finishing better than `place`
    d = df[~(df.finish < place)].copy()           # keeps NaN finish (non-completers) as candidates
    d['y'] = (d.finish == place).astype(float)
    d = d[d.groupby('race_id').y.transform('sum') == 1]
    s, i = race_index(d.race_id.values)
    return np.log(d.pwin.values)[:, None], d.y.values, s, i


X2, y2, s2, i2 = exploded(fit, 2)
g, _ = fit_clogit(X2, y2, s2, i2)
X3, y3, s3, i3 = exploded(fit, 3)
dlt, _ = fit_clogit(X3, y3, s3, i3)
gamma, delta = g[0], dlt[0]
print(f'[{TAG}] gamma={gamma:.4f} delta={delta:.4f}  (Harville = 1, 1)')

# ---------- payouts per race ----------
pay_by = {bt: pay[pay.bet_type == bt].groupby('race_id') for bt in ['馬連', 'ワイド', '馬単', '三連複', '三連単']}
pay_maps = {}
for bt in ['馬連', '馬単', '三連複', '三連単']:
    d = pay[pay.bet_type == bt]
    cnt = d.groupby('race_id').size()
    d = d[d.race_id.map(cnt) == 1]                                   # skip dead heats / multiple winners
    pay_maps[bt] = dict(zip(d.race_id, zip(d.horse_numbers.apply(tuple), d.payout)))
wd = pay[pay.bet_type == 'ワイド']
wide_map = {}
for rid, grp in wd.groupby('race_id'):
    wide_map[rid] = {tuple(sorted(h)): v for h, v in zip(grp.horse_numbers, grp.payout)}

ODD_BANDS = np.array([0, 10, 30, 100, 300, 1000, 3000, 10000, 1e12])
LBL = ['<10', '10-30', '30-100', '100-300', '300-1k', '1k-3k', '3k-10k', '>10k']


def band(prob, rate):
    return np.searchsorted(ODD_BANDS, rate / np.clip(prob, 1e-12, 1), side='right') - 1


acc = {bt: {'bets': np.zeros(8), 'hits': np.zeros(8), 'ret': np.zeros(8)} for bt in ['馬連', 'ワイド', '三連複', '三連単']}
rows = []
for model_name, (gm, dl) in {'harville': (1.0, 1.0), 'discount': (gamma, delta)}.items():
    pass

out_rows = []
for rid, grp in runs.groupby('race_id', sort=False):
    p = grp.pwin.values.astype(float)
    p = p / p.sum()
    hn = grp.horse_number.values
    pos = {h: i for i, h in enumerate(hn)}
    year = grp.year.iat[0]
    rec = {'race_id': rid, 'year': year, 'n': len(p)}
    for mname, (gm, dl) in (('H', (1.0, 1.0)), ('D', (gamma, delta))):
        P2, P3, um, srp, wide = combo_tables(p, gm, dl)
        for bt, key in (('馬連', 'um'), ('馬単', 'ut'), ('三連複', 'srp'), ('三連単', 'srt')):
            if rid not in pay_maps[bt]:
                continue
            combo, payout = pay_maps[bt][rid]
            try:
                ix = [pos[h] for h in combo]
            except KeyError:
                continue
            if bt == '馬連':
                pm = um[ix[0], ix[1]]
            elif bt == '馬単':
                pm = P2[ix[0], ix[1]]
            elif bt == '三連複':
                pm = srp[ix[0], ix[1], ix[2]]
            else:
                pm = P3[ix[0], ix[1], ix[2]]
            q = RATE[bt] * 100.0 / payout
            rec[f'{mname}_{bt}_llr'] = np.log(max(pm, 1e-12)) - np.log(q)
            rec[f'{mname}_{bt}_pay'] = payout
            rec[f'{mname}_{bt}_pm'] = pm
        if mname == 'D':
            # band ROI (flat 100 yen on every combination in band)
            n = len(p)
            iu = np.triu_indices(n, 1)
            for bt, mat in (('馬連', um), ('ワイド', wide)):
                probs = mat[iu]
                b = band(probs, RATE[bt])
                acc[bt]['bets'] += np.bincount(b, minlength=8)
                if bt == '馬連' and rid in pay_maps['馬連']:
                    combo, payout = pay_maps['馬連'][rid]
                    if all(h in pos for h in combo):
                        i, j = sorted(pos[h] for h in combo)
                        bb = band(np.array([mat[i, j]]), RATE[bt])[0]
                        acc[bt]['hits'][bb] += 1
                        acc[bt]['ret'][bb] += payout
                if bt == 'ワイド' and rid in wide_map:
                    for combo, payout in wide_map[rid].items():
                        if all(h in pos for h in combo):
                            i, j = sorted(pos[h] for h in combo)
                            bb = band(np.array([mat[i, j]]), RATE[bt])[0]
                            acc[bt]['hits'][bb] += 1
                            acc[bt]['ret'][bb] += payout
            comb = np.array(list(__import__('itertools').combinations(range(n), 3)))
            probs = srp[comb[:, 0], comb[:, 1], comb[:, 2]]
            b = band(probs, RATE['三連複'])
            acc['三連複']['bets'] += np.bincount(b, minlength=8)
            if rid in pay_maps['三連複']:
                combo, payout = pay_maps['三連複'][rid]
                if all(h in pos for h in combo):
                    i, j, k = [pos[h] for h in combo]
                    bb = band(np.array([srp[i, j, k]]), RATE['三連複'])[0]
                    acc['三連複']['hits'][bb] += 1
                    acc['三連複']['ret'][bb] += payout
            perm = P3[P3 > 0]
            b = band(perm, RATE['三連単'])
            acc['三連単']['bets'] += np.bincount(b, minlength=8)
            if rid in pay_maps['三連単']:
                combo, payout = pay_maps['三連単'][rid]
                if all(h in pos for h in combo):
                    i, j, k = [pos[h] for h in combo]
                    bb = band(np.array([P3[i, j, k]]), RATE['三連単'])[0]
                    acc['三連単']['hits'][bb] += 1
                    acc['三連単']['ret'][bb] += payout
    out_rows.append(rec)

df = pd.DataFrame(out_rows)
df.to_parquet(f'{OUT}/combo_llr_{TAG}.parquet')
print('races', len(df))
for bt in ['馬連', '馬単', '三連複', '三連単']:
    for m in ('H', 'D'):
        c = f'{m}_{bt}_llr'
        if c not in df:
            continue
        x = df[c].dropna()
        g_ = x + np.log(RATE[bt])
        tr = df.loc[x.index, 'year'] <= 2019
        print(f'{bt} {m}: n={len(x)} meanLLR all={x.mean():+.4f} (<=2019 {x[tr].mean():+.4f}, >=2020 {x[~tr].mean():+.4f})'
              f'  Kelly-proportional growth/race={g_.mean():+.4f}')

print('\nFlat-bet every combination, bands by model-implied odds (rate/P), 2014-07..2026:')
for bt in ['馬連', 'ワイド', '三連複', '三連単']:
    a = acc[bt]
    t = pd.DataFrame({'band': LBL, 'bets': a['bets'].astype(int), 'hits': a['hits'].astype(int),
                      'roi': a['ret'] / np.maximum(a['bets'] * 100, 1)})
    t['hit_rate'] = t.hits / t.bets.clip(lower=1)
    print(f'--- {bt}  overall ROI {a["ret"].sum() / (a["bets"].sum() * 100):.4f}')
    print(t.to_string(index=False))
