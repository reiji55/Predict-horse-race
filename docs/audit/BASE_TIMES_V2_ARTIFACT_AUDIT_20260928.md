# base_times v2 frozen artifact 監査 — `base-times-v2-coverage-20260928`

作成: Claude（2026-09-30）
設計: `docs/audit/SPEED_BASE_TIMES_V2_DESIGN_20260928.md` §14 PR B — frozen artifact
モード: **研究用 artifact の作成と凍結のみ。** Champion・予想経路・`config/base_times.json`・`logic/`・model registry・Challenger は変更していない。値の手修正もしていない。

## 1. 識別子とハッシュ

| 項目 | 値 |
|---|---|
| artifact_id | `base-times-v2-coverage-20260928` |
| lookup | `data/reference/base_times/base-times-v2-coverage-20260928.json` |
| meta | `data/reference/base_times/base-times-v2-coverage-20260928.meta.json` |
| cutoff_date | **2026-09-27**（forward 検証の登録 2026-09-28 より前で固定） |
| estimator | `v1-median-class-offset`（v1 と同じ式：`median(win_time − class_offset × dist/2000)`、小数1桁）、min_samples=5、全馬場 |
| lookup_sha256 | `01a376e12af58e8e3cd6a683835dce3a4af3500e45789a0ce4ea53c3ccffe86e` |
| records_sha256（build に使った 12,333 件） | `93ca31308e8c42d150994479acd5cccecbcbab1e1e6b99c0e224c15d50eb558d` |
| meta_content_sha256（built_at を除く） | `871ad517fd9ab8395c5c1313eee29e454d1c3346effd280b8611b6df9bda702e` |
| source file sha256 | `ee276002a450ca9ede839fb64f1bd364b32bb56c7f10d85f77fd636256434cc9` |
| source records_sha256（取得した 12,340 件） | `e8ecf8bae6f8d4fe4e0f176e875ea045957854cbb6a708c1718cafdd3d748a60` |

**再現性:** `scripts/audit_base_times_v2.py` でソースから作り直し、lookup・lookup_sha256・records_sha256・meta_content_sha256 の4つがすべて一致した。
**凍結:** 同じ artifact_id への再書き出しは、built_at だけが違っても拒否されることを確認した（ファイルのバイトは不変）。

## 2. 取得（controlled acquisition）

- 経路：既存の netkeiba レース検索（`scripts.build_base_times.fetch_course_records` → `scraper.common.http`）
  - 間隔 3 秒＋揺らぎ 0〜2 秒、UA 明示、リトライ 1 回
- 実行：一時 workflow（`temp_acquire_base_times_v2.yml`）で `python -m scripts.acquire_base_times_v2 --start-year 2023 --end-year 2026` を実行
  - 2026-09-29 19:56〜20:15 JST
  - 101コースすべてを取得し、完成版のファイルだけをコミットした（029f92e）
  - 一時 workflow はこの PR で削除済み
- 取得順：芝を最優先 → 手元 raw の需要が多い順
- 取得結果：12,340 行・101コース
  - 別コースの行の混入は 0 件
  - 0 件だったのは 2 コース：京都/ダ/1100、東京/ダ/2400。コース表側にある距離だが、期間内に施行が無かったと思われる
- 期間：2023-01-05〜2026-09-27
  - cutoff より後の記録は 0 件、日付の欠損も 0 件
- クラス内訳：mi 5,466 / 1win 3,395 / 2win 1,704 / 3win 786 / op 509 / g3 255 / g2 141 / g1 77
  - クラスが判定できなかった 7 件は推定の対象外（`skipped_by_estimator=7`）
- 馬場内訳：良 8,924 / 稍重 2,031 / 重 990 / 不良 395
- 芝ダ内訳：芝 6,228 / ダ 6,112

## 3. カバレッジ

