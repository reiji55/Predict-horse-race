# 市場だけで作る確率の基準線（market-baseline-v1、observe-only）

Issue #39 の PR-B。次の確率モデル（#39 案1：市場＋能力モデルの混合）が勝たなければならない「床」を、
モデルを一切使わずに発走前の市場オッズだけで作り、結果の出たレースで proper な指標を測る。
コード：`research/market_baseline.py`。設定：`config/market_baseline_v1.json`（固定値。ここでは当てはめない）。
出力：`data/research/market_baseline/`。予想・正式成績・UI・既存の shadow 採点には使わない。

## 入力（発走前に固定）

- raw は「発走前に snapshot を最後に更新したコミット」の版（`research/market_audit.py` の固定をそのまま使う）。
- 3連複・単勝ともオッズの時点が `pre_race_verified` のレースだけを使う。時点が `unknown` のレース（10/3・10/4 の4レース）や、発走前のコミットが無いレース（9月の9レース）は、発走前と読み替えずに除外し、理由を `summary.excluded` に残す。
- 単勝オッズの無い出走馬が1頭でもいるレースは、取消と確認できない限りレースごと除外する（`runner_without_win_odds_unconfirmed`）。その馬を外して残りで正規化すると、無条件の予想と別物になるため（PR #41 レビューの申し送り）。

## 作る分布（どれも「どの3頭が3着以内に入るか」の分布。全組で合計 1）

| 名前 | 作り方 |
|---|---|
| `market_trio` | 3連複オッズそのもの。収録されている組は `(1/odds)/Z`（`Z = 1/払戻率 = 1/0.75`）。収録されていない組（1000倍超など）には、残りの質量 `R = 1 − Σ` を Harville の形の比で配る（0 にしない）。未収録の組のオッズは収録の最大値以上なので、1組あたり `1/(最大オッズ×Z)` を上限にし、超えた分は他の未収録組に配り直す。`R` が負（払戻率の仮定より市場の合計が大きい）や上限超えのときは丸め、通った経路を `market_trio.method` に記録する |
| `harville` | 単勝オッズを正規化した勝率 `p` から、素の Harville（＝素の Plackett–Luce）で作る |
| `discounted_harville` | 2着・3着の選び方に `p^λ2`・`p^λ3` を使う補正版。`λ2 = 0.81`、`λ3 = 0.65` は文献でよく引用される値で、**このプロジェクトのデータでは推定していない**。当てはめは PR-C（walk-forward）の担当 |

Henery・Stern などの他の補正式は、モンテカルロの誤差の扱いと依存ライブラリ（numpy 無し）の都合で、この PR では入れていない。必要なら式・パラメータ・検証区間を決めて別途足す。

## 指標（結果が出たレースだけ）

- `trio_logloss`：実際の3着以内の組に付けた確率の −log。全組で正規化した分布なので proper。
- `top3_brier`：各馬の「3着以内に入る確率」（分布の周辺確率）の Brier の平均。
- `win_logloss_market`：単勝オッズを正規化した勝率での、勝ち馬の −log（参考）。
- `actual_trio_rank`・`top5_hit`：実際の組が確率の高い順で何番目か、上位5組に入っていたか（参考）。
- `paired_vs_market_trio`：各分布と `market_trio` の差を同じレースで対にした平均と標準誤差。**有意性は判定しない**（`significance: not_assessed`）。

## 未収録組の補い方への感度

- `market_trio` の未収録組への補い方は仮定（特に「未収録の組のオッズは収録の最大値以上」は収集仕様で証明されていない）。
- 各レースで、払戻率 0.725/0.75/0.775 × 配る形 harville/discounted_harville/uniform × 1組の上限あり/なし の 18 通りの `market_trio` を作り、指標を `sensitivity` に残す。主の補い方は 0.75・harville・上限あり。
- `summary.sensitivity.ordering_stable_across_variants` は、Harville 系との大小が 18 通りで入れ替わらないかを示す。入れ替わるなら、`market_trio` の優劣は補い方に依存している。

## 近似の扱い

- 払戻率 75%・市場の効率性という仮定に基づく近似。未収録組への配り方（Harville の形）も仮定。
- オッズは発走前の凍結時点（だいたい 12〜14 時台）のもので、確定オッズではない。学習・評価でこの時点を混ぜない。
- 期待値・優位性は、この基準線だけでは主張しない（PR-C 以降で、モデルとの混合を事前登録して前向きに比べる）。

## 実行

```
python -m research.market_baseline              # data/research/market_baseline/ に書く
python -m research.market_baseline --out DIR    # 別の場所に書く（確認用）
```

`run_results.yml` の成績集計の後に、observe-only のステップとして毎週実行する（失敗しても止めない）。
