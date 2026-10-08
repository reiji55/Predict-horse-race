# Race Performance v1 — 事前登録（PR-C0）

version: `race-performance-v1`
設定: `config/race_performance_v1.json`
もとの仕様: `Weekend3CARDS_PRC_RacePerformance_Prereg_v1_20261008.md`（ChatGPT 作成、2026-10-08）

> **研究専用の事前登録。** この文書と設定ファイルは、PR-C1 で実装する Challenger `race-performance-v1` の式・設定・評価方法を、forward の結果を見る前に固定するためのもの。
> - この PR（PR-C0）では、計算の実装・Champion・Challenger の登録・本番 workflow・買い目・UI を変えない。
> - どのコードも `config/race_performance_v1.json` を読まない。`model_registry.HASH_CONFIGS` にも入れないので、`config_hash` は変わらない。
> - ここに書く式は「優位性が証明された式」ではない。これから検証する v1 の仮説を固定するための定義。

## 0. 位置づけ

- **PR-C は B 案だけを扱う。** B 案は、class と margin で成績の質を測る案。
  - A 案（研究用の長期履歴15走・同レース実績）は、この Challenger に入れない。
  - 入力の `past_runs` は、いまの raw の直近4〜5走だけ。
- **進め方は3段階。**

  | 段階 | 内容 |
  |---|---|
  | PR-C0（この PR） | 事前登録の文書と研究用の設定だけ。レビューでルールを確定し、マージしてから次へ進む |
  | PR-C1 | 登録した式・設定を変えずに、`race_performance_score` という1要因だけを、独立した Challenger `race-performance-v1` に実装する。Champion には適用しない |
  | PR-C2（必要なら） | forward の比較・診断の出力だけを足す。評価のロジックを結果に合わせて変えない |

- **やらないこと：**
  - 2要因を同時に入れる
  - 特定のキャラの役割を変える
  - Bet Builder・予算・テンプレートを変える
  - オッズを混ぜる
  - T・λ を調整する

## 1. 登録記録

| 項目 | 値 |
|---|---|
| version | `race-performance-v1` |
| 設定ファイル | `config/race_performance_v1.json` |
| 設定の canonical SHA-256 | `592cf78fef99866839d1c385eded9a7388c7e6a5a7e5a1635d7bd9e902121b70` |
| canonical の作り方 | `json.dumps(設定, ensure_ascii=False, sort_keys=True, separators=(",", ":"))` を UTF-8 にして SHA-256。`model_registry._canonical_sha256` と同じで、キーの順番・空白・改行には依存しない |
| 登録 commit | この PR を main に入れたマージ commit。SHA・committer 時刻・PR の `merged_at` は、PR-C1 の最初にこの表へ追記する |
| 事前登録の有効日時 | GitHub の PR の `merged_at` と、上の登録 commit の committer 時刻の**遅い方**。どちらも GitHub・git が記録した実際の時刻 |
| 評価式の登録 | 設定の `evaluation` 節。有効日時は事前登録と同じ |

**登録日時を手で書かない理由**
- 文書や設定の中に、それ自身を含む commit の SHA や時刻は書けない。
- 手で時刻を書くと、未来の時刻になるか、過去へ遡った時刻になる。
- そこで、マージの実際の時刻を有効日時とする。値はマージ後に、起きた事実として追記する。
- `merged_at` と committer 時刻の遅い方を使うのは、どちらか一方だけで境界を前へ遡らせないため。

### 変更履歴

| 日付 | 段階 | 内容 |
|---|---|---|
| 2026-10-08 | PR-C0 提出（`44d5f01`） | 初版。もとの仕様のとおりに式・パラメータ・統合・評価を書き起こした。仕様に無い点・既存実装との衝突は §11 に挙げ、仕様どおりに読める形を下書きとして入れた |
| 2026-10-08 | PR-C0 レビュー対応（登録前の設計変更） | ① 識別力の無い RPS（使える馬が2頭未満・sd=0）は、そのレースだけ因子ごと無効にし、既存3因子に完全に戻す（§5.2）。② キャラ別の重みは `unchanged_3_factor` で確定。RPS で変わり得る経路の記述を実コードに合わせて直した（§5.3）。③ Brier を「主要な副指標（key secondary）」に統一し、照合の契約をモデル間とモデルごとに書き分けた（§7.2・§7.4）。④ 有効日時を `merged_at` と committer 時刻の遅い方にした。式・パラメータ・重みの値は変えていない |

