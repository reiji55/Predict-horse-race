# 血統（父・母・母父）の取得と蓄積

予想ロジック（`logic/`）は**まだ血統を使わない**。いまは取得と蓄積だけを始める。
十分にたまったら、オッズを使わない3着内確率の式に「父・母父ごとの芝ダ・距離の成績」を足して効くかを検証する。

## 保存先

`data/pedigree/pedigree.csv`（1頭1行。血統は変わらないので、一度入った馬は書き換えない）

| 列 | 中身 |
|---|---|
| horse_ref | netkeibaの馬ID（10桁） |
| sire / sire_ref | 父の名前 / 馬ID |
| dam / dam_ref | 母の名前 / 馬ID |
| damsire / damsire_ref | 母父の名前 / 馬ID |
| source | `shutuba_past`（毎週）/ `shutuba_past_backfill`（過去レース）/ `ped_page`（血統表ページ） |
| fetched_at | 取得日時 |

`data/reference/` ではなく `data/pedigree/` に置く。予想経路が凍結表を読む入口を `logic/model_registry.py` の
1か所に限るテスト（`test_prediction_path_reads_v2_artifacts_only_through_the_registry`）を守るため。

## 経路1：毎週（追加のアクセスなし）

C2（出馬表の「過去5走」ページ）の `td.Horse_Info` から読む。C2は毎週取っているので、アクセスは増えない。
`build_raw` が raw の各 entry に `pedigree` を入れ、最後に `pedigree.csv` に追記する。

**【要確認】** Horse_Info の中の並びは推定で読んでいる：`.Horse01`＝父、`.Horse03`＝母、`.Horse04`＝（母父）。

確かめ方（iPad/Safari、tests/samples/README.md と同じ手順）：

1. `https://race.netkeiba.com/race/shutuba_past.html?race_id=202609040511` を開いて Webアーカイブで保存する
2. `tests/samples/shutuba_past_hanshin11.html` として置く
3. `python3 tests/test_pedigree.py` を実行する。`test_real_sample_if_present` が OK なら並びは合っている
4. 合っていなければ、`scraper/fetchers/pedigree.py` の `parse_horse_info_pedigree` のクラス名を直す

## 経路2：過去のメインレース（GitHub Actions・手動）

`.github/workflows/manual_pedigree_backfill.yml` を Actions 画面から手動実行する（入力：`limit`＝レース数）。

- 対象：`data/pedigree/backfill_race_ids_11R.txt`（2014年以降のJRA平地メインレース 3,656レース、新しい順）
- 1レース約4秒（`scraper/common/http.py` の間隔をそのまま守る）。200レースで約15分、全部で約4時間
- 済んだレースは `data/pedigree/backfill_done_race_ids.txt` に記録し、次の実行で飛ばす
- 最初の5レースで血統が1頭も取れなければ止まる（並びの推定違い・古いレースにページが無い、のどちらか）

1日に数回ずつ、数日に分けて回す想定。netkeibaへの負荷を考え、連続で何回も起動しない。
土日の日中（10時〜20時ごろ、週次の取り込み・オッズ・結果の workflow が動く時間）は避ける。
同じ時間に両方が `pedigree.csv` を書き換えると、後から push する側が rebase で止まることがある。

## 経路3：馬ごとの5代血統表（手元のPC・Colab）

`scripts/backfill_pedigree.py` が `db.netkeiba.com/horse/ped/{馬ID}/` を取る。
`db.netkeiba.com/horse/` は GitHub Actions からだと bot 判定されるので、**Actions では動かさない**。
1頭約4秒（1,000頭で約70分）。経路2で取れない馬（メインレースに出ていない馬）を埋めるときに使う。

```
python -m scripts.backfill_pedigree --ids horse_ids.txt --limit 500
```

5代血統表のパーサーは実ページの構造で確認済み（父・母が rowspan 最大、その次の世代の3番目が母父）。
