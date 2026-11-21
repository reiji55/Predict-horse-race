"""Win-bet backtest at (final) odds for a prediction column: bet 100 yen when p * odds > threshold."""
import sys
import os
MAIN_ONLY = os.environ.get("MAIN") == "1"
import numpy as np
import pandas as pd
from common import D, OUT

pred_file = sys.argv[1]
pcol = sys.argv[2]
pr = pd.read_parquet(pred_file)
runs = pd.read_parquet(f'{D}/runs.parquet')[['race_id', 'horse_number', 'odds', 'finish', 'race_num']]
df = pr.merge(runs, on=['race_id', 'horse_number'])
if MAIN_ONLY:
    df = df[df.race_num == 11]
df['win'] = (df.finish == 1)
df['ev'] = df[pcol] * df.odds
print(f'== {pcol}: rows {len(df)} races {df.race_id.nunique()} years {sorted(df.year.unique())}')
for th in [0.9, 1.0, 1.1, 1.2, 1.3, 1.5]:
    s = df[df.ev > th]
    ret = (s.win * s.odds * 100)
    roi = ret.sum() / (len(s) * 100)
    top = ret.sort_values(ascending=False)
    roi_ex5 = (ret.sum() - top.head(5).sum()) / (len(s) * 100) if len(s) > 5 else np.nan
    by = ' '.join(f'{y}:{(g.win * g.odds).sum() / len(g):.3f}' for y, g in s.groupby('year'))
    print(f'EV>{th}: bets={len(s):6d} wins={int(s.win.sum()):5d} hit={s.win.mean():.3f} ROI={roi:.4f} '
          f'(excl. top5 payouts {roi_ex5:.4f})  avg odds={s.odds.mean():.1f}  | {by}')
# by odds band at EV>1.1
s = df[df.ev > 1.1].copy()
s['band'] = pd.cut(s.odds, [1, 3, 5, 10, 20, 50, 100, 1000])
print(s.groupby('band', observed=True).apply(lambda g: pd.Series({'bets': len(g), 'hit': g.win.mean(),
      'roi': (g.win * g.odds).sum() / len(g)}), include_groups=False).round(3).to_string())
# calibration
b = pd.cut(df[pcol], [0, .01, .02, .05, .1, .2, .3, .5, 1])
print(df.groupby(b, observed=True).agg(n=('win', 'size'), p=(pcol, 'mean'), rate=('win', 'mean')).round(4).to_string())
