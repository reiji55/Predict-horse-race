import lightgbm as lgb, numpy as np, pandas as pd
from models_lib import load_features
D='/home/claude/keiba_lab/data'
ODDS={'last_logodds','last_pop_pct','last_mkt_beat','logodds_mean5','mkt_beat_mean5','logodds_mean5_rel','logodds_mean5_rk','mkt_beat_mean5_rel','mkt_beat_mean5_rk','jky_mratio','trn_mratio','own_mratio','jky90_mratio','log_pi','pi_rank'}
F,FUND=load_features(f'{D}/features_v5.parquet'); F['race_id']=F.race_id.astype(str)
base=[c for c in FUND if c not in ODDS and not c.startswith(('pred_','n_pred_front'))]
for fn in ('features_x3','features_x4'):
    X=pd.read_parquet(f'{D}/{fn}.parquet'); X['race_id']=X.race_id.astype(str); F=F.merge(X,on=['race_id','horse_number'],how='left')
x3=[c for c in pd.read_parquet(f'{D}/features_x3.parquet').columns if c not in('race_id','horse_number')]
x4=[c for c in pd.read_parquet(f'{D}/features_x4.parquet').columns if c not in('race_id','horse_number')]
r=pd.read_parquet(f'{D}/runs.parquet',columns=['race_id','horse_number','first_corner']); r['race_id']=r.race_id.astype(str)
F=F.merge(r,on=['race_id','horse_number'],how='left'); F['t']=F.first_corner/F.field
# race-relative versions of the new start features
for c in ('late_start_rate','jky_pos_bias','jky_pos_bias_x','prior_pos1_mean'):
    F[c+'_rel']=F[c]-F.groupby('race_id')[c].transform('mean'); x4.append(c+'_rel')
pp=dict(objective='regression',learning_rate=0.08,num_leaves=63,min_data_in_leaf=300,feature_fraction=0.7,bagging_fraction=0.8,bagging_freq=1,lambda_l2=10.0,max_bin=127,num_threads=2,seed=1,verbosity=-1)
tr=F.year.between(2011,2022)&F.t.notna(); te=(F.year>=2024)&F.t.notna()
for name,fe in (('今の材料＋テン',base+x3),('＋出遅れ癖・騎手の傾向',base+x3+x4)):
    b=lgb.train(pp,lgb.Dataset(F.loc[tr,fe].values.astype(np.float32),F.loc[tr,'t'].values),400)
    p=b.predict(F.loc[te,fe].values.astype(np.float32))
    print(name,'R2=',round(np.corrcoef(p,F.loc[te,'t'])[0,1]**2,4),flush=True)