マージ前のレビューで直した点は、登録前の設計変更としてこの表に残す。

## 2. 仮説

**仮説：** いまの指標は、過去走の着順を頭数で正規化して見ている。これに加えて、**レース格（`class`）と勝ち馬との差（`margin_sec`）を1つの成績内容因子として評価すると、登録後のレースの勝率予測 log loss が Champion より下がる。**

## 3. 入力と境界

- **発走前に使えた raw だけを使う。** 具体的には `entries[].past_runs`、つまりいまの予想の入力そのもの。
  - 取消・除外、過去走のうち未来や同日のもの、後から更新された資料は混ぜない。
- **オッズは特徴量に使わない。** 価格差や期待回収率の検証は、確率予測とは別に行う。
- **finish（着順）は RPS に使わない。** 現行の着順評価と二重にしないため。
- **研究用の長期履歴（PR #27 の ajax）と同レース実績は使わない。**

## 4. Race Performance Score（RPS）の定義

### 4.1 class strength

順序は固定。数値は探索用の仮パラメータで、後から最適化しない。

| class | mi | 1win | 2win | 3win | op | g3 | g2 | g1 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| strength | 0.10 | 0.25 | 0.40 | 0.55 | 0.70 | 0.80 | 0.90 | 1.00 |

- キーは既存の `scraper.common.constants.CLASS_KEYS` と同じ8つ。
- 既存の正規化では、次のように丸めている。
  - 新馬 → `mi`
  - (L) → `op`
  - Jpn 表記（地方交流重賞）→ `g1`〜`g3`
- RPS は raw に入っている `class` をそのまま使う。

### 4.2 有効走

`past_runs` の各走を先頭から見る。下の理由に当たった走は除外し、**最初に当たった理由を1つだけ**数える。

| 順 | 理由 | 条件 |
|---|---|---|
| 1 | `beyond_max_runs` | 入力位置が5走目より後ろ（いまの raw は最大5走なので、通常は起きない） |
| 2 | `date_invalid` | 日付が無い・読めない（補完しない） |
| 3 | `same_day` | `run.date == race_date` |
| 4 | `future` | `run.date > race_date` |
| 5 | `today_not_flat` | 今日のレースの surface が芝・ダ以外 |
| 6 | `run_not_flat` | その走の surface が芝・ダ以外（障害など） |
| 7 | `surface_mismatch` | 今日と surface が違う |
| 8 | `dist_missing` | その走か今日の距離が無い |
| 9 | `dist_out_of_range` | `|run.dist − today.dist| > 400` |
| 10 | `class_unknown` | class が null、または §4.1 の表に無い |
| 11 | `margin_missing` | `margin_sec` が null |
| 12 | `margin_invalid` | 数値でない・bool・NaN・±Inf |

- `race_date` は race id の先頭8桁（YYYYMMDD）。読めなければ、そのレースの RPS は全馬 null。

### 4.3 1走の値

```
m     = max(0, margin_sec)
v_run = class_strength[class] × exp(−m / 1.0)        # tau_sec = 1.0
```

- **margin が負の場合：** 0 として扱い、`margin_clamped_negative` を記録する。勝ち馬の 0 より有利にしない。
  - いまの raw では、1着の margin は 0.0 に上書きされているので、負の値は出ていない。
- **大きな有限値：** 除外しない。exp で 0 に近づくだけで、NaN や −Inf は生まれない。
- この式なら、次の走は自動的には高く評価されない。
  - 格下のレースでの大差勝ち（class が低い）
  - 上位クラスでの大敗（margin が大きい）