| | 充足 | 未充足 |
|---|---|---|
| 芝 | **59 / 64**（v1 は 4/64） | 函館/芝/1000 (n=4)、東京/芝/2300 (n=4)、東京/芝/3400 (n=4)、中山/芝/3600 (n=3)、京都/芝/3200 (n=4) |
| ダ | **33 / 37**（v1 は 16/37） | 新潟/ダ/2500 (n=3)、中山/ダ/2500 (n=3)、東京/ダ/2400 (0件)、京都/ダ/1100 (0件) |
| 合計 | **92 / 101**（v1 は 20/101） | |

未充足はいずれも年に数回しか施行されない長距離・特殊距離で、min_samples=5 に届かないため lookup には載せていない（meta には `insufficient_samples` として残る）。

### 手元 raw（W38+W39）の需要に対するカバレッジ

| 表 | speed で使える走 520 本のうちカバー |
|---|---|
| Champion v1 | 220（未カバー 300） |
| **v2（この artifact）** | **519**（未カバーは 東京/芝/3400 の 1 本だけ） |

地方・海外・障害の 26 本は、speed の対象外なので需要に含めていない。

## 4. 重点コース

| コース | 基準タイム | n | MAD | IQR | 期間 |
|---|---|---|---|---|---|
| 東京/芝/1600 | 92.6 | 247 | 0.6 | 1.27 | 2023-01-28〜2026-06-21 |
| 東京/芝/1400 | 80.3 | 185 | 0.5 | 1.00 | 2023-01-28〜2026-06-21 |
| 阪神/芝/1600 | 92.8 | 175 | 0.7 | 1.35 | 2023-02-11〜2026-09-27 |
| 京都/芝/1600 | 92.8 | 204 | 0.8 | 1.70 | 2023-04-22〜2026-05-31 |
| 阪神/芝/1200 | 67.7 | 70 | 0.3 | 0.68 | 2023-02-26〜2026-09-21 |

- クラス内訳・馬場内訳はすべて、1クラスや1馬場への極端な偏りなし（詳細は audit JSON の `focus_courses`）。
- raw の需要上位12コースは、すべて n ≥ 109 で充足している。

## 5. サンプル数

- 充足コースの n：最小 5、中央値 111.5。
- n < 20 の薄いコース（要注意、9コース）：
  - 福島/ダ/2400 (5)
  - 京都/芝/3000 (8)、小倉/ダ/2400 (8)、札幌/ダ/2400 (8)、阪神/芝/3000 (8)
  - 東京/芝/2500 (9)、阪神/芝/2600 (9)
  - 函館/ダ/2400 (11)
  - 新潟/芝/2400 (15)
- いずれも長距離で raw の需要は小さい。PR C 以降で speed-v2 の結果を見るときは、これらのコース由来の指数は信頼度が低いものとして扱うこと。

## 6. 外れ値・異常値（閾値は事前固定。値は変えていない）

- **コース内の外れ値**（正規化後のロバスト z > 5）：4 件・4 コース

  | コース | 日付 | クラス | 馬場 | 勝ちタイム | 正規化 | コース中央値 | z |
  |---|---|---|---|---|---|---|---|
  | 中山/ダ/2500 | 2023-12-10 | 1win | 良 | 164.4 | 162.53 | 161.32 | 8.09 |
  | 函館/芝/1000 | 2025-06-14 | mi | 良 | 56.4 | 55.15 | 56.30 | −7.76 |
  | 東京/芝/1600 | 2024-06-23 | mi | 稍重 | 99.9 | 97.90 | 92.60 | 5.96 |
  | 阪神/芝/2000 | 2025-04-13 | op | 稍重 | 123.5 | 123.50 | 118.90 | 5.17 |

  - 中山/ダ/2500 と函館/芝/1000 は未充足（lookup に載っていない）ので、表への影響はない。
  - 東京/芝/1600（n=247）と阪神/芝/2000 は、中央値を使っているので1件の外れ値の影響はほぼ無い。
  - 除外や修正はしていない。
- **コース内のばらつきが大きい**（IQR / 距離 > 1.5 秒/1000m）：函館/芝/2600 の1コース（n=25、IQR 4.1 秒、MAD 2.1 秒）。長距離・洋芝で開催時期による差が大きいと考えられる。
- **コース間の整合**（芝ダ別に「距離→基準タイム」を直線近似し、残差 > 1 秒かつロバスト z > 3.5）：フラグ **0 件**。
  - 傾きは芝 6.50 秒/100m、ダ 6.82 秒/100m。
