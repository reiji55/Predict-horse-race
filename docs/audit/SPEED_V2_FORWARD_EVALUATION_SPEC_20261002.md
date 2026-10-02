# speed-v2 Challenger forward 評価の事前登録 — 2026-10-02

作成: Claude（2026-10-02、10/3 以降の結果を見る前）
version: `speed-v2-forward-eval-v1`（`config/model_evaluation.json`）
実装: `research/model_evaluation.py`
モード: **evaluation-only / observe-only。** Champion・Challenger の予想・選定・買い目・重み・T・λ・テンプレ・Chappy・鳳ゲート・基準タイム表（r2）は変えない。

## 0. 目的

Champion（`win-v1-speed-guard`）と speed-v2 Challenger（`speed-base-times-v2-coverage`）を比べるときに、「何をもって良かった／悪かったと判断するか」を、**10/3 以降の結果を見る前に固定する**。

- 結果を見てから評価方法や閾値を都合よく変えないためのもの。
- **このバージョンでは昇格の判定をしない。**
  - 「何レースで Champion を超えたら採用」「ROI が何%以上なら採用」といった基準は決めていない。
  - 昇格基準は、historical dataset が揃った段階で、chronological holdout・bootstrap CI・market baseline を含めて別途事前登録する。

## 1. 対象（forward・paired）

**forward の境界**
- `max(評価式の registered_at, Challenger の registered_at)` を境界にする。
  - 評価式の registered_at は 2026-10-02T22:00 JST、Challenger の registered_at は 2026-10-03T09:00 JST。
  - いまの境界は 2026-10-03T09:00 JST。
- この時刻より**後に発走した**レースだけを数える。

**対にする条件**
- 次のすべてを満たすレースだけを、同じレース・同じ発走前情報の対として評価する。
  1. 確定着順（3着まで）がある
  2. 両モデルの snapshot が発走前に凍結されている（`pre_race=true`、`frozen_at < 発走`）
  3. 同じビルドで作られている（`frozen_at`・`config_hash` が同じ）
  4. 出走馬と単勝オッズが同じ
  5. Challenger の snapshot が、登録どおりの凍結表で作られている（`base_times_ref` の artifact_id・lookup_sha256 が registry と一致）

**評価しないレースの扱い**
- 満たさないレースは評価せず、理由付きで `coverage.not_evaluated` に残す。黙って落とさない。
  - 理由の例：`challenger_missing` / `challenger_not_prerace` / `frozen_at_mismatch` / `config_hash_mismatch` / `prerace_inputs_differ` / `challenger_artifact_mismatch` / `*_frozen_after_post`
- `coverage_rate` は、評価できた対の数 ÷ forward で結果のあるレース数。
- 登録前のレースは `excluded_pre_registration` に分けて出す。coverage の分母にも入れない。
- 結果待ちのレースは `pending_result` に出す。

## 2. 入力

**発走前の記録（capture）**
- 通常パイプラインで予想と snapshot が作られた直後に、両モデルについて発走前の記録を追記する。
  - 保存先：`data/shadow/model_evaluation/prerace/{model_id}/{race_id}/{時刻}_{phase}.json`
- 本番と同じ関数で再計算し、snapshot と一致したとき（fidelity=true）だけ記録する。記録するのは次の2つ。
  - 全精度の score
  - キャラ別の選定順（ケイ・哲・源）
- 発走後は記録しない。

**採点時の照合**
- 採点時の snapshot と SHA-256 が一致し、発走前に取られた capture だけを使う。

**公平のための規則**
- 全精度 score とキャラ別選定順は、**両モデルとも揃ったときだけ**使う。
- 片方でも欠けたら、score は両モデルとも snapshot.marks の値（0.1丸め）にそろえる。選定順の指標は両モデルとも記録しない。

## 3. Primary metrics

### 3.1 確率評価
- 単勝 p = softmax(score / T)。T は `config/myomi.json` の本番値で、両モデル共通。
- 評価する馬の集合
  - score と正の単勝オッズを、両モデルとも持つ馬に限る。
  - その馬数がフィールドの 80% 未満、または勝ち馬が対象外なら、そのレースの確率評価はしない（status に理由を残す）。
- 指標
  - **log loss** = −ln p(勝ち馬)
  - **Brier** = Σ_i (p_i − y_i)²（多クラス。勝ち馬だけ y=1）
  - 参考：市場 q（1/odds を正規化）の同じ指標も並べる（モデルではなく基準線）。
