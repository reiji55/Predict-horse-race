set -e
cd /home/claude/keiba_lab/src
python3 -c "
import pandas as pd
pd.concat([pd.read_parquet(f'../out/place_scores_{y}.parquet') for y in (2024, 2025, 2026)]).to_parquet('../out/place_scores_holdout.parquet')
print('concat ok')
"
echo '=== order (place models), holdout all'
python3 eval_order.py ../out/preds_all.parquet ../out/place_scores_holdout.parquet 2019,2020,2021,2022,2023 2024,2025,2026 place_holdout > ../out/order_place_holdout.txt 2>&1
echo '=== order (harville), holdout all'
python3 eval_order.py ../out/preds_all.parquet none 2019,2020,2021,2022,2023 2024,2025,2026 harville_holdout harville > ../out/order_harville_holdout.txt 2>&1
echo '=== fuku, holdout all'
python3 fuku_model.py ../out/preds_all.parquet ../out/place_scores_holdout.parquet 2024,2025,2026 holdout > ../out/fuku_holdout.txt 2>&1
echo '=== fuku, holdout 11R'
MAIN=1 python3 fuku_model.py ../out/preds_all.parquet ../out/place_scores_holdout.parquet 2024,2025,2026 holdout_11R > ../out/fuku_holdout_11R.txt 2>&1
echo done
