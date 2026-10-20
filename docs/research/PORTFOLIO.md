# 3連複ポートフォリオ層（portfolio-v1、observe-only）

Issue #39 の PR-D（#38 の「軸不在の代替展開」と「5点の集合確率」をここに統合）。
コード：`research/portfolio.py`。設定：`config/portfolio_v1.json`。出力：`data/research/portfolio/`。
予想・正式成績・UI・Champion・scenario-frame/v1 の記録は変えない。**事前登録ではない**（前向きの採否は PR-E で別に登録する）。

## 何をするか

同じ確率（engine）の上で、3連複 100 円 × 5 点の組み方を、制約あり／なしで比べる。

| 組み方 | 制約 |
|---|---|
| `top_prob` | なし。確率の和が最大の5組（#39 のチャット参考券と同じ考え方） |
| `x_out_2` | 軸 X を含まない組を最低2点（#38 の候補ルール） |
| `exposure_cap_3` | 1頭が入る組を最大3点（5点の60%。#38 の試験値で、利益の根拠ではない） |
| `cap3_x_out_2` | 上の2つを両方。5点のときは露出上限3で X 抜きが必ず2点以上になるので、`exposure_cap_3` と同じ組になる（確認のために残す） |

- **X**：engine の3着内周辺確率が最大の馬（同点は馬番の小さい方）。
- **選び方**：制約を満たす5組のうち、確率の和が最大のもの。全組からの深さ優先探索で、確率の和の上界で枝刈りする（上界は制約を無視した値なので、最適解は落とさない）。探索の上限（200万ノード）に達したら `proven_optimal: false` を残す。
- 5組を選べなければ、無理に埋めずに `infeasible`（`constraints_not_satisfiable`）とする。
- **比較**：scenario-frame/v1 の記録の券も、同じ確率で同じ値を測る。記録は読むだけで、正式な有効性は `research.chat_experiment` の判定（`official_v1_valid`）を写す。
  - v1 の記録は 12:30 前後の版のオッズで作られている。ここでは、どの組み方も発走前に固定した最終版の確率で測る（同じ確率で比べるため）。

## engine（確率）

- 今は市場だけの分布：`research/market_baseline.py` の `market_trio`（払戻率 0.75・配る形 `harville_trio(p_mkt)`・1組の上限あり）。
- market-mix-v1 は採用が決まっていないので使わない（採用されたら、PR-D 以降の shadow とチャット参考券の engine にしてよい、と事前登録にある）。
- 入力は発走前に固定した版（`research/market_audit.py` の固定）。オッズの時点が `pre_race_verified` でないレース、単勝オッズの無い馬がいるレースは除外する（PR-B と同じ契約）。

## 測る値

発走前（すべてのレース）：

| 値 | 意味 |
|---|---|
| `p_hit` | 的中確率。別々の組は同時に当たらないので、確率の和 |
| `p_hit_given_x_in` / `p_hit_given_x_out` | X が3着内のとき／外れたときの条件付き的中確率 |
| `tickets_without_x`・`max_exposure` | X 抜きの組の数、最も多くの組に入っている馬とその数 |
| `tickets[].branch` | `primary_X_in`（X を含む）／`alternate_X_out`（X を含まない） |
| `p_hit_variants` | 選んだ組を固定したまま、未収録組の補い方 18 通りで測った的中確率の幅 |
| `expected_return_market` | 収録オッズで測った期待回収。未収録の組を含むときは出さない |

- **`expected_return_market` は優位性の指標ではない。** 市場の確率では、収録組の `p × オッズ` が組によらずほぼ一定（≒払戻率）になる。そのため、どの組み方でも約 0.75 になる。期待値で組み方を選んだり見送りを決めたりするには、市場より確かな確率（モデル）が要る。
- 展開（ペース・脚質）のデータは無いので、全レース `pace: pace_unknown`。存在しない展開予測は作らない。

発走後（結果のあるレースだけ）：

- `realized`：的中と払戻（`race_results.json` の 3連複の配当）。**反実仮想**で、正式成績・ROI には入れない。
- 3連複の配当は外れ値に強く支配されるので、少ないレース数での合計払戻は比較に使わない（確率の指標で比べる）。

## 集計（`summary.json`）

- `strategies`：組み方ごとの平均（`p_hit`・条件付き的中確率・X 抜きの数・露出・期待回収）と、反実仮想の的中・払戻。
- `constraint_cost_vs_top_prob`：制約で `p_hit` がどれだけ下がり、`p_hit_given_x_out` がどれだけ上がるか（同じレースで対にした平均）。
- `scenario_frame_v1`：全記録と、正式に有効な記録だけの、同じ値。
- `infeasible`・`not_proven_optimal`・`excluded`。

## 実行

```
python -m research.portfolio              # data/research/portfolio/ に書く
python -m research.portfolio --out DIR    # 別の場所に書く（確認用）
```

`run_results.yml` の成績集計の後に、observe-only のステップとして毎週実行する（失敗しても止めない）。

## 次（PR-E）への申し送り

- 前向きの shadow では、engine・組み方・制約・見送りの規則・判定の指標とレース数を、最初の評価レースの前に事前登録する。ここでの過去レースの数値は採否の根拠にしない。
- 見送り（pass）は、市場より確かな確率で期待値を測れるようになってから規則にする。市場だけの engine では、組み方による期待回収の差は出ない。