### 4.4 重み

```
w_recency = [1.0, 0.9, 0.8, 0.7, 0.6][入力位置]   # 入力位置は past_runs の0始まりの位置
w_dist    = 1 − |run.dist − today.dist| / 800     # 距離差 400m 以内の走だけなので 0.5〜1.0
```

- **入力位置で決める。** 無効な走を詰めて、位置をずらさない。

### 4.5 集約

```
rps_raw = Σ(v_run × w_recency × w_dist) / Σ(w_recency × w_dist)    # 有効走だけ
```

- **有効走が2走未満なら `rps_raw = null`。** 0 埋め・平均補完・推測はしない。
- **パラメータは1セットだけ。** 補正の階段や複数の tau を比べて、10/4 の勝ち馬がいちばん上がるものを選ぶ、ということはしない。

### 4.6 検算例（合成データ。PR-C1 の単体テストにする）

**1走の値**

| 走 | v_run |
|---|---|
| G1・4着・0.0秒差 | 1.00 × e⁰ = **1.000000** |
| 3勝クラス・1着・0.0秒 | 0.55 × e⁰ = **0.550000** |
| G2・0.1秒差 | 0.90 × e^−0.1 = **0.814354** |
| G2・2.0秒差 | 0.90 × e^−2.0 = **0.121802** |

- G1 の 0.0秒差4着は、3勝クラスの0.0秒差1着より高い。
- G2 の 0.1秒差は、G2 の 2.0秒差より高い。

**集約の例**

今日は芝1800。過去5走が次のとき：

| 位置 | 走 | 扱い | v_run | w_recency | w_dist |
|---|---|---|---|---|---|
| 0 | 芝1800・G1・0.0秒 | 有効 | 1.000000 | 1.0 | 1.00 |
| 1 | ダ1800 | `surface_mismatch` で除外 | − | − | − |
| 2 | 芝2000・3勝・0.5秒 | 有効 | 0.333592 | 0.8 | 0.75 |
| 3 | class 不明 | `class_unknown` で除外 | − | − | − |
| 4 | 芝2200・OP・1.2秒 | 有効 | 0.210836 | 0.6 | 0.50 |

```
rps_raw = (1.000000×1.0×1.00 + 0.333592×0.8×0.75 + 0.210836×0.6×0.50)
        / (1.0×1.00 + 0.8×0.75 + 0.6×0.50)
        = 1.263406 / 1.9
        = 0.664950
```

- 位置2・4の重みは、除外した走（位置1・3）を詰めず、入力位置のまま。

## 5. Challenger への統合

### 5.1 重みと標準化

- **既存の3因子を置き換えない。** `race_performance_raw` を4番目の標準化因子として足す。
- **重み：** 既存3因子の相対比を保ったまま 0.85 倍し、RPS に 0.15。

  | 因子 | Champion（`config/cards.json`） | race-performance-v1 |
  |---|---:|---:|
  | speed | 0.45 | 0.3825 |
  | aptitude | 0.30 | 0.2550 |
  | human | 0.25 | 0.2125 |
  | race_performance | − | 0.1500 |

- **レース内の z 標準化：** 因子が有効なレース（§5.2）では、`logic/base_score.z_standardize` と同じ規則（母標準偏差）。RPS は補完しないので、stat_mask は使わない。
- **欠損：** 因子が有効なレースで RPS が null の馬は、その馬だけ残りの因子で重みを再正規化する（`composite_scores` と同じ）。欠損を一律に平均で埋めない。
- **uncertain フラグ：** RPS の欠損では立てない。既存3因子の定義のまま。
- **Champion と既存の Challenger**（`top3-partner-v1` / `race-regime-abstain-v1` / `speed-base-times-v2-coverage`）は、元の3因子のまま。

### 5.2 識別力の無いレースでは因子ごと無効にする

**次のどちらかに当たるレースでは、RPS 因子をそのレースだけ無効にし、既存3因子に完全に戻す。**

