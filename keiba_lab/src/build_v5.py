"""features_v5 = features_v3 + 新しい材料（展開補正・位置取りの予測・開催日次・輸送・左右回り・直接対決・
騎手の当日の調子・休み明けから何戦目か）と、そのレース内の平均との差。"""
import numpy as np
import pandas as pd

D = '/home/claude/keiba_lab/data'
X_COLS = ['tr_luck_last', 'tr_luck_mean3', 'tr_resid_last', 'tr_resid_mean3', 'tr_resid_best5', 'meet_day', 'away',
          'dir_edge', 'dir_n', 'h2h_net_per', 'h2h_rate', 'h2h_meet', 'pred_pos1', 'pred_pos4', 'pred_pos1_rk',
          'pred_pos4_rk', 'n_pred_front', 'pred_front_gap', 'jky_today_n', 'jky_today_edge', 'run_in_block',
          'prev_fresh', 'gap_change']
F = pd.read_parquet(f'{D}/features_v3.parquet')
F['race_id'] = F.race_id.astype(str)
import pyarrow.parquet as pq
for fname in ('features_x', 'features_pos', 'features_x2'):
    have = [c for c in X_COLS if c in pq.read_schema(f'{D}/{fname}.parquet').names]
    X = pd.read_parquet(f'{D}/{fname}.parquet', columns=['race_id', 'horse_number'] + have)
    X['race_id'] = X.race_id.astype(str)
    F = F.merge(X, on=['race_id', 'horse_number'], how='left')
for c in X_COLS:
    F[f'{c}_rel'] = (F[c] - F.groupby('race_id')[c].transform('mean')).astype(np.float32)
    F[c] = F[c].astype(np.float32)
F.to_parquet(f'{D}/features_v5.parquet')
print(F.shape)
