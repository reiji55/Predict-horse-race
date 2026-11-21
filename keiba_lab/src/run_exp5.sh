cd /home/claude/keiba_lab/src
L=../out/exp5.log
X=tr_luck_last,tr_luck_mean3,tr_resid_last,tr_resid_mean3,tr_resid_best5,meet_day,away,dir_edge,dir_n,h2h_net_per,h2h_rate,h2h_meet,pred_pos1,pred_pos4,pred_pos1_rk,pred_pos4_rk,n_pred_front,pred_front_gap,jky_today_n,jky_today_edge,run_in_block,prev_fresh,gap_change
python3 exp_harness.py best $X >> $L 2>&1
SEED=2 python3 exp_harness.py best_s2 $X >> $L 2>&1
SPLIT=surf_code python3 exp_harness.py best_splitsurf $X >> $L 2>&1
echo DONE >> $L