| 理由（`factor_inactive_reason`） | 条件 |
|---|---|
| `fewer_than_min_horses_with_rps` | RPS が null でない馬が2頭未満（全馬 null を含む） |
| `zero_sd` | RPS のレース内の標準偏差が 0（使える馬の RPS が全部同じ値） |

- **無効のときの合成：** Champion と同じ `score_weights`（`config/cards.json` の3因子の重みそのもの）で合成する。
  - 0.85 倍した重みを再正規化するのではなく、元の重みを使う。
  - こうすると、そのレースの score と p は既存3因子の版と同じ値になる。
- **理由：** 無効にしないと、識別力の無い RPS で次のことが起きる。
  - RPS の z が全馬 0 になる。
  - 既存3因子の寄与だけが一律 0.85 倍に縮む。
  - score の分布が平らになって softmax の実効温度が変わり、log loss が動く。
  - これでは「class＋margin の識別力」と「ただの分布の平坦化」を切り分けられない。
- **記録：** レースごとに `factor_active`・`factor_inactive_reason` を残す（§6）。
- 2頭以上で sd>0 でも、RPS がある馬が少ないと z は大きく振れる。これは監査項目として見る（§12）。

### 5.3 キャラ別の重みと、RPS で変わり得る経路

- **キャラ別の重みは変えない（`unchanged_3_factor`）。**
  - ケイ・源さんは、`config/cards.json` に3因子の上書き重みを持つ。それによる3因子の計算（`sel_score`・`sel_p`・`sel_base_rank`）は変えず、RPS を入れない。
  - ケイ・源さんの独自の配分まで同時に広げると、初回の実験で効果がどこから来たか分からなくなるため。
  - 鳳は、Chappy カードの特別な状態として作られる。通常のパイプラインでは `otori.score_weights` を使わない（`cards.generate_card_for_character` は固定3キャラだけを作る）。
- **ただし、共通の score・base_rank・p を使う経路は、RPS で変わり得る。**

  | 経路 | 変わり得るか | 理由 |
  |---|---|---|
  | 印・共通の base_rank・p・妙味 | 変わり得る | 共通の score から作る |
  | 哲さんのカード | 変わり得る | 上書き重みが無く、共通の base_rank・p を使う |
  | 源さんの軸・券 | 変わり得る | 軸の制約 `axis_base_rank_floor` は共通の base_rank を見る（`cards.select_horses`） |
  | Chappy・鳳のカード | 変わり得る | `logic/chappy.py` は共通の score・p を使う |
  | ケイ・源さんの `sel_score`・`sel_p` | 変わらない | キャラ固有の3因子の重みで作る |
  | q・T・テンプレート・λ・予算 | 変わらない | RPS と無関係 |

- どの経路が変わり得て、どの経路が変わらないかは、PR-C1 のテストで確かめる。

### 5.4 変えないもの

- T、score_scale、q
- テンプレート・λ・予算・キャラのルール・キャラ固有の3因子の重み
- Chappy のルール、鳳のゲート、speed guard、基準タイム表

## 6. 監査用に残すもの（発走前だけ）

**馬ごと**
- `n_input_runs`・`n_usable`・`rps_raw`
- 除外理由別の件数
- `margin_clamped_negative` の走数
- 有効走ごとの値：入力位置・日付・class・margin_sec・距離・v_run・w_recency・w_dist

**レースごと**
- 出走頭数・RPS が使えた頭数・`rps_coverage`
- `factor_active`・`factor_inactive_reason`（§5.2）
- 除外理由別の合計
- 設定の SHA-256・version・入力 raw の hash

**レース後の情報を混ぜない。** 記録は発走前の snapshot と capture にだけ残す。

## 7. forward 評価（採否は別に決める）

### 7.1 対象

