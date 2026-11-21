cd /home/claude/keiba_lab/src
L=../out/run_exp14.log
eval "$(grep -E '^(A|T|S|P|U|M|B|N|N2)=' run_picks.sh)"
G=gf_n,gf_gap,gf_best,gf_last,gf_last_age,gfd_best,gfd_n,wet_n,wet_gap,gf_best_rk
X=jky_hi_n,jky_hi_t3,jky_hi_edge,t3_streak,t3_streak_all,age_f,young_vs_old,older_share,win_same_days,win_surf_days,win_same_n
: > $L
python3 new_feats11.py > ../out/new_feats11.log 2>&1 || { echo FAILED11 >> $L; exit 1; }
mv ../out/pick_ids.txt ../out/pick_ids.hold
python3 exp_harness.py p3 $A,$T,$S,$P,$U,$M,$N,$N2,$G,$X >> $L 2>&1
SEED=2 python3 exp_harness.py p3_s2 $A,$T,$S,$P,$U,$M,$N,$N2,$G,$X >> $L 2>&1
python3 exp_harness.py p3g $A,$T,$S,$P,$U,$M,$N,$N2,$G >> $L 2>&1
SEED=2 python3 exp_harness.py p3g_s2 $A,$T,$S,$P,$U,$M,$N,$N2,$G >> $L 2>&1
mv ../out/pick_ids.hold ../out/pick_ids.txt
echo DONE >> $L
python3 cmp_exp.py p2,p2_s2 p3,p3_s2 >> $L 2>&1
python3 cmp_exp.py p2,p2_s2 p3g,p3g_s2 >> $L 2>&1
