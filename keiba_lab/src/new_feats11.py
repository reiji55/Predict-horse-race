"""実績馬どうしを共通の物差しで比べる材料（すべてレース前に分かる過去の結果だけ）。

A) 共通の物差し（週ごとのまとめ解き）
   その週より前の3年ぶんの走りを、芝・ダート別に一度に解く：
       y（着差の成績、1000mあたり秒） = 馬の力 a + レースの水準ずれ b + 斤量の効き g×(斤量−55)
   新しい走りほど重く（半減期1年）。同じ馬が別のレースに出ていることを手がかりに、
   別々のレースを同じ物差しに乗せる。あとから分かった相手の強さも、過去のレースの水準に反映される。
   1走ごとの値打ち pr = y − b − g×(斤量−55)
   net_abil        : 今回と同じ芝ダでの馬の力 a
   net_abil_oth    : もう一方の芝ダでの a
   net_n           : 解くのに使えた走数（同じ芝ダ）
   pr_last/mean3   : 直近走・直近3走の値打ち
   pr_best/top2    : 3年以内の最高値・上位2走の平均
   pr_best_1y      : 1年以内の最高値
   pr_best_dist    : 今回の距離±200mでの最高値
   pr_best_hi      : オープン・重賞での最高値、pr_hi_n : その走数
A2) 今日と同じ馬場の区分（良／稍重／重・不良）での走り
   gf_n, gf_gap（その区分での値打ち − その馬の平均、走数で縮める）, gf_best, gf_last, gf_last_age,
   gfd_best（同じ区分かつ距離±200m）, wet_n, wet_gap（稍重以下での値打ち − 平均）
B) 直接対決の着差
   今回の相手と過去3年に同じレースを走ったときの、成績の差の平均（対戦数で縮める）
   h2h_margin, h2h_margin_n
writes data/features_x11.parquet
"""
import sys
import time

import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix, vstack, identity, hstack, csr_matrix
from scipy.sparse.linalg import lsqr

D = '/home/claude/keiba_lab/data'
WIN, HALF, LAM, CAP, WCAP = 365 * 3, 365.0, 0.5, 2.0, 0.6
Y0 = int(sys.argv[1]) if len(sys.argv) > 1 else 2011
Y1 = int(sys.argv[2]) if len(sys.argv) > 2 else 2100
OUTF = sys.argv[3] if len(sys.argv) > 3 else f'{D}/features_x11.parquet'

r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'horse_id', 'horse_number', 'surf', 'distance', 'cls',
                                                  'impost', 'finish', 'time_seconds', 'margin_sec', 'second_time', 'field', 'jockey_id', 'cond'])
r['race_id'] = r.race_id.astype(str)
r['date'] = pd.to_datetime(r.date)
r = r.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
lead = (r.second_time - r.time_seconds) * 1000.0 / r.distance
y = np.where(r.finish == 1, np.minimum(lead, WCAP), np.maximum(-r.margin_sec * 1000.0 / r.distance, -CAP))
r['y'] = np.where(r.finish.notna() & r.time_seconds.notna(), y, np.nan)
r['ord'] = (r.date - pd.Timestamp('2000-01-03')).dt.days            # 2000-01-03 は月曜
r['week'] = r.ord // 7
r['hid'] = pd.factorize(r.horse_id)[0]
r['rid'] = pd.factorize(r.race_id)[0]
r['imp'] = (r.impost.astype(float) - 55.0).fillna(0.0)
r['jk'] = pd.factorize(r.jockey_id)[0]
r['gg'] = r.cond.clip(upper=2)                                         # 馬場の区分：0=良、1=稍重、2=重・不良
r['gap'] = r.ord - r.groupby('hid').ord.shift(1)                      # 前走からの日数（芝ダ問わず）
r['prev_gap'] = r.groupby('hid').gap.shift(1)
r['is_lay'] = (r.gap >= 90).astype(float)                              # 休み明け
r['is_sec'] = ((r.prev_gap >= 90) & (r.gap < 60)).astype(float)        # 休み明け2戦目
print('runs', len(r), 'y ok', int(r.y.notna().sum()), r.surf.value_counts().to_dict(), flush=True)