- **forward の境界：** `post_at > max(事前登録の有効日時, Challenger の登録日時, 評価式の登録日時)`
  - 3つとも、それぞれの登録を main に入れたときの時刻（PR の `merged_at` と、マージ commit の committer 時刻の遅い方）。
  - 評価式はこの設定の `evaluation` 節で、この PR で登録する。PR-C2 は実装だけで、定義を変えない。
  - Challenger の登録（PR-C1 のマージ）は必ずこの PR より後になる。なので、実際の境界は PR-C1 のマージ時刻で決まる。
- **両モデルの発走前 snapshot と capture が揃うレースだけ**を対にする。
- **登録より前に発走したレースは、forward の成績として採点しない。** 10/3・10/4 を含む（§8）。

### 7.2 主指標と主要な副指標

| 区分 | 指標 | 向き |
|---|---|---|
| 主指標（primary） | paired log loss（Challenger − Champion）＝ −ln p(勝ち馬) の差 | 低いほど良い |
| 主要な副指標（key secondary） | paired multi-class Brier ＝ Σ_i (p_i − y_i)² の差 | 低いほど良い |
| 必ず併記 | `score_coverage`・`rps_coverage`・paired で評価できたレースの率 | − |

- 主指標は log loss の1つだけ。Brier は主指標ではない（後から都合のよい指標を選べないよう、ここで固定する）。

- 定義は、既存の `research/model_evaluation.py` と `research/calibration.win_metrics` と同じ。
  - eps は 1e-12。
  - p は、score のある全馬で softmax(score/T)。
  - T は凍結時の値だけを使う。
  - score のある馬が80%未満のレースは、確率評価をしない。

### 7.3 診断指標

- top3 の実着順の `recall@4`・`recall@6`
- キャラ別の選定
- 確率帯別の calibration
- 単勝市場 q との比較
- 投資額・払戻・ROI・見送り率

ROI は分散が大きいので、初期の採否の根拠には使わない。

### 7.4 整合性の検査（照合の契約）

**モデル間で同じであることを確かめるもの（共通の入力）**
- race_id・post_at・出走馬の集合・発走前の単勝オッズ
- 入力 raw の hash（capture に記録する）
- frozen_at（同じビルドで作られたこと）・T

**モデルごとに確かめるもの**
- 各モデルの snapshot を、**そのモデル自身の発走前 capture**（snapshot の SHA-256・fidelity）と照合する。
- モデル間で snapshot の中身や SHA-256 が同じことは求めない（モデルが違えば中身は違う）。

**モデルの識別**
- 「両モデルが同じ版か」ではなく、**それぞれを自分の登録値と照合する**。
  - Champion：model_id と config_hash
  - Challenger：model_id と `race_performance_ref`（version・設定の canonical SHA-256）

**合わないとき**
- 合わないレースや、登録値と違うモデルの記録を、黙って落とさない。理由つきで coverage に残す。

### 7.5 不確実性と点検

- **不確実性：** 会場・開催日の中の相関を無視しない。レース単位の paired 差と、開催日単位の bootstrap などで見る。
- **併せて見る：** log loss が改善しても、Brier・calibration・coverage が悪化していないか。
- **40 paired races で最初の運用点検をする。**
  - バグ・coverage・方向感の点検であって、**昇格の閾値ではない**。
  - 少数レースの成績では昇格しない。十分な結果が揃うまで、式とパラメータを凍結したまま続ける。

### 7.6 採否

採否に必要なサンプル数と不確実性の閾値は、ここでは決めない（`promotion: not_registered`）。過去の実績を見たあとに自由に選ばないよう、結果を見る前に**別の事前登録**で決める。

## 8. 既知レースと探索の区別

- **登録より前に発走したレースは、探索・バグ回帰専用。** 2026-10-03・10-04 を含む `raw/2026-W38`〜`W40` の全レース。
  - forward の成績として採点しない。
  - 10/4 の「1→13→3」を当てるために、定数や候補の条件を調整しない。単独の的中や上位馬の順位の改善は、根拠として不十分。
