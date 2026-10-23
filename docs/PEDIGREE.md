# 血統（父・母・母父）の取得と蓄積

予想ロジック（`logic/`）は**まだ血統を使わない**。いまは取得と蓄積だけを始める。
十分にたまったら、オッズを使わない3着内確率の式に「父・母父ごとの芝ダ・距離の成績」を足して効くかを検証する。

## 最初にやること：並びの確認（これが通るまで何も保存しない）

C2（出馬表の「過去5走」ページ）の `td.Horse_Info` の中で、父・母・母父がどこにあるかは推定で読んでいる
（`.Horse01`＝父、`.Horse03`＝母、`.Horse04`＝（母父））。推定のまま保存すると、別の項目を血統として
永久に残してしまうので、**実ページで確かめるまで、毎週の保存も過去レースの取り込みも止まっている**。

確かめ方：GitHub の Actions 画面で「**血統の並びの確認（手動）**」（`manual_pedigree_verify.yml`）を実行する。

- 血統がよく知られた4頭（イクイノックス、エフフォーリア、ソングライン、タスティエーラ）が出たレースの
  過去5走ページを取り（4ページ・約15秒）、読めた父・母・母父を正しい値と照らし合わせる
- 4頭すべて3項目が一致したときだけ `data/pedigree/c2_layout_check.json` が `"verified": true` になり、コミットされる
- 一致しなければ false のまま。同じファイルに Horse_Info の中身の並び（クラス名と文字）が残るので、
  それを見て `scraper/fetchers/pedigree.py` の `C2_SELECTORS` を直し、もう一度実行する
- 結果の表は実行画面の Summary に出る
- `C2_SELECTORS` を変えると、記録されたセレクタと合わなくなるので確認はやり直しになる

この workflow は PR でも動く（`pedigree.py` などを変えたとき）。PR では結果を表示・保存するだけでコミットはしない。
新しい workflow はマージ後でないと Actions 画面に出ないので、手動の実行はマージ後に1回行う。

HTML そのものは保存しない（netkeiba の著作物。リポジトリは公開）。

## 保存の約束

`data/pedigree/pedigree.csv`（1頭1行）

| 列 | 中身 |
|---|---|
| horse_ref | netkeibaの馬ID（10桁） |
| sire / sire_ref | 父の名前 / 馬ID |
| dam / dam_ref | 母の名前 / 馬ID |
| damsire / damsire_ref | 母父の名前 / 馬ID |
| source | `shutuba_past`（毎週）/ `shutuba_past_backfill`（過去レース）/ `ped_page`（血統表ページ） |
| fetched_at | 取得日時 |

- **父・母・母父の3つがそろった馬だけ**保存する。1つでも欠けたら保存せず、次の機会に取り直す
  （欠けたまま「取得済み」にしない）。raw の entry にも、そろった血統だけを入れる
- 一度入った馬は書き換えない（血統は変わらない）
- 別の取得で違う値が来たら、上書きも無視もせず `data/pedigree/conflicts.csv` に両方を残す
  （全角半角・空白の違いは同じとみなす）
- 書き込みは一時ファイル経由で置き換えるので、途中で落ちても壊れたCSVは残らない

`data/reference/` ではなく `data/pedigree/` に置く。予想経路が凍結表を読む入口を `logic/model_registry.py` の
1か所に限るテスト（`test_prediction_path_reads_v2_artifacts_only_through_the_registry`）を守るため。

## 経路1：毎週（追加のアクセスなし）

C2 は毎週取っているので、同じHTMLから読むだけ。`build_raw` の最後に `pedigree.csv` に追記する。
並びが未確認の間は、血統を読まず、何も書かない（ログに「未確認のため保存しません」と出る）。

## 経路2：過去のメインレース（GitHub Actions・手動）

「**血統の取り込み（過去レース・手動）**」（`manual_pedigree_backfill.yml`）を実行する（入力：`limit`＝レース数）。
並びが未確認なら、何も取りに行かずに止まる。

- 対象：`data/pedigree/backfill_race_ids_11R.txt`（2014年以降のJRA平地メインレース 3,656レース、新しい順）
- 1レース約4秒（`scraper/common/http.py` の間隔をそのまま守る）。200レースで約15分、全部で約4時間
- レースごとの結果を `data/pedigree/backfill_status.csv` に残す

| status | 意味 | 次回 |
|---|---|---|
| done | 出走馬全員の父・母・母父がそろった | 取りに行かない |
| retry | 取得失敗・表が無い・1頭も取れない・一部欠け（`note` に理由、`attempts` に回数） | もう一度取りに行く |
| unavailable | 3回やっても表が無い／取得できない／1頭も取れない | 取りに行かない |
| partial_final | 3回やっても一部の馬が欠けたまま（そろった馬は保存済み） | 取りに行かない |

列：`race_id, status, n_runners, n_complete, n_missing, attempts, last_tried, note`

止まる条件（そのレースの結果は記録しない＝あとで同じレースから取り直せる）：

- 今回の最初の5レースで、そろった血統が1頭も無い
- そのあと、そろった血統が1頭も無いレースが10回続いた

1日に数回ずつ、数日に分けて回す想定。netkeibaへの負荷を考え、連続で何回も起動しない。
土日の日中（10時〜20時ごろ、週次の取り込み・オッズ・結果の workflow が動く時間）は避ける。
同じ時間に両方が `pedigree.csv` を書き換えると、後から push する側が rebase で止まることがある
（止まった回の分は記録されないので、次の実行で取り直される）。

## 経路3：馬ごとの5代血統表（手元のPC・Colab）

`scripts/backfill_pedigree.py` が `db.netkeiba.com/horse/ped/{馬ID}/` を取る。
`db.netkeiba.com/horse/` は GitHub Actions からだと bot 判定されるので、**Actions では動かさない**。
1頭約4秒（1,000頭で約70分）。経路2で取れない馬（メインレースに出ていない馬）を埋めるときに使う。

```
python -m scripts.backfill_pedigree --ids horse_ids.txt --limit 500
```

5代血統表は、いちばん大きい rowspan がちょうど2つ（父・母）、その半分がちょうど4つ（父父・父母・母父・母母）
のときだけ読む。形が違えば保存しない。
