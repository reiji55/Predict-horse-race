cd /home/claude/keiba_lab/src
L=../out/run_exp13.log
eval "$(grep -E '^(A|T|S|P|U|M|B|N|N2)=' run_picks.sh)"
W=horse_weight,weight_change,horse_weight_rel,horse_weight_rk,day_style_adv,day_gate_adv,day_n,$B
: > $L
mv ../out/pick_ids.txt ../out/pick_ids.hold
DROP=$W python3 exp_harness.py pre2 $A,$T,$S,$P,$U,$M,$N,$N2 >> $L 2>&1
DROP=$W SEED=2 python3 exp_harness.py pre2_s2 $A,$T,$S,$P,$U,$M,$N,$N2 >> $L 2>&1
mv ../out/pick_ids.hold ../out/pick_ids.txt
echo DONE >> $L
python3 cmp_exp.py net2,net2_s2 pre2,pre2_s2 >> $L 2>&1