- **この PR の作成では、実レースで RPS を計算していない。** 式・パラメータは、もとの仕様のとおり。
- **確認したのはデータの形だけ。** W38〜W40 の raw の過去走 848 走について、着順・結果・RPS の値は見ていない。
  - class の種類：8つのキー＋不明 5走
  - surface：芝 558・ダ 280・障 10
  - margin_sec：0.0〜11.8 秒。負の値と null は無い

## 9. 変更規則

- **登録（マージ）後は、v1 の設定を変えない。**
  - 変えるなら `race-performance-v2` として新しく事前登録し、新しい境界を作る。
  - v1 の forward データとは混ぜない。
- **マージ前のレビューでの修正**は、登録前の設計変更として §1 の変更履歴に残す。
- **PR-C1 の実装中に定義の矛盾が見つかった場合**は、黙って直さない。止めて報告し、必要なら版を上げて登録し直す。
- **定義を変えないバグ修正**（実装を登録した定義に合わせる修正）はしてよい。変更履歴に残す。
- **Champion の `score_weights` が将来変わった場合**も、v1 の重みは登録値のまま（作り直さない）。
  - 比較の前提（既存3因子の相対比が同じ）が崩れるので、PR-C1 では起動時に `derived_from` と `config/cards.json` を照合する。
  - 違えば、Challenger だけ fail-closed にする。

## 10. PR-C1 以降の実装の約束（この登録に含む）

- **RPS の計算は専用の関数（モジュール）に分ける。** Challenger の `model_spec` でだけ有効にする。
- **Champion と既存の3 Challenger の既定経路は、ビット単位で同じにする。**
  - 全馬の素点・score・p・印・キャラ別の買い目・金額が、変更前と完全に一致することをテストで示す。
- **`config_hash` は「不変」と主張しない。**
  - `models.json` は `HASH_CONFIGS` に入っているので、Challenger を登録すると全モデルの `config_hash`（メタデータ）が変わる。
  - 差分の比較では、`config_hash`・`git_commit`・時刻などのメタデータを除き、計算値・カード・金額で厳密に比べる。
- **PR-C1 のテストに入れるもの（この登録で決めた挙動の確認）：**
  - §4.6 の検算例（1走の値と集約）。
  - §4.2 の除外理由と順番、入力位置での重み。
  - §5.2：RPS が2頭未満・sd=0 のレースで `factor_active=false` になり、そのレースの score と p が既存3因子の版と同じ値になる。
  - §5.3：変わり得る経路（印・共通の base_rank・p・妙味・哲さん・源さんの軸・Chappy・鳳）と、変わらない経路（ケイ・源さんの `sel_score`・`sel_p`、q・T・テンプレート・λ・予算）。
- **Challenger の snapshot に残すもの：**
  - `race_performance_ref`：version・設定のパス・canonical SHA-256
  - `race_performance_quality`：§6 のレース単位の記録
  - どちらも、既存の `base_times_ref` と同じく「その項目を持つモデルだけ写す」形にする。Champion の snapshot の形は変えない。
- **起動時の照合：** 設定の canonical SHA-256 を `models.json` の登録値と照合する。違えば、その Challenger だけ fail-closed にし、Champion と他の Challenger の成功・snapshot を止めない。失敗したことは記録として残す。
- **発走前の capture の再計算**（snapshot との一致の確認）でも、RPS を含めて再計算する。
- **長期履歴の ajax（PR #27）は入力にしない。**

## 11. 既存実装との衝突・確認事項（PR 本文にも記載）

1. **キャラ別の重み（レビューで確定）**
   - 仕様が決めているのは、共通の重みだけ。一方、`config/cards.json` ではケイ・源さんが3因子の上書き重みを持ち、`cards.assign_character_ranks` がそれでキャラ固有の選定順と p を作り直している。
   - **v1 は `unchanged_3_factor`（上書き重みは変えず、RPS を入れない）で確定。** ケイ・源さんの独自の配分まで同時に広げると、初回の実験で効果がどこから来たか分からなくなるため。
   - 初版では「ケイ・源さん・鳳の選定順は Champion と同じになる」と書いたが、これは実コードと合っていなかった。正しくは §5.3 のとおり。
     - 源さんの軸の制約は共通の base_rank を見るので、源さんの軸・券は変わり得る。
     - Chappy・鳳は共通の score・p を使うので、変わり得る。
     - 鳳は Chappy カードの特別な状態で、`otori.score_weights` は通常のパイプラインでは使われていない。