obs = r[r.y.notna()]
by_surf = {s: g for s, g in obs.groupby('surf')}
weeks = sorted(r.loc[r.date.dt.year.between(Y0, Y1), 'week'].unique())
parts, gam = [], []
t0 = time.time()
for wi, wk in enumerate(weeks):
    tgt = r[r.week == wk]
    start = wk * 7                                   # その週の月曜。これより前の走りだけを使う
    res = {}
    for s, o in by_surf.items():
        lo, hi = np.searchsorted(o.ord.values, [start - WIN, start])
        w = o.iloc[lo:hi]
        if len(w) < 500:
            continue
        hu, hi_ = np.unique(w.hid.values, return_inverse=True)
        ru, ri_ = np.unique(w.rid.values, return_inverse=True)
        n, nh, nr = len(w), len(hu), len(ru)
        sw = np.sqrt(0.5 ** ((start - w.ord.values) / HALF))
        rows = np.arange(n)
        A = coo_matrix((np.concatenate([sw, sw, sw * w.imp.values]),
                        (np.concatenate([rows, rows, rows]), np.concatenate([hi_, nh + ri_, np.full(n, nh + nr)]))),
                       shape=(n, nh + nr + 1)).tocsr()
        reg = hstack([identity(nh, format='csr') * np.sqrt(LAM), csr_matrix((nh, nr + 1))])
        reg2 = hstack([csr_matrix((nr, nh)), identity(nr, format='csr') * np.sqrt(1e-3), csr_matrix((nr, 1))])
        sol = lsqr(vstack([A, reg, reg2]).tocsr(), np.concatenate([sw * w.y.values, np.zeros(nh + nr)]),
                   atol=1e-7, btol=1e-7, iter_lim=400)[0]
        a, b, g = sol[:nh], sol[nh:nh + nr], sol[-1]
        gam.append((wk, s, g))
        pr = w.y.values - b[ri_] - g * w.imp.values
        cnt = np.bincount(hi_, minlength=nh)
        res[s] = (pd.DataFrame({'hid': hu, 'a': a, 'n': cnt}),
                  pd.DataFrame({'hid': w.hid.values, 'ord': w.ord.values, 'dist': w.distance.values, 'cls': w.cls.values, 'pr': pr,
                                'jk': w.jk.values, 'is_lay': w.is_lay.values, 'is_sec': w.is_sec.values, 'gg': w.gg.values}))
    out = tgt[['race_id', 'horse_number', 'hid', 'surf', 'distance', 'jk', 'is_lay', 'is_sec', 'gg']].copy()
    for c in ('net_abil', 'net_abil_oth', 'net_n', 'pr_last', 'pr_mean3', 'pr_best', 'pr_top2', 'pr_best_1y', 'pr_best_dist',
              'pr_best_hi', 'pr_hi_n', 'pr_best_age', 'pr_peak_drop', 'combo_n', 'combo_pr_gap', 'lay_n', 'lay_fit', 'sec_fit',
              'pr_best_oth', 'pr_oth_n', 'gf_n', 'gf_gap', 'gf_best', 'gf_last', 'gf_last_age', 'gfd_best', 'gfd_n', 'wet_n', 'wet_gap'):
        out[c] = np.nan
    for s in out.surf.unique():
        m = out.surf == s
        if s in res:
            ab, P = res[s]
            mp = ab.set_index('hid')
            out.loc[m, 'net_abil'] = out.loc[m, 'hid'].map(mp.a).values
            out.loc[m, 'net_n'] = out.loc[m, 'hid'].map(mp.n).fillna(0).values
            P = P[P.hid.isin(out.loc[m, 'hid'].values)].sort_values(['hid', 'ord'])
            if len(P):
                gp = P.groupby('hid').pr
                f = pd.DataFrame({'pr_last': gp.last(), 'pr_mean3': gp.apply(lambda x: x.iloc[-3:].mean()), 'pr_best': gp.max(),
                                  'pr_top2': gp.apply(lambda x: x.nlargest(2).mean()),
                                  'pr_best_1y': P[P.ord >= start - 365].groupby('hid').pr.max(),
                                  'pr_best_hi': P[P.cls >= 5].groupby('hid').pr.max(),
                                  'pr_hi_n': P[P.cls >= 5].groupby('hid').pr.size()})
                for c in f.columns:
                    out.loc[m, c] = out.loc[m, 'hid'].map(f[c]).values
                out.loc[m, 'pr_hi_n'] = out.loc[m, 'pr_hi_n'].fillna(0)
                best_ord = P.loc[gp.idxmax(), ['hid', 'ord']].set_index('hid').ord
                mean_all = gp.mean()
                hm = out.loc[m, 'hid']
                out.loc[m, 'pr_best_age'] = (start - hm.map(best_ord)).values
                out.loc[m, 'pr_peak_drop'] = (out.loc[m, 'pr_best'] - out.loc[m, 'pr_mean3']).values
                for flag, cn, cf in (('is_lay', 'lay_n', 'lay_fit'), ('is_sec', None, 'sec_fit')):
                    sub = P[P[flag] == 1].groupby('hid').pr.agg(['mean', 'size'])
                    k = hm.map(sub['size']).fillna(0).values
                    gapv = (hm.map(sub['mean']) - hm.map(mean_all)).fillna(0).values * k / (k + 2.0)
                    out.loc[m, cf] = gapv * out.loc[m, flag].values          # 今回がその場面のときだけ値が入る
                    if cn:
                        out.loc[m, cn] = k
                q = out.loc[m, ['race_id', 'horse_number', 'hid', 'jk']].merge(P[['hid', 'jk', 'pr']], on=['hid', 'jk'])
                q = q.groupby(['race_id', 'horse_number']).pr.agg(['mean', 'size'])
                idx = pd.MultiIndex.from_frame(out.loc[m, ['race_id', 'horse_number']])
                k = q['size'].reindex(idx).fillna(0).values
                out.loc[m, 'combo_n'] = k
                out.loc[m, 'combo_pr_gap'] = (q['mean'].reindex(idx).values - hm.map(mean_all).values) * k / (k + 2.0)
                out.loc[m, 'combo_pr_gap'] = out.loc[m, 'combo_pr_gap'].fillna(0)
                # 今日と同じ馬場の区分（良／稍重／重・不良）での走り
                q = out.loc[m, ['race_id', 'horse_number', 'hid', 'gg', 'distance']].merge(P[['hid', 'gg', 'dist', 'pr', 'ord']], on='hid', suffixes=('_t', ''))
                ma = hm.map(mean_all).values
                qg = q[q.gg == q.gg_t].sort_values('ord')
                a = qg.groupby(['race_id', 'horse_number']).agg(n=('pr', 'size'), mean=('pr', 'mean'), best=('pr', 'max'), last=('pr', 'last'), lo=('ord', 'last')).reindex(idx)
                k = a.n.fillna(0).values
                out.loc[m, 'gf_n'] = k
                out.loc[m, 'gf_gap'] = np.nan_to_num((a['mean'].values - ma) * k / (k + 2.0))
                out.loc[m, 'gf_best'] = a.best.values
                out.loc[m, 'gf_last'] = a['last'].values
                out.loc[m, 'gf_last_age'] = start - a.lo.values
                qd = qg[(qg.dist - qg.distance).abs() <= 200].groupby(['race_id', 'horse_number']).pr.agg(['max', 'size']).reindex(idx)
                out.loc[m, 'gfd_best'] = qd['max'].values
                out.loc[m, 'gfd_n'] = qd['size'].fillna(0).values
                qw = q[q.gg >= 1].groupby(['race_id', 'horse_number']).pr.agg(['mean', 'size']).reindex(idx)
                k = qw['size'].fillna(0).values
                out.loc[m, 'wet_n'] = k
                out.loc[m, 'wet_gap'] = np.nan_to_num((qw['mean'].values - ma) * k / (k + 2.0))
                q = out.loc[m, ['race_id', 'horse_number', 'hid', 'distance']].merge(P[['hid', 'dist', 'pr']], on='hid')
                q = q[(q.dist - q.distance).abs() <= 200].groupby(['race_id', 'horse_number']).pr.max()
                idx = pd.MultiIndex.from_frame(out.loc[m, ['race_id', 'horse_number']])
                out.loc[m, 'pr_best_dist'] = q.reindex(idx).values
        for s2 in res:
            if s2 != s:
                out.loc[m, 'net_abil_oth'] = out.loc[m, 'hid'].map(res[s2][0].set_index('hid').a).values
                go = res[s2][1].groupby('hid').pr.agg(['max', 'size'])
                out.loc[m, 'pr_best_oth'] = out.loc[m, 'hid'].map(go['max']).values
                out.loc[m, 'pr_oth_n'] = out.loc[m, 'hid'].map(go['size']).fillna(0).values
    parts.append(out.drop(columns=['hid', 'surf', 'distance', 'jk', 'is_lay', 'is_sec', 'gg']))
    if wi % 50 == 0:
        print(f'{wi}/{len(weeks)} {time.time() - t0:.0f}s', flush=True)
