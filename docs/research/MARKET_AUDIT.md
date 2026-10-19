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
   - `post_race_odds`（どちらかが発走以降なら true）
   - 収録組数と全組数、Σ1/odds、`trio_mass_listed_approx`（= 0.75 × Σ1/odds。市場の確率質量のうち収録分の目安）
3. **現在の main の raw**：同じレースのオッズが発走後のものに書き換わっているか（`head_raw.post_race_odds`）。true のレースは、main の raw を学習・評価に使ってはいけない。
4. **券の依存度**（固定3人・チャッピーのカードと、scenario-frame の記録）：
   - 馬ごとの露出、最も露出の多い馬の割合
   - その馬が3着外のときに残る券（`if_axis_out`）と、市場で3着内が最も濃い馬が3着外のときに残る券（`if_market_x_out`）
   - 3連複の各券の近似確率 `p_mkt_approx = 0.75 / オッズ`
   - scenario-frame の記録は、記録が見た版（`snapshot_ref.frozen_at` が同じ版）の raw で計算する。

あわせて `paired_win_logloss.json` に、race-performance-v1 の前向き評価の各レースから、Champion と市場の勝ち馬 log loss を同じレースで対にした値を、レース ID・除外理由・評価コードのコミットと一緒に残す（#39 のレビューで求められた再現性）。

## 近似の扱い（重要）

- `0.75 / オッズ` は、払戻率75%と市場の効率性を仮定した近似。すべての出力に `approx: true` を付ける。
- 未収録の組（1000倍超など）は「確率0」ではなく「不明」。未収録の組を含む券の集合には和を出さない（`trio_hit_prob_status: has_unlisted_combo`）。
- 全組で正規化した分布（proper な log loss に必要）は作らない。それは PR-B（市場だけの基準線）の担当。

## 実行

```
python -m research.market_audit              # data/research/market_audit/ に書く
python -m research.market_audit --out DIR    # 別の場所に書く（確認用）
```

`run_results.yml` の成績集計の後に、observe-only のステップとして毎週実行する（失敗しても止めない）。
