"""Convert stage scores (s1, s2, s3) into the win-prob / place-score files that eval_order.py reads.
usage: python make_pure_inputs.py SCORE_FILE TAG"""
import sys

import numpy as np
import pandas as pd

from common import OUT

sc = pd.read_parquet(sys.argv[1])
tag = sys.argv[2]
sc = sc.sort_values(['race_id', 'horse_number']).copy()
e = np.exp(sc.s1 - sc.groupby('race_id').s1.transform('max'))
sc['p_res'] = e / e.groupby(sc.race_id).transform('sum')
sc[['race_id', 'horse_number', 'year', 'p_res']].to_parquet(f'{OUT}/win_{tag}.parquet')
sc[['race_id', 'horse_number', 's2', 's3']].to_parquet(f'{OUT}/place_{tag}.parquet')
print('ok', len(sc))