2. **識別力の無い RPS（レビューで追加）**
   - 初版のままだと、RPS が全馬同じ値のときなどに、RPS の z は全馬 0 なのに既存3因子の寄与だけが 0.85 倍に縮み、p が動いていた。
   - §5.2 の規則（因子ごと無効にし、既存3因子に完全に戻す）を足した。
3. **margin の「異常」の解釈**
   - 除外するのは null・数値でない・bool・NaN・±Inf だけ。
   - 大きな有限値（raw の最大は 11.8 秒）は除外せず、exp の減衰に任せる。
4. **登録日時の記録方法**
   - 文書自身の commit SHA は、同じ commit の中に書けない。
   - そこで、有効日時は PR の `merged_at` とマージ commit の committer 時刻の遅い方とし、PR-C1 の最初に追記する（§1）。
5. **評価の仕組みが1つの Challenger 専用**
   - いまの `config/model_evaluation.json` と `research/model_evaluation.py` は speed-v2 専用になっている。
     - `challenger_model_id` が1つ。
     - 照合キーが基準タイム表（`base_times_ref`）。
     - capture の再計算が、モデルの違いを基準タイム表でしか表せない。
   - RPS では、照合キーを `race_performance_ref` にし、capture の再計算に `model_spec` を渡す拡張が要る（PR-C1・C2）。
   - 評価式の定義はこの PR で登録し、C2 は実装だけにする。こうすると評価式の登録時刻は C0 のマージになり、C1〜C2 の間のレースを境界で失わない。
6. **`config_hash` の変化と speed-v2 の評価**
   - PR-C1 で `models.json` に登録すると、`config_hash` が変わる。
   - speed-v2 の評価は、capture に残した T を使っている。10/3 の評価済み2レースは、どちらも `temperature_source=capture`。なので、影響は小さい。
   - ただし capture の無いレースは、T を確かめる経路（snapshot の `config_hash` ＝ いまの `config_hash`）が使えなくなり、`temperature_unverifiable` になる。
7. **fail-closed の記録**
   - いまの `build_predictions.main` は、Challenger の `OSError` / `ValueError` を受けてログに出すだけで、記録ファイルは残さない。
   - 仕様の「当該 Challenger のみを fail-closed で記録」には、C1 で記録を足す必要がある。
8. **speed が無効のレースでの実効比重（監査項目）**
   - speed guard でレース全体の speed が無効になると、残りの因子で再正規化される。そのため RPS の実効比重は 0.15 / (0.2550 + 0.2125 + 0.15) ≈ **24.3%** に上がる。
   - W40 では4レース中3レースで speed が無効だった。仕様どおりの動きだが、forward で必ず見る。
9. **score のある馬の集合が変わる可能性**
   - 既存3因子が全部欠けていて RPS だけある馬がいると、Champion と Challenger で score のある馬が変わる。
   - その場合、既存の評価では `score_set_mismatch` になり、そのレースは比べない。まれなケースだが、理由つきで残る。

## 12. 監査項目（forward で必ず見る）

- 出走頭数や、RPS がある馬の数による z の拡大（RPS がある馬が少ないレースほど z が大きく振れる）。
- class は speed にも入っている（基準タイムの `class_offset`）。そのため speed と RPS が重なる可能性がある（レース内の相関）。
- speed が無効のレースでの、RPS の実効比重（§11 の8）。
- `factor_active=false` になったレースの数と理由（§5.2）。
- `rps_coverage` の分布と、除外理由の分布。
- 地方交流重賞（Jpn）を g と同格に丸めている既存の正規化の影響。
