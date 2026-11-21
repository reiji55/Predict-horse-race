"""調教評価（netkeibaの無料で見られる記号 A〜D と短評）と血統（父・母父）。メインレース（11R）の馬が対象。
すべて発走前に分かる情報。集計は「その日より前」の走だけを使う（point-in-time）。

調教評価（その行が11Rのときだけ値が入る）
  trn_grade        : A=3, B=2, C=1, D=0
  trn_grade_rk     : レース内の順位（0〜1、大きいほど評価が高い）
  trn_n_A          : そのレースの A の頭数
  trn_grade_vs_own : 今回の記号 − その馬の過去の記号の平均（いつもより良いか）
  trn_critic_te    : 短評ごとの過去の成績（3着以内 − 3/頭数 の平均、50件分の0で縮める）
血統（父・母父が分かっている馬のすべての走に値が入る）
  sire_all / dsire_all       : 父（母父）の産駒の着順の良さ（1 − 着順率）の平均（縮め 30）
  sire_surf_edge / dsire_... : この芝ダでの良さ − 全体
  sire_dist_edge / dsire_... : この距離帯（〜1300/〜1700/〜2100/それ以上）での良さ − 全体
  sire_soft_edge             : 道悪（稍重以下）での良さ − 全体（今日が良馬場なら 0 を掛ける＝符号つきで入れる）
writes data/features_x9.parquet
"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'date', 'horse_id', 'horse_number', 'finish', 'field',
                                                  'surf', 'distance', 'cond'])
r['race_id'] = r.race_id.astype(str)
r['horse_id'] = r.horse_id.astype(str)
r['date'] = pd.to_datetime(r.date)
r = r.sort_values(['date', 'race_id', 'horse_number']).reset_index(drop=True)
ok = r.finish.notna() & (r.field >= 2)
r['good'] = np.where(ok, 1 - (r.finish - 1) / (r.field - 1).clip(lower=1), np.nan)
r['t3res'] = np.where(ok, (r.finish <= 3).astype(float) - 3 / r.field.clip(lower=3), np.nan)

# ---------- 調教評価 ----------
e = pd.read_csv(f'{D}/training_eval.csv', dtype=str).rename(columns={'horse_ref': 'horse_id'})
e['trn_grade'] = e.grade.map({'A': 3, 'B': 2, 'C': 1, 'D': 0}).astype(float)
r = r.merge(e[['race_id', 'horse_id', 'trn_grade', 'critic']], on=['race_id', 'horse_id'], how='left')
g = r.groupby('race_id')
r['trn_grade_rk'] = g.trn_grade.rank(pct=True)
r['trn_n_A'] = g.trn_grade.transform(lambda s: (s == 3).sum()).where(r.trn_grade.notna())


def prior_by_day(keys, val, shrink):
    """keys の組ごとに、その日より前の val の平均（shrink 件分の0で縮める）と件数。"""
    d = r[keys + ['date']].copy()
    d['v'] = r[val].fillna(0).values
    d['n'] = r[val].notna().astype(float).values
    valid = d[keys].notna().all(axis=1)
    day = d[valid].groupby(keys + ['date'], sort=False)[['v', 'n']].sum().reset_index().sort_values(keys + ['date'])
    c = day.groupby(keys)[['v', 'n']].cumsum()
    day['cv'], day['cn'] = c.v - day.v, c.n - day.n
    m = d[keys + ['date']].merge(day[keys + ['date', 'cv', 'cn']], on=keys + ['date'], how='left')
    return (m.cv / (m.cn + shrink)).values, m.cn.values


own_mean, own_n = prior_by_day(['horse_id'], 'trn_grade', 0)
r['trn_grade_vs_own'] = np.where(own_n > 0, r.trn_grade - own_mean, np.nan)
r['t3res_c'] = r.t3res.where(r.critic.notna())
r['trn_critic_te'] = np.where(r.critic.notna(), prior_by_day(['critic'], 't3res_c', 50)[0], np.nan)

# ---------- 血統 ----------
p = pd.read_csv(f'{D}/pedigree.csv', dtype=str).rename(columns={'horse_ref': 'horse_id'})
p['sire_k'] = p.sire_ref.fillna(p.sire)
p['dsire_k'] = p.damsire_ref.fillna(p.damsire)
r = r.merge(p[['horse_id', 'sire_k', 'dsire_k']], on='horse_id', how='left')
r['dband'] = pd.cut(r.distance, [0, 1300, 1700, 2100, 9999], labels=False)
r['soft'] = (r.cond.astype(float) >= 1).astype(int) if r.cond.dtype != object else r.cond.isin(['稍重', '重', '不良']).astype(int)
for who in ('sire', 'dsire'):
    k = f'{who}_k'
    allm, alln = prior_by_day([k], 'good', 30)
    base = np.where(alln > 0, allm * (alln + 30) / np.maximum(alln, 1), np.nan)      # 縮める前の平均
    r[f'{who}_all'] = np.where(r[k].notna(), (allm * (alln + 30) + 0.5 * 30) / (alln + 30), np.nan)   # 0.5 へ縮める
    for name, key in (('surf', 'surf'), ('dist', 'dband')):
        m, n = prior_by_day([k, key], 'good', 0)
        raw = np.where(n > 0, m, np.nan)               # shrink=0 → そのままの平均
        r[f'{who}_{name}_edge'] = np.where(r[k].notna(), np.nan_to_num(raw - base) * (n / (n + 30)), np.nan)
    if who == 'sire':
        m, n = prior_by_day([k, 'soft'], 'good', 0)
        raw = np.where(n > 0, m, np.nan)
        r['sire_cond_edge'] = np.where(r[k].notna(), np.nan_to_num(raw - base) * (n / (n + 30)), np.nan)
cols = ['trn_grade', 'trn_grade_rk', 'trn_n_A', 'trn_grade_vs_own', 'trn_critic_te', 'sire_all', 'sire_surf_edge',
        'sire_dist_edge', 'sire_cond_edge', 'dsire_all', 'dsire_surf_edge', 'dsire_dist_edge']
r[['race_id', 'horse_number', 'critic'] + cols].to_parquet(f'{D}/features_x9.parquet')
print(r[cols].describe().T[['count', 'mean', 'std']].round(4).to_string())
print('cond values', r.cond.value_counts().head(6).to_dict())
