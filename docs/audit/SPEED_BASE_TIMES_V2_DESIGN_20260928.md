# 芝Speed / base_times_v2 設計 — 2026-09-28

作成: ChatGPT  
モード: **設計のみ。Championは変更しない。**  
背景: 9/27監査、PR #16 Forward Diagnostics、PR #17 app-wide assessment review。

## 0. 結論

現状のspeed問題は、まず「speed式が悪い」以前に **reference table が不足していて、設計どおりspeedが起動していない** 問題として扱う。

最初の実験では変更点を1つに限定する。

> **speed-v2 coverage Challenger = 現行のspeed式・weights・guardを完全に据え置き、基準タイム表だけを十分なcoverageへ置換する。**

この段階では speed index formula / class_offset / going_adj / k_impost / recency_weights / best-avg 0.6-0.4 / guard / cards weights / λ / probability temperature / ticket templates / Chappy / Otori を変えない。

これにより「speedが存在すること自体に価値があるか」を原因分離して測る。

その後、十分なhistorical dataを得てから **speed-v2.1 calibration** として class/going/base_time推定方法を学習化する。

## 1. Current state audit

### 1.1 現行 base_times coverage

scripts/build_base_times.py::COURSES はJRA 101コース:
- 芝: 64
- ダート: 37
- 合計: 101

current config/base_times.json:
- 芝: **4 / 64 = 6.25%**
- ダート: **16 / 37 = 43.24%**
- 合計: **20 / 101 = 19.80%**
- 芝4コースはすべて中山（1200/1600/1800/2000）

つまり「芝speedの精度が低い」というより、多くの芝過去走で **speed indexを計算する入口自体が無い**。

### 1.2 current raw demand

raw/2026-W38.json + raw/2026-W39.json の past_runs:
- venue/surface/dist がある走: **546**
- distinct course keys: **93**
- current base_times に無い走: **326（59.7%）**
- 未登録course keys: **73**

未登録需要上位:
1. 東京/芝/1600 — 20走
2. 東京/芝/1400 — 19走
3. 阪神/芝/1600 — 19走
4. 京都/芝/1600 — 17走
5. 阪神/芝/1200 — 13走
6. 東京/ダ/2100 — 11走
7. 京都/芝/1200 — 11走
8. 中京/芝/1200 — 11走
9. 函館/芝/1200 — 10走
10. 京都/芝/1400 — 9走

### 1.3 race-level effect

current rawを current guard（same_surface_only=true / min_usable_runs=2 / min_race_coverage=0.5）で見たcoverage potential:

| race | current qualified | current coverage | 全必要courseにbase timeがある場合 |
|---|---:|---:|---:|
| 9/19 阪神 ダ1400 | 16/16 | 1.000 | 16/16 |
| 9/19 中山 ダ1800 | 12/14 | .857 | 12/14 |
| 9/20 阪神 芝1200 | 4/16 | .250 | 15/16 |
| 9/20 中山 芝2200 | 1/13 | .077 | 13/13 |
| 9/26 阪神 ダ2000 | 11/16 | .688 | 14/16 |
| 9/26 中山 芝1600 | 6/14 | .429 | 14/14 |
| 9/27 阪神 芝1600 | 0/16 | .000 | 15/16 |
| 9/27 中山 芝1200 | 1/16 | .063 | 16/16 |

右列は「course keyが埋まればusable条件を満たし得る」というcoverage simulationであり、精度改善の証明ではない。ただし、coverage不足が現在のspeed停止の主要因であることは明確。

## 2. 追加で見つかった基盤問題

### 2.1 collect_from_raw() がcurrent schemaを読めない

scripts/build_base_times.py::collect_from_raw() は race["horses"] を見るが、current raw schema は races[].entries[].past_runs。

したがって current rawを --source raw に渡しても **0件**。fill_base_times.py::demand_from_raw() は正しく entries を使っており、builder本体と不整合。

v2 implementationで修正必須。

### 2.2 base_times artifactに品質情報が無い

