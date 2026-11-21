"""確定単勝オッズから、各馬の「市場が見た3着以内の確率」を全レースについて出す（学習の先生役に使う。式の入力にはしない）。
q = 単勝の支持率（合計1に正規化）。2着・3着は残りの馬で q^GQ・q^DQ に比例。data/mkt_top3.parquet"""
import numpy as np
import pandas as pd

from pure_eval_lib import top3_probs, GQ, DQ

D = '/home/claude/keiba_lab/data'
r = pd.read_parquet(f'{D}/runs.parquet', columns=['race_id', 'horse_number', 'mkt_p'])
r['race_id'] = r.race_id.astype(str)
r = r.sort_values(['race_id', 'horse_number']).reset_index(drop=True)
out = np.full(len(r), np.nan)
starts = np.flatnonzero(np.r_[True, r.race_id.values[1:] != r.race_id.values[:-1]])
ends = np.r_[starts[1:], len(r)]
mp = r.mkt_p.values
for a, b in zip(starts, ends):
    q = mp[a:b]
    ok = ~np.isnan(q) & (q > 0)
    if ok.sum() < 4:
        continue
    qq = q[ok] / q[ok].sum()
    res = np.full(b - a, np.nan)
    res[ok] = top3_probs(qq, qq ** GQ, qq ** DQ)
    out[a:b] = res
r['q3'] = out
r[['race_id', 'horse_number', 'q3']].to_parquet(f'{D}/mkt_top3.parquet')
print(r.q3.describe().round(4).to_string())
