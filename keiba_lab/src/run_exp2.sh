cd /home/claude/keiba_lab/src
L=../out/exp2.log
python3 exp_harness.py base none >> $L 2>&1
python3 exp_harness.py trip tr_luck_last,tr_luck_mean3,tr_resid_last,tr_resid_mean3,tr_resid_best5 >> $L 2>&1
python3 exp_harness.py pos pred_pos1,pred_pos4,pred_pos1_rk,pred_pos4_rk,n_pred_front,pred_front_gap >> $L 2>&1
python3 exp_harness.py allx tr_luck_last,tr_luck_mean3,tr_resid_last,tr_resid_mean3,tr_resid_best5,meet_day,away,dir_edge,dir_n,h2h_net_per,h2h_rate,h2h_meet,pred_pos1,pred_pos4,pred_pos1_rk,pred_pos4_rk,n_pred_front,pred_front_gap >> $L 2>&1
python3 exp_harness.py recent5 none recent5 >> $L 2>&1
python3 exp_margin.py >> $L 2>&1
echo DONE >> $L