- **v1（Champion の表）との比較**：共通 20 コースのうち、差 > 0.5 秒は **0 件**。
  - 19 コースは完全一致、阪神/ダ/1800 だけ +0.1 秒。
  - v1 も同じ netkeiba 経路・同じ式で作られているため、整合している。

## 7. speed guard の回復見込み（retrospective・カバレッジのみ）

guard は現行のまま（same_surface_only=true、min_usable_runs=2、min_race_coverage=0.5）。

| レース | コース | Champion v1 | v2 |
|---|---|---|---|
| 9/19 阪神 | ダ1400 | 16/16 used | 16/16 used |
| 9/19 中山 | ダ1800 | 12/14 used | 12/14 used |
| 9/20 阪神 | 芝1200 | 4/16 off | **15/16 used** |
| 9/20 中山 | 芝2200 | 1/13 off | **13/13 used** |
| 9/26 阪神 | ダ2000 | 11/16 used | 14/16 used |
| 9/26 中山 | 芝1600 | 6/14 off | **14/14 used** |
| **9/27 阪神** | 芝1600 | 0/16 off | **15/16 used**（coverage 0.94） |
| **9/27 中山** | 芝1200 | 1/16 off | **16/16 used**（coverage 1.00） |

speed が通るレースは 3/8 → **8/8** になり、設計書 §1.3 の上限見積もりと一致した。

**注意（リーク）：**
- この表の期間は 9/27 まで（cutoff は forward 検証の開始前）なので、9/19〜9/27 のレース自身の勝ちタイムも標本に含まれている。
  - 例：阪神/芝/1600 の date_max は 2026-09-27 で、9/27 のポートアイランドSそのもの。
- したがって、上の表で意味を持つのは guard を通過する頭数（カバレッジ）だけ。
- 9/27 以前のレースをこの表で採点して性能を語ることはできない（設計書 §6）。
- 性能の検証は、PR C 以降の forward（9/28 以降のレース）で行う。

## 8. 未解決・レビューで見てほしい点

1. 未充足の 9 コース（§3）は lookup に載せていない。v1 と同じく、これらのコースを走った過去走は speed に使われない。
2. 薄い 9 コース（§5）の扱い：min_samples=5 は v1 と同じ値。変える場合は新しい artifact_id で作り直す（この artifact は変えない）。
3. コース表（`COURSES`）にある京都/ダ/1100・東京/ダ/2400 は、期間内の施行が 0 件だった。
4. 外れ値 4 件（§6）はそのまま残している（中央値なので影響は小さい）。
5. 取得に使った一時 workflow は削除済み。
   - 取得の記録はソースファイルに残っている：コースごとの `fetched_at`・`rows`・`records`・`dropped_mismatched_course`・`empty`。
   - 取得期間と HTTP の設定もソースファイルの `source` に入っている。

## 9. ファイル

- ソース：`data/reference/base_times/sources/netkeiba-race-search-2023-2026.records.json`
- artifact：`data/reference/base_times/base-times-v2-coverage-20260928.json` / `.meta.json`
- 報告：
  - `data/reference/base_times/reports/base-times-v2-coverage-20260928.coverage.json`（raw カバレッジ）
  - `data/reference/base_times/reports/base-times-v2-coverage-20260928.audit.json`（監査の全データ）
- 再現コマンド：

```
python -m scripts.base_times_v2 --source file \
  --input data/reference/base_times/sources/netkeiba-race-search-2023-2026.records.json \
  --cutoff 2026-09-27 --artifact-id base-times-v2-coverage-20260928          # dry-run（書かない）
python -m scripts.audit_base_times_v2 --artifact-id base-times-v2-coverage-20260928 \
  --source data/reference/base_times/sources/netkeiba-race-search-2023-2026.records.json \
  --output /tmp/audit.json                                                     # 再現性チェック付き
```
