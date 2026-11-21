cd /home/claude/keiba_lab/src
L=../out/exp9.log
A=tr_luck_last,tr_luck_mean3,tr_resid_last,tr_resid_mean3,tr_resid_best5,meet_day,away,dir_edge,dir_n,h2h_net_per,h2h_rate,h2h_meet,pred_pos1,pred_pos4,pred_pos1_rk,pred_pos4_rk,n_pred_front,pred_front_gap
T=ten_last,ten_mean3,ten_best5,pos1_mean3,ten_n,ten_mean3_rk,ten_best5_rk,race_n_fast,race_ten_min,ten_gap_to_fastest
S=sub_pos1,sub_pos4,sub_l3,sub_margin,sub_pace,sub_trip_fin,sub_trip_luck,sub_pos4_x_pace,sub_l3_x_pace,sub_pos1_rk,sub_pos4_rk,sub_l3_rk,sub_margin_rk,sub_trip_fin_rk,sub_trip_luck_rk
P=pos1_sd5,late_start_rate,jky_pos_bias,trn_pos_bias,jky_pos_bias_x
U=l3_adj_last,l3_adj_mean3,l3_adj_best5,l3_adj_pos_last,l3_adj_pos_mean3,l3_adj_pos_best5,l3_vs_race_last,l3_vs_race_mean3,l3_vs_race_best5,l3_adj_mean3_rk,l3_adj_best5_rk,l3_adj_pos_mean3_rk,l3_adj_pos_best5_rk
python3 exp_harness.py chain4 $A,$T,$S,$P,$U,meet_front_adv,meet_inner_adv,meet_n,meet_front_fit,meet_inner_fit,bw_best_gap,bw_best_gap_abs,bw_win_gap,bw_vs_mean,bw_max_gap,bw_trend3,bw_rel_field >> $L 2>&1
SEED=2 python3 exp_harness.py chain4_s2 $A,$T,$S,$P,$U,meet_front_adv,meet_inner_adv,meet_n,meet_front_fit,meet_inner_fit,bw_best_gap,bw_best_gap_abs,bw_win_gap,bw_vs_mean,bw_max_gap,bw_trend3,bw_rel_field >> $L 2>&1
echo DONE >> $L
