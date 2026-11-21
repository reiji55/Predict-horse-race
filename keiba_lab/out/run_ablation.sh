cd /home/claude/keiba_lab/src
for spec in MKT+CTX MKT+CTX+RATING MKT+CTX+PASTMKT MKT+CTX+BODY MKT+CTX+CONN MKT+CTX+PACE MKT+CTX+FORM MKT+CTX+APT ALL-RATING; do
  python3 ablation.py $spec 2021,2022,2023 $spec
done
