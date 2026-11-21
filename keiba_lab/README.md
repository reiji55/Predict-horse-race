# keiba_lab — 市場に勝つ確率式の探索コード

JRA 平地 2010-01〜2026-10（55,738 レース）で、確定単勝オッズより正確な確率を出す式を探した研究コード。
結果のまとめは Docs のレポート「市場に勝つ確率式 探索レポート」を参照。

## データの用意
公開リポジトリ iinumac/keiba_core の `data/warehouse/`（netkeiba 由来の Parquet）を使う。

```bash
git clone --filter=blob:none --no-checkout https://github.com/iinumac/keiba_core
cd keiba_core && git sparse-checkout init --no-cone
printf '/data/warehouse/\n' > .git/info/sparse-checkout && git checkout HEAD
```
その後、races / results / payouts / odds を年ごとに結合して `data/{races,results,payouts,odds}.parquet` に置く
（`src/build_base.py` の冒頭が読む形）。

## 実行順
| 手順 | スクリプト | 内容 |
|---|---|---|
| 1 | `src/build_base.py` | 取消・除外を除き、1頭1行の表 `data/runs.parquet` を作る |
| 2 | `src/market_baseline.py` | 市場（確定単勝オッズ）の精度と人気薄バイアス |
| 3 | `src/market_combos.py` | 想定オッズ帯ごとの組み合わせ馬券の回収率（高配当の歪みの検証） |
| 4 | `src/features.py` | 発走前に分かる108の材料（収束する能力値＝カルマン型レーティングを含む）。約15分 |
| 5 | `src/models.py` | 開発期間 2019〜2023 の年単位ウォークフォワード（市場・一から予想・Benter型・ズレ学習） |
| 6 | `src/holdout.py` | 固定した式で 2024〜2026 を一度だけ予測（PREREGISTRATION.md） |
| 7 | `src/place_models.py` | 2着・3着の専用モデル（同じくズレ学習） |
| 8 | `src/eval_order.py` / `src/fuku_model.py` / `src/win_roi.py` | 組み合わせ・複勝・単勝の評価（実際の払戻で計算） |

`MAIN=1` を付けると各評価を 11R だけに絞る。

### オッズを使わない式（後半の作業）
| 手順 | スクリプト | 内容 |
|---|---|---|
| 9 | `src/extra_features.py` / `src/speed_level.py` / `src/more_features.py` | 材料の追加（v2・タイム指数とレースレベルの v3・効かなかった v4） |
| 10 | `src/pure_models.py` | オッズ抜きの3段の式（1・2・3着）。`FEAT_FILE` で材料の版を選ぶ |
| 11 | `src/pure_eval.py` / `src/box_eval.py` / `src/rank_eval.py` | 3着内確率の較正、上位K頭ボックスの的中・回収率 |
| 12 | `src/formula2.py` | 係数を読める20材料の式 |
| 13 | `src/pace_value.py` / `src/divergence.py` | 位置取り・ペースの情報の価値、市場と違う見方の検証 |
| 14 | `src/scenario.py` | 展開別の式（前残り・差し決着）と複数の式のボックス |
| 15 | `src/shape_predict.py` | 決着の形の事前予想と、2つの式を組み合わせたワイド |
| 16 | `src/anaba.py` | 穴専用の式（本命の式で6位以下の馬だけで学習） |
| 17 | `src/new_feats.py` / `src/new_feats2.py` | 展開補正つき過去走・開催日次・輸送・左右回り・直接対決／騎手の当日・休み明け何戦目 |
| 18 | `src/residual_mining.py` | 逆算：式の見立てと実際のずれが偏る条件（2023で発見→2024–26で確認） |
| 19 | `src/exp_position.py` | 位置取り（1角・4角）の事前予測（年ごとの抜き取り） |
| 20 | `src/exp_harness.py` / `src/exp_margin.py` | 材料の効き目を同じ物差しで比較（SEED・SPLIT・重み付け）、着差の回帰 |
| 21 | `src/build_v5.py` | 効いた材料を本命の3段の式に入れる features_v5 |
| 22 | `src/new_feats3.py` / `src/sub_models.py` | テンの近似値、部分ごとの式（位置・上がり・着差・ペース）と代入 |
| 23 | `src/exp_mixture.py` | ペース・位置で条件づけた式（上限の測定、事前予測の代入） |
| 24 | `src/new_feats4.py` / `src/new_feats5.py` / `src/new_feats6.py` | 出遅れ癖・騎手の位置取り傾向／上がりの指数／開催を通した馬場の傾向 |
| 25 | `src/exp_catboost.py` | CatBoost（騎手・調教師・馬主をカテゴリのまま、has_time） |
| 26 | `src/new_feats7.py` | 馬体重（好走時との差など） |
| 27 | `src/residual_mining2.py` | 最良の式の外れ方の分析（2024で発見→2025–26で確認） |
| 28 | `src/formula3.py` / `share/three_cards_formula_guide.md` | 予想チャット用の手で使える式とガイド |
| 29 | `src/new_feats8.py` | JRAの含水率・クッション値（アプリの data/track/jra_track_archive.csv を data/ に置く） |
| 30 | `src/new_feats9.py` | 調教評価（記号・短評）と血統（父・母父）の材料。アプリの training_eval.csv・pedigree.csv を data/ に置く |
| 31 | `src/oof_base.py` / `src/stage2_11r.py` | 答えを見ない基礎スコア（年ごとの抜き取り）と、メインレースでの調教評価・血統の検証 |
| 32 | `src/mkt_top3.py` | 市場が見た3着内確率（学習の先生役の検証用。exp_harness の SOFT=λ） |
| 33 | `src/fermi.py` / `src/fermi_hand.py` / `src/fermi_11r.py` | 重要指数の判定、フェルミ式（基準オッズ×倍率）、手で使える版、メインレースの年次検証 |
| 34 | `src/ceiling.py` | 再戦の一致率から「強さを完全に知っていたときの当たり方」を概算 |
| 35 | `src/fermi_fixed.py` | 指数を固定してフェルミ式を合わせ直す（`six`＝6項目版、`full13`＋`DROP1=1`＝1つ抜く検証・条件別） |
| 36 | `src/trio_lib.py` / `src/trio_eval.py` | 3着内確率から3連複の同時確率（Plackett–Luce）を作り、確率の高い順5点の的中率と確率の合い方を測る |
| 37 | `src/fermi_11r_temp.py` | メインレースの過信の補正（オッズを0.79乗） |
| 38 | `src/today.py` | 今週のレースの13項目の入力づくり（アプリの raw JSON＋データベース＋調教評価）。`--check RACE_ID` で一致確認 |
| 39 | `src/chappy_checks.py` / `share/chappy_review_2026-11-14.md` | 本命の式への上乗せ・5点の作り方の比較と、チャッピーの指摘へのレビュー |


## 主な式
- 能力値：r ← r + K(y − r − b)、K = v/(v+σ²)、v ← (1−K)v、休養で v が増える（features.py）
- 勝率：p_i ∝ exp(θ·log π_i + f(x_i))。f は LightGBM、レースごとの softmax を目的関数にした自作の学習（models_lib.py）
- 2着・3着：残りの馬の中で同じ形の softmax（place_models.py）

## 注意
- オッズは確定オッズ。発走前に買う場合は優位が縮む可能性が高い。
- 組み合わせ馬券のオッズ表は無い（当たった組み合わせの払戻だけ）。