out = pd.concat(parts, ignore_index=True)
print('impost coef', pd.DataFrame(gam, columns=['wk', 's', 'g']).groupby('s').g.describe().round(4).to_string(), flush=True)

# ---- B) 直接対決の着差 ----
print('h2h margin ...', flush=True)
T = r.loc[r.date.dt.year.between(Y0, Y1), ['race_id', 'horse_number', 'hid', 'ord']].rename(columns={'race_id': 'trace', 'ord': 'tord'})
Pp = obs[['hid', 'rid', 'ord', 'y']]
hs = []
for lo in range(0, len(T), 60000):
    j = T.iloc[lo:lo + 60000].merge(Pp, on='hid')
    j = j[(j.ord < j.tord) & (j.ord >= j.tord - WIN)]
    g = j.groupby(['trace', 'rid']).y
    j['n'] = g.transform('size')
    j = j[j.n >= 2].copy()
    j['sum'] = j.groupby(['trace', 'rid']).y.transform('sum')
    j['d'] = (j.n * j.y - j['sum']) / (j.n - 1)          # 同じ過去レースにいた今回の相手の平均との差
    j['w'] = j.n - 1
    j['wd'] = j.w * j.d
    a = j.groupby(['trace', 'horse_number'])[['wd', 'w']].sum()
    hs.append(a)
    del j
