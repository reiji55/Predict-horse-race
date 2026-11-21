# 取った結果・出馬表を足して、材料づくり → 本命の式（前日版の5本：p1×2, p2×2, pcat）→ 5点の合計50%以上のレースを選ぶ、までを通しで回す。
#   bash run_picks.sh         保存してある式で見立てだけ出す（毎週・当日朝はこれ。作り直さないので速い）
#   bash run_picks.sh train   式を作り直して保存する（材料や作りを変えたときだけ）
#   bash run_picks.sh score   材料は作り直さず、式の見立てと選び出しだけ
cd /home/claude/keiba_lab/src
L=../out/run_picks.log
A=tr_luck_last,tr_luck_mean3,tr_resid_last,tr_resid_mean3,tr_resid_best5,meet_day,away,dir_edge,dir_n,h2h_net_per,h2h_rate,h2h_meet,pred_pos1,pred_pos4,pred_pos1_rk,pred_pos4_rk,n_pred_front,pred_front_gap
T=ten_last,ten_mean3,ten_best5,pos1_mean3,ten_n,ten_mean3_rk,ten_best5_rk,race_n_fast,race_ten_min,ten_gap_to_fastest
S=sub_pos1,sub_pos4,sub_l3,sub_margin,sub_pace,sub_trip_fin,sub_trip_luck,sub_pos4_x_pace,sub_l3_x_pace,sub_pos1_rk,sub_pos4_rk,sub_l3_rk,sub_margin_rk,sub_trip_fin_rk,sub_trip_luck_rk
P=pos1_sd5,late_start_rate,jky_pos_bias,trn_pos_bias,jky_pos_bias_x
U=l3_adj_last,l3_adj_mean3,l3_adj_best5,l3_adj_pos_last,l3_adj_pos_mean3,l3_adj_pos_best5,l3_vs_race_last,l3_vs_race_mean3,l3_vs_race_best5,l3_adj_mean3_rk,l3_adj_best5_rk,l3_adj_pos_mean3_rk,l3_adj_pos_best5_rk
M=meet_front_adv,meet_inner_adv,meet_n,meet_front_fit,meet_inner_fit
N=net_abil,net_abil_oth,net_n,pr_last,pr_mean3,pr_best,pr_top2,pr_best_1y,pr_best_dist,pr_best_hi,pr_hi_n,h2h_margin,h2h_margin_n,net_abil_rk,pr_best_rk,pr_top2_rk,pr_mean3_rk
N2=pr_best_age,pr_peak_drop,combo_n,combo_pr_gap,lay_n,lay_fit,sec_fit,pr_best_oth,pr_oth_n,gap_net3,gap_prm3,gap_peak3,gap_top2_3,field_net_top3,field_known
B=bw_best_gap,bw_best_gap_abs,bw_win_gap,bw_vs_mean,bw_max_gap,bw_trend3,bw_rel_field
: > $L
step() { echo "=== $(date +%H:%M:%S) $*" >> $L; "$@" >> $L 2>&1 || { echo "FAILED: $*" >> $L; exit 1; }; }
if [ "$1" = "train" ]; then export FAST=0; else export FAST=1; fi
if [ "$1" != "score" ]; then
step python3 picks_ingest.py
for s in build_base features extra_features speed_level new_feats exp_position new_feats2 build_v5 new_feats3 sub_models new_feats4 new_feats5 new_feats6 new_feats7 new_feats11; do
  step python3 $s.py
done
fi
rm -f ../out/pick_*.parquet
step python3 exp_harness.py p1 $A,$T,$S,$P,$U,$M,$N
SEED=2 step python3 exp_harness.py p1_s2 $A,$T,$S,$P,$U,$M,$N
step python3 exp_harness.py p2 $A,$T,$S,$P,$U,$M,$N,$N2
SEED=2 step python3 exp_harness.py p2_s2 $A,$T,$S,$P,$U,$M,$N,$N2
step python3 exp_catboost.py $A,$T,$S,$P,$U pcat
step python3 picks_select.py
echo "DONE $(date +%H:%M:%S)" >> $L