current base_times.json はlookup値だけで、後から sample count / source / source period / date range / dispersion / class composition / going composition / estimator version が分からない。

### 2.3 実験中にtableを育てると比較が混ざる

forward Challenger の評価中にtableが変わると、同じmodel idの中身が変わる。**forward experiment用tableはfreezeする。** 新しいtableを使うなら new artifact id / new hash / 必要ならnew Challenger version にする。

## 3. base_times_v2 — Stage A: Coverage-only

目的は **現行speed logicを変えず、reference coverageだけを改善したときに何が起きるか測る** こと。

H1: 十分なbase-time coverageを与えると、芝でもspeed guardが起動し、Championよりranking / probability / character diversityの一部が改善する可能性がある。

H0: speedを起動してもmarket baselineやChampionより改善しない、あるいは悪化する。

## 4. Artifact design

Championの config/base_times.json は触らない。

推奨:
- data/reference/base_times/base-times-v2-coverage-20260928.json
- data/reference/base_times/base-times-v2-coverage-20260928.meta.json

lookup artifact は既存 speed_index.lookup_base_time() と同じshapeを維持し、Stage Aでspeed計算コードを変えない。

metadata artifact は最低限以下を保存:
- schema / artifact_id / built_at / cutoff_date
- source kind / source period
- estimator version / formula / class_offset hash / min_samples
- coverage totals
- courseごとの value / n / date_min / date_max / median / MAD / P25 / P75 / class_counts / going_counts

これらmetadataはStage Aではpredictionに使わない。監査用。

## 5. Data source strategy

### 5.1 Short-term — existing netkeiba route

既存 scripts.build_base_times.fetch_course_records / scripts.fill_base_times を **base_times coverage-only Challenger のbootstrap** に使う。

ただしcanonical historical DBにはしない。HTML/API structure・page parameter・availabilityに依存するため、long-term reproducibilityが弱い。取得はcontrolled manual jobで行い、サイト規約・負荷へ配慮する。

### 5.2 Long-term — JRA-VAN Data Lab

長期historical model developmentでは第一候補。

2026-09現在公式情報:
- 月額 2,090円
- 約40年のJRA公式データ
- real-time odds等あり
- JV-Link必須
- Windows専用

想定architecture:
Windows/JV-Link ingestion → normalized local dataset → research pipeline → derived features/models。

rawデータをrepoへ保存・共有してよいかは利用条件確認が必要。repoにはderived aggregates / schema / hashesだけを置く構成も選べる。

## 6. Leakage rules

Forward Challenger の source cutoff は registrationより前。例: source cutoff=2026-09-27, registered_at>=2026-09-28。その後tableはfreeze。

Historical backtestで9/20を評価するtableに9/27のrace resultを含めてはいけない。

valid methods:
- Fixed train/test: base-time training 2023-2025 → model evaluation 2026
- Rolling as-of: 各評価日より前のデータだけでtable構築

Stage Aのcurrent raw retrospective dry-runで見るのは coverage / ranking difference まで。performanceをunbiased backtestとは呼ばない。

## 7. Estimator policy

Stage A は v1-compatible。式は完全に現行のまま:

base_time = median(win_time - class_offset[class] * dist/2000)

理由は **coverageとformulaを同時に変えない** ため。

class offsets / all-going median / current class normalization に問題がある可能性は認識するが、この実験では固定。

## 8. Stage B — speed-v2.1 calibrated reference

Stage Aとは別experiment。

historical dataが揃った後に、

win_time = course_intercept[venue,surface,dist] + class_effect[class] * dist/2000 + going_effect[surface,going] * dist/2000 + residual

を robust regression / median regression で推定する案を優先。

目的は course base time / class effects / going effects を同じhistorical dataから推定し、手決めclass_offset / going_adj依存を減らすこと。

必要ならyear effect / season effectも候補だが、最初から入れない。

class_credit=0 の「speedは絶対走破能力を測る」という思想は維持可能。class effectはbase time推定時のnormalizationに使い、horse indexへ足し戻さない。

