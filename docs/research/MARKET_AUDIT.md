# 発走前の市場オッズ監査（market-audit-v1、observe-only）

Issue #38 (A) と #39 の PR-A。次の確率モデル（#39）に入る前に、入力の「時点」と「欠け」を機械的に確かめる。
コード：`research/market_audit.py`。出力：`data/research/market_audit/`。予想・正式成績・UI・既存の shadow 採点には使わない。

## なぜ必要か

raw は発走後の結果取り込み・再実行で書き換わる。main の最新の raw でオッズを読むと、発走後のオッズが混ざる（リーク）。
実際に 10/18 の Claude の #39 コメントでは、発走後の raw で3連複のカバー率を計算していた（ChatGPT の指摘で訂正）。

## 何をするか

レースごとに（`data/snapshots/` にある全レース）:

1. **発走前の版への固定**：snapshot に触れたコミットのうち、発走時刻より前にコミットされた `pre_race` の版を集め、最後の版のコミットに raw を固定する。発走後にまとめてコミットされた snapshot（後からの追加）は、Git の時刻で発走前性を示せないので `no_prerace_commit` として監査しない。
2. **オッズの時点とカバー率**：固定した raw の3連複オッズについて、次を記録する。
   - `source_time` と `observed_at`、それと発走時刻との差（`minutes_before_post`）
   - `odds_timing_status`（時刻が無いことを「発走前」と同一視しないための判定）
     - `pre_race_verified`：どちらも発走前で、`source_time ≤ observed_at ≤ 固定したコミットの時刻` の順序も正しい
     - `post_race_detected`：どちらかが発走時刻以降
     - `unknown`：時刻が記録されていない（時点は未検証。10/3・10/4 の4レースが該当）。後で使う確率エンジンは除外するか別に扱う
     - `inconsistent`：観測の順序が崩れている（異常）
     - 互換用の `post_race_odds` は、発走後を検出したときだけ true、検証済みの発走前だけ false、それ以外は null
   - 収録組数と全組数、Σ1/odds、`trio_mass_listed_approx`（= 0.75 × Σ1/odds。市場の確率質量のうち収録分の目安）
   - `trio_invalid_rows`：形式が不正な行や、raw.entries に無い馬番を含む行（確率質量に積まずに捨てた数）
3. **現在の main の raw**：同じレースのオッズが発走後のものに書き換わっているか（`head_raw.post_race_odds`）。true のレースは、main の raw を学習・評価に使ってはいけない。
4. **券の依存度**（固定3人・チャッピーのカードと、scenario-frame の記録）：
   - 馬ごとの露出、最も露出の多い馬の割合
   - その馬が3着外のときに残る券（`if_axis_out`）と、市場で3着内が最も濃い馬が3着外のときに残る券（`if_market_x_out`）
   - 3連複の各券の近似確率 `p_mkt_approx = 0.75 / オッズ`
   - scenario-frame の記録は、記録が見た版（`snapshot_ref.frozen_at` が同じ版）の raw で計算する。
   - **scenario-frame の監査は市場の監査だけ（`status: audited_market_only`、`audit_scope: market_only`）。** 正式な v1 記録として有効か（発走前のコミット・ルール不変・見た版が発走前など）の判定は `research.chat_experiment`（verify / settle）の担当で、その結果を `official_v1_valid` に写すだけにする。発走後に足した記録は `official_v1_valid: false` になり、集計（`summary.concentration.scenario_frame`）にも入らない。

あわせて `paired_win_logloss.json` に、race-performance-v1 の前向き評価の各レースから、Champion と市場の勝ち馬 log loss を同じレースで対にした値を、レース ID・除外理由・各レースの `evaluation_config_hash` と一緒に残す（#39 のレビューで求められた再現性）。
- `evaluation_code_last_commit_at_audit` は監査した時点でのコードの最終コミットで、各レースの出力を計算したときのコードとは限らない。
- 有意性は `significance: not_assessed`（事前登録した検定・区間推定と、同じ開催日のレースどうしの依存を検証するまで判断しない）。

## 近似の扱い（重要）

- `0.75 / オッズ` は、払戻率75%と市場の効率性を仮定した近似。すべての出力に `approx: true` を付ける。
- `trio_hit_prob_approx` は**3連複の券だけ**の近似的中確率。ワイド・馬連を含むカード全体の的中確率ではない。
- 未収録の組（1000倍超など）は「確率0」ではなく「不明」。未収録の組を含む券の集合には和を出さない（`trio_hit_prob_status: has_unlisted_combo`）。
- 全組で正規化した分布（proper な log loss に必要）は作らない。それは PR-B（市場だけの基準線）の担当。

## 実行

```
python -m research.market_audit              # data/research/market_audit/ に書く
python -m research.market_audit --out DIR    # 別の場所に書く（確認用）
```

`run_results.yml` の成績集計の後に、observe-only のステップとして毎週実行する（失敗しても止めない）。
