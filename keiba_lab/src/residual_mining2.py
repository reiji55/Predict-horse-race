"""今の最良の式（chain4 2本平均）に、もう一度「外れ方の分析」をかける。
ずれ = 実際の3着以内(0/1) − 式の見立て（レースごとに合計3になるよう調整した確率）。
切り口ごとに段に分け、2024年で見つけた偏りが 2025–2026年でも同じ向き・同じ大きさで出るかを見る。"""
import numpy as np
import pandas as pd

D, OUT = '/home/claude/keiba_lab/data', '/home/claude/keiba_lab/out'
a = pd.read_parquet(f'{OUT}/exp_chain4.parquet')
b = pd.read_parquet(f'{OUT}/exp_chain4_s2.parquet')
P = a[['race_id', 'horse_number', 'year', 'finish', 'top3', 'race_num']].copy()
P['s'] = (a.s.values + b.s.values) / 2
P['race_id'] = P.race_id.astype(str)


def shift_to_sum(logit, target, iters=30):
    c = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(logit + c)))
        c -= (p.sum() - target) / max((p * (1 - p)).sum(), 1e-9)
    return 1 / (1 + np.exp(-(logit + c)))


P['p'] = P.groupby('race_id').s.transform(lambda x: pd.Series(shift_to_sum(x.values, min(3, len(x))), index=x.index))
P['resid'] = P.top3 - P.p
F = pd.read_parquet(f'{D}/features_v3.parquet')
F['race_id'] = F.race_id.astype(str)
P = P.merge(F, on=['race_id', 'horse_number'], how='left', suffixes=('', '_f'))
runs = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'owner_id', 'trainer_id'])
runs['race_id'] = runs.race_id.astype(str)
P = P.merge(runs, on=['race_id', 'horse_number'], how='left', suffixes=('', '_r'))
P['period'] = np.where(P.year == 2024, 'find', 'check')
P['rk'] = P.groupby('race_id').s.rank(ascending=False)

# 切り口（式に入っている材料も、段の端で見立てがずれていないかを見る）
cands = {
    '式の順位': P.rk.clip(upper=12),
    '頭数': pd.cut(P.field, [0, 10, 13, 16, 18]),
    'クラス': P.cls, '芝ダ': P.surf_code, '競馬場': P.venue_code, '距離帯': pd.cut(P.distance, [0, 1300, 1700, 2100, 4000]),
    '馬場': P.cond, '月': P.month, '年齢': P.age.clip(upper=7), '性': P.sex_code,
    '休養日数': pd.cut(P.days_off, [0, 20, 35, 63, 120, 240, 9999]),
    '距離変更': pd.cut(P.dist_change, [-9999, -401, -201, -1, 0, 200, 400, 9999]),
    '芝ダ替わり': P.surf_change, 'クラス変更': P.cls_change.clip(-2, 2), '乗り替わり': P.jockey_same,
    '斤量変化': P.impost_change.clip(-3, 3).round(), '馬番の位置': pd.cut(P.gate_pct, [-0.01, 0.2, 0.4, 0.6, 0.8, 1.0]),
    'キャリア': pd.cut(P.n_runs, [-1, 0, 2, 5, 10, 20, 999]), '馬体重': pd.cut(P.horse_weight, [0, 440, 470, 500, 530, 999]),
    'ハンデ戦': P.handicap, '牝馬限定': P.female_only, 'レース番号': P.race_num_f if 'race_num_f' in P else P.race_num,
}
rows = []
for name, seg in cands.items():
    t = P.assign(seg=seg).groupby(['seg', 'period'], observed=True).agg(n=('resid', 'size'), r=('resid', 'mean'),
                                                                          act=('top3', 'mean'), pred=('p', 'mean')).unstack('period')
    for lvl in t.index:
        nf, nc = t.loc[lvl, ('n', 'find')], t.loc[lvl, ('n', 'check')]
        if not (nf >= 800 and nc >= 1500):
            continue
        rf, rc = t.loc[lvl, ('r', 'find')], t.loc[lvl, ('r', 'check')]
        se_c = np.sqrt(0.17 / nc)
        rows.append({'切り口': name, '段': str(lvl), 'n_2025-26': int(nc), 'ずれ_2024(pt)': round(rf * 100, 2),
                     'ずれ_2025-26(pt)': round(rc * 100, 2), 'z_2025-26': round(rc / se_c, 1),
                     '実際_2025-26': round(t.loc[lvl, ('act', 'check')] * 100, 1),
                     '見立て_2025-26': round(t.loc[lvl, ('pred', 'check')] * 100, 1),
                     '同じ向き': bool(np.sign(rf) == np.sign(rc))})
R = pd.DataFrame(rows)
R = R[R['同じ向き'] & (R['z_2025-26'].abs() >= 2.5)].sort_values('z_2025-26', key=abs, ascending=False)
pd.set_option('display.width', 200)
print('2024で見つけ、2025–26でも同じ向きで |z|≥2.5 のずれ（実際 − 見立て）')
print(R.head(30).to_string(index=False))
