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

1. **入力の契約**（§3）：レースごとに、発走前の固定、オッズの時点、オッズの無い馬、3連複オッズを確かめる。結果が無ければ `pending_result`。結果があれば、p_model の契約、同着、3着内の馬のオッズを確かめる。
2. **当てはめ**（§4・§5）：登録後のレースのある開催日ごとに、その前の開催日の契約を満たすレース（登録前を含む）で a・b、続けて w を当てはめる。
   - 18 通りの補い方ごとに w を当てはめ直す（`w_variants`）。
   - 副の p_model（race-performance-v1）は主の補い方だけで同じ手順を回す。
3. **評価**（§6.1）：その開催日のレースで、主指標 `D = LL(T2) − LL(market_trio)`、副指標、18 通りの `D` を出す。
4. **区間と判定**（§6.2〜6.4）：
   - 発走前の契約を満たす登録後のレース（結果待ちを含む）の累計で、30・60・100 の区間を開催日単位で切る。
   - 30・60 は報告だけ。
   - 100 または 2027-06-30 の区間は、結果を 14 日待ってから判定する。
   - 最初に確定した判定は `decision.json` に固定し、以後は再計算と一致するかだけを見る（`decision_recomputed_matches_fixed`）。

### 計算量の工夫

訓練レースは `race_id` の昇順なので、ある開催日の訓練は前の開催日の訓練の続きになる。そこで、レースごとに (a, b) の格子全点の損失を一度だけ作り、順に足し上げる。足す順番は毎回最初から足すのと同じなので、結果も同じになる。

## 出力

| ファイル | 中身 |
|---|---|
| `registration.json` | §1 を Git から導出した記録 |
| `races/{race_id}.json` | 契約の判定、固定した版、p_model の出どころ（blob SHA・frozen_at・model_id・config_hash・capture の確認）、評価（a・b・w・各 log loss・D・18 通りの D・副の p_model） |
| `days.json` | 開催日ごとの訓練レースの一覧、a・b・目的関数・同点の数、w と 18 通りの w（主・副） |
| `summary.json` | 状態（`collecting`・`checkpoint_30`・`checkpoint_60`・`awaiting_results`・`adopted`・`not_adopted`・`undecidable`・`spec_changed`）、除外の理由、区間ごとの集計と bootstrap、これまでの集計（報告だけ）、Champion の区間 |
| `decision.json` | 確定した判定（1回だけ書く） |
| `p_model_source_changes.json` | p_model の個票（blob）が前回の出力から変わったレースの記録（追記だけ）。変わった馬・差の最大値・今の capture の確認結果 |

## 実行

```
python -m research.market_mix                      # data/research/market_mix/ に書く
python -m research.market_mix --out DIR            # 別の場所に書く（確認用）
python -m research.market_mix --as-of 2026-11-20   # 判定の日付を指定する（JST）
```

`run_results.yml` では、その回の生成物（結果・p_model）をコミットした後に走らせる。結果・p_model をコミット済みの版から読むため。

## 読むときの注意

- 途中の数値（`to_date`・チェックポイント）は報告だけで、採否の根拠にしない。採否は §6 の判定の時点でだけ決まる。
- `market_trio` の未収録組は推定で補った近似で、市場の真の確率ではない。モデルの優位性や回収率はここから主張しない。