- **calibration bucket**
  - 全レースの (p, 勝ち/負け) をまとめ、固定した区切りで数・平均 p・実際の勝率を出す。
  - 区切りは [0, .02, .05, .1, .15, .2, .3, .5, 1.0]。

### 3.2 ランキング・馬の選定
確定 Top3 に対する recall@4 / recall@6（分母は3）を、次の順位で出す。
- `base`：印の並び（base score 降順）
- `top3`：Top3 モデルの順位（`top3_rank`）
- `kei_sel` / `tetsu_sel` / `gen_sel`：キャラ別の選定順（capture。両モデル揃ったときだけ）
- キャラのカード選定 recall：買い目に登場した馬のうち Top3 に入った数 ÷ 3

### 3.3 Speed data quality（今回の実験の中心）
- 両モデルについて、レース単位で次を残す。
  - `used` / `qualified_horses` / `total_horses` / `coverage` / `raw_available_horses` / `reason`
- 累積では、全体と芝・ダ別に次を出す。
  - speed が使われたレース数・率
  - guard を通った馬の数・率
  - 平均 coverage
- **「芝で speed が実際に起動するようになったか」** は、芝の `used_rate` で見る。

### 3.4 キャラの多様性（ケイ・哲・源）
- `research/forward_diagnostics.fixed_three` と同じ定義を使う。
  - 選定順の順位相関（Spearman、ペアごと）
  - 3人の軸一致数（`max_axis_agreement`）、3人とも同じ軸か
  - dominant axis と、その馬に依存する購入額の割合（`dominant_axis_exposure_ratio`）
  - 買い目に登場する馬の集合の Jaccard（ペアごと）
- これらは**良し悪しの向きを決めない観察値**として扱う（paired の direction は `descriptive_only`）。
  - 「無理やり違う馬を選ばせる」のではなく、speed 復活で本来のキャラ差が自然に戻るかを見るため。

## 4. Secondary metrics（判定には使わない）
- 固定3人（合計・キャラ別）と自動 Chappy について、次を出す。
  - カードの的中率、購入額、払戻、ROI
  - 採点は正式成績と同じ `results.build_results` の関数で行う。
- conversion loss
  - 3着内の2頭を選んでいたのに、成立する券種で買っていない
  - 3頭とも選んでいたのに、3連複を買っていない
- 固定3人の買い目全体での的中組のカバー
  - ワイドの3組のうち何組、馬連1-2、3連複
- `largest_single_race_payout_share`
  - 払戻が1レースに偏っていないかの目印。1回の大当たりで勝ちと読まないため。

## 5. paired の集計
各指標について、両モデルとも値があるレースだけで次を出す。
- n
- 両モデルの平均
- **差の平均（Challenger − Champion）とその標準誤差**
- Challenger が良かった／Champion が良かった／同じ、のレース数
  - 良し悪しの向きは、log loss・Brier は小さいほど良い、recall は大きいほど良い。
  - 多様性は向きを決めない。

これらは**記録であって判定ではない**。少数レースでの差や勝ち数で、採用・不採用を決めない。

## 6. 出力（研究専用）
- `data/shadow/model_evaluation/speed-base-times-v2-coverage/races/{race_id}.json`：レース単位（評価しなかったレースも理由付きで残す）
- `data/shadow/model_evaluation/speed-base-times-v2-coverage/summary.json`：累積（coverage・Primary・Secondary）
- `data/shadow/model_evaluation/prerace/…`：発走前の記録

**分離**
- 正式成績（`data/results.json`）・UI・予想経路（`logic/`・`scraper/`・`results/`）には混ぜない。
- `config/model_evaluation.json` は Champion の `config_hash` に含めない。

**workflow**
- `run_pipeline.yml`：予想のあとに capture を実行する
- `run_results.yml`：Challenger の採点のあとに evaluate を実行する
- どちらも `continue-on-error`。

## 7. 変更のルール
- 評価式・バケット・対象条件を変えるときは、`version` と `registered_at` を両方進める。
- 変更より前のレースは、新しい式の forward に入れない（境界は自動で後ろへずれる）。
- 結果を見たあとで、同じ version のまま式を変えない。

## 8. 実装時の確認について
- 実装の動作確認には、W39（9/26・9/27）の実データを使った。
  - これらは登録前のレースで、凍結表に勝ちタイムが入っているリークのあるデータ。
  - テストでは登録日時を仮に早めて動かしている。
- 確認したのは、仕組みが動くこと（4レースが対になる、芝で speed が起動する、など）だけ。
- 出てきた数字を見て、評価式・区切り・条件を変えることはしていない。