## 9. Speed-v2 Challenger integration

current build pipelineは全modelへ同じ base_times を渡すため、model-specific reference sourceを追加する。

model spec案:
- id: speed-base-times-v2-coverage
- role: challenger
- enabled: true
- base_times_file: data/reference/base_times/base-times-v2-coverage-20260928.json
- description: Championと同一ロジック。base_times coverageだけv2 frozen table。

各prediction/snapshotに:
- base_times_artifact_id
- base_times_hash
- base_times_cutoff
- base_times_method_version

を記録。model-specific fileのhashを使う。

## 10. Evaluation

Primary:
1. speed_quality.used rate
2. qualified horse coverage
3. Win p: log loss / Brier / calibration
4. actual Top3 recall: base recall@4 / recall@6
5. character selection: Kei/Tetsu/Gen rank correlation / dominant-axis agreement / axis exposure

Secondary:
- card hit rate
- payout / ROI
- conversion loss

ROIは少数レースで極端にぶれるので昇格primaryにはしない。

## 11. Expected diagnostic value

speed OFF時:
- Kei nominal 55/25/20 → speed absent時 ≈ aptitude 55.6% / human 44.4%
- Tetsu common 45/30/25 → speed absent時 ≈ aptitude 54.5% / human 45.5%

ほぼ同じ。

したがって v2によってspeedが安定して使えるようになれば、**KeiとTetsuのキャラ相関が本来の特徴量差によって下がるか** も重要な自然実験になる。多様性を人工的に作るのではなく、本来設計されていた特徴量差が復活するかを見る。

## 12. Acceptance criteria — base-times builder

Implementation PRで必須:
1. current raw schema entries[].past_runs を読む
2. old horses[] は必要ならbackward-compatible
3. duplicate recordをdeterministicに除外
4. cutoff dateを超えたrecordを拒否
5. metadataを生成
6. lookup + metaを同一buildから生成
7. source/method/hashをmanifestへ保存
8. dry-run coverage report

Tests:
- current raw fixtureからrecordを拾える
- race["horses"]しか見ないregressionを防ぐ
- same recordsならv1 formulaとvalue parity
- future-date record exclusion
- min_samples
- duplicate removal
- metadata n/MAD/date-range
- deterministic output/hash

## 13. Acceptance criteria — Challenger

1. Champion data/predictions.json byte-identical
2. Champion snapshot byte-identical
3. v2 table only speed Challengerが読む
4. Challenger snapshotにartifact id/hash/cutoff
5. forward pre-race freezeは既存と同一guard
6. post-race resultsをprediction pathで読まない
7. current Challengersと共存可能
8. model comparison coverageを必ず出す

## 14. Implementation sequence

PR A — reference foundation only:
- raw collector bug
- v2 builder/metadata
- coverage audit
- prediction codeは変更しない
- artifact生成はdry-run / research only

PR B — frozen artifact:
- controlled acquisition
- build v2 lookup/meta
- freeze
- hash
- sample counts/outliers review

PR C — speed-v2 Challenger:
- per-model base_times override
- model registry entry
- shadow only
- Champion byte-identical tests

After merge:
Champion(v1 sparse base_times) vs speed-v2 Challenger(full frozen base_times) を同時にforward記録。

## 15. Promotion rule

Speed-v2をChampionへ入れる条件は事前固定。

最低限:
- data coverageが安定
- log loss / BrierがChampionより悪化しない
- base recall@Kが改善または非劣化
- character diversityが人工的な逆張りなしで改善
- 特定1レースの大払戻に依存しない
- forward期間中にartifactを差し替えていない

数レースのROIだけでは昇格しない。

## 16. 今回やらないこと

- Tを調整しない
- λを調整しない
- weightsを調整しない
- ticket templateを変えない
- Chappy missingを直さない
- pace/styleを本番入力にしない
- Otori gateを変えない

Stage A の問いは1つだけ:

> **基準タイムcoverageが十分なら、現在のspeed factorは価値を持つか？**

これだけを測る。
