# market-mix-v1 の実装（#39 PR-C2、observe-only）

仕様は `docs/research/MARKET_MIX_PREREG_V1.md` と `config/market_mix_v1.json`（登録コミットで凍結）。
この文書は実装の読み方だけを書く。仕様と食い違ったら仕様が正しく、実装の誤りとして直す（仕様は変えない）。
コード：`research/market_mix.py`。出力：`data/research/market_mix/`。予想・正式成績・UI・Champion には使わない。

## 登録（§1）

- `main` の first-parent 履歴で `config/market_mix_v1.json` が初めて現れたコミットを登録コミットにする。
  今回は PR #42 のマージコミット `30ad25b`（2026-10-19 23:29:43 JST）。
- 毎回 Git から導出して `registration.json` に書く：
  - 登録日時
  - 文書と設定の SHA-256（登録コミットのバイト列）
  - 参照コードの blob（登録コミットの版と今の版）
- 今の文書・設定が登録コミットとバイト単位で違えば、`summary.status = spec_changed` で評価を出さない。
- 参照コード（`research/market_audit.py`・`research/market_baseline.py`）は、登録コミットの版を Git から読み込んで使う。後でこれらのファイルが変わっても、v1 の計算は変わらない。

## 計算の流れ

1. **入力の契約**（§3）：レースごとに、発走前の固定、オッズの時点、オッズの無い馬、3連複オッズ、p_model の契約を確かめる。続けて、結果があれば同着と3着内の馬のオッズを確かめる。
   - p_model の契約（§3.5）は発走前の契約なので、結果の有無と切り離して確かめる（`eligibility`：`verified`・`failed`・`unverified`）。
   - p_model の個票は結果の取り込み後に作られるので、結果待ちで個票がまだ無いレースは `unverified`（数えない）。
   - `unverified` のまま開催日から 14 日たったレースは、確かめられないものとして数えない（`eligibility_unverifiable`）。
2. **当てはめ**（§4・§5）：登録後のレースのある開催日ごとに、その前の開催日の契約を満たすレース（登録前を含む）で a・b、続けて w を当てはめる。
   - 18 通りの補い方ごとに w を当てはめ直す（`w_variants`）。
   - 副の p_model（race-performance-v1）は主の補い方だけで同じ手順を回す。
3. **評価**（§6.1）：その開催日のレースで、主指標 `D = LL(T2) − LL(market_trio)`、副指標、18 通りの `D` を出す。
4. **区間と判定**（§6.2〜6.4）：
   - 発走前の契約を確かめた登録後のレース（`verified`。結果待ちを含む）の累計で、30・60・100 の区間を開催日単位で切る。
   - `unverified` のレースのある開催日から先では、区間の境界を決めない（`resolved_through` までで数える）。境界は、個票や結果の届く順番によらず、すべてを確かめたときと同じ開催日になる。期限（2027-06-30）での判定も、それまでの開催日がすべて確かめられるまで待つ（`awaiting_eligibility`）。
   - 30・60 は報告だけ。
   - 100 または 2027-06-30 の区間は、結果を 14 日待ってから判定する。
   - 最初に確定した判定は `decision.json` に固定し、上書きしない。中身は次のとおり。
     - 入力：区間のレースの一覧と、各レースの状態・固定した raw のコミット・p_model の blob・使った結果
     - 出力：判定と区間の全集計
     - それぞれの digest
   - 以後は毎回同じ記録を計算し直して digest を比べ、`decision_check` に出す（`matches`・`decision_input_changed`・`decision_output_changed`・`status_changed`・`changed_races`）。判定の状態が同じでも、平均・bootstrap・感度・評価レースの一覧のどれかが変われば不一致になる。

### 計算量の工夫

訓練レースは `race_id` の昇順なので、ある開催日の訓練は前の開催日の訓練の続きになる。そこで、レースごとに (a, b) の格子全点の損失を一度だけ作り、順に足し上げる。足す順番は毎回最初から足すのと同じなので、結果も同じになる。

## 出力

| ファイル | 中身 |
|---|---|
| `registration.json` | §1 を Git から導出した記録 |
| `races/{race_id}.json` | 契約の判定、固定した版、p_model の出どころ（blob SHA・frozen_at・model_id・config_hash・capture の確認）、評価（a・b・w・各 log loss・D・18 通りの D・副の p_model） |
| `days.json` | 開催日ごとの訓練レースの一覧、a・b・目的関数・同点の数、w と 18 通りの w（主・副） |
| `summary.json` | 状態（`collecting`・`checkpoint_30`・`checkpoint_60`・`awaiting_results`・`awaiting_eligibility`・`adopted`・`not_adopted`・`undecidable`・`spec_changed`）、`warnings`、未確認のレース、除外の理由、区間ごとの集計と bootstrap、これまでの集計（報告だけ）、Champion の区間、`decision_check` |
| `decision.json` | 確定した判定（入力・出力・digest。1回だけ書き、上書きしない） |
| `p_model_source_changes.json` | p_model の個票（blob）が前回の出力から変わったレースの記録（追記だけ）。変わった馬・差の最大値・今の capture の確認結果 |

## 実行

```
python -m research.market_mix                      # data/research/market_mix/ に書く
python -m research.market_mix --out DIR            # 別の場所に書く（確認用）
python -m research.market_mix --as-of 2026-11-20   # 判定の日付だけを変えた計算（下記）
```

- `--as-of` は、今の入力（HEAD の結果・p_model）のまま判定の日付だけを変えた計算（`as_of_mode: simulation_on_current_inputs`）。その日時点に存在した入力の再生ではない。`decision.json` と p_model の変化の記録は書かない。
- 終了コードは次のどれかで 1 になる（`summary.warnings` に理由が出る）。
  - 登録の不一致（`spec_changed`・`registration_not_found`）
  - 確定した判定との不一致（`decision_recomputed_differs_from_fixed`）
  - 判定の区間内の p_model の変化（`p_model_source_changed_in_decision_window`）
  - レースの失敗（`race_errors`）
- `run_results.yml` では、その回の生成物（結果・p_model）をコミットした後に走らせる。結果・p_model をコミット済みの版から読むため。
  - 出力はコミットしてから終了コードを返す。失敗しても成績集計は止めない（continue-on-error）。
  - 失敗したら、次のステップが警告（annotation とジョブの summary）を出し、成功と見誤らないようにする。

## 読むときの注意

- 途中の数値（`to_date`・チェックポイント）は報告だけで、採否の根拠にしない。採否は §6 の判定の時点でだけ決まる。
- `market_trio` の未収録組は推定で補った近似で、市場の真の確率ではない。モデルの優位性や回収率はここから主張しない。
