cd /home/claude/keiba_lab/src
python3 exp_harness.py base none >> ../out/exp1.log 2>&1
python3 exp_harness.py trip tr_luck_last,tr_luck_mean3,tr_resid_last,tr_resid_mean3,tr_resid_best5 >> ../out/exp1.log 2>&1
python3 exp_harness.py allx tr_luck_last,tr_luck_mean3,tr_resid_last,tr_resid_mean3,tr_resid_best5,meet_day,away,dir_edge,dir_n,h2h_net_per,h2h_rate,h2h_meet >> ../out/exp1.log 2>&1
python3 exp_harness.py recent5 none recent5 >> ../out/exp1.log 2>&1
echo DONE >> ../out/exp1.log