h = pd.concat(hs)
h = h.groupby(level=[0, 1]).sum()
h['h2h_margin'] = h.wd / (h.w + 2.0)
h = h.rename(columns={'w': 'h2h_margin_n'})[['h2h_margin', 'h2h_margin_n']].reset_index().rename(columns={'trace': 'race_id'})
out = out.merge(h, on=['race_id', 'horse_number'], how='left')
out['h2h_margin'] = out.h2h_margin.fillna(0.0)
out['h2h_margin_n'] = out.h2h_margin_n.fillna(0.0)
g = out.groupby('race_id')
for c in ('net_abil', 'pr_best', 'pr_top2', 'pr_mean3'):
    out[f'{c}_rk'] = g[c].rank(ascending=False, pct=True)
out['gf_best_rk'] = g.gf_best.rank(ascending=False, pct=True)
# ---- C) 今回の相手の中で3着以内に届くか：自分を除いた相手の3番目の値との差 ----
def third_of_others(col):
    rk = g[col].rank(ascending=False, method='first')
    srt = out[['race_id', col]].assign(rk=rk).dropna()
    v3 = srt[srt.rk == 3].set_index('race_id')[col]
    v4 = srt[srt.rk == 4].set_index('race_id')[col]
    return np.where(rk <= 3, out.race_id.map(v4), out.race_id.map(v3))
t_net, t_m3 = third_of_others('net_abil'), third_of_others('pr_mean3')
out['gap_net3'] = out.net_abil - t_net
out['gap_prm3'] = out.pr_mean3 - t_m3
out['gap_peak3'] = out.pr_best - t_m3
out['gap_top2_3'] = out.pr_top2 - t_m3
out['field_net_top3'] = out.race_id.map(out.assign(rk=g.net_abil.rank(ascending=False, method='first')).query('rk <= 3').groupby('race_id').net_abil.mean())
out['field_known'] = g.net_abil.transform(lambda x: x.notna().mean())
out.to_parquet(OUTF)
print(out.describe().T[['count', 'mean', 'std', 'min', 'max']].round(3).to_string())
