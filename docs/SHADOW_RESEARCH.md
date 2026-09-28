# Shadow研究基盤（前向き検証・observe-only）

根拠: PR #11 の独立監査レポート（`docs/audit/CLAUDE_MODEL_AUDIT_REPORT_20260926.md`、PR #11 で保持）。

## 原則

- **Championの予想・重み・temperature・固定3人・Top3・自動Chappyは一切変えない。**
  shadow は発走前に凍結された snapshot を**読むだけ**です。
- **前向き検証。** `config/shadow_research.json` の `registered_at`（2026-09-27 00:00 JST）以降に発走したレースだけを `forward` として集計します。
  それ以前のレースは `retrospective` として分け、判定には使いません。
  例えば integrated 案は 9/26 を見た後に定義したので、9/26 は in-sample です。
- **正式成績に混ぜない。** 出力は `data/shadow/` だけです。`data/results.json` と UI は読みも書きもしません。
- **再現性。** 各出力に、実験ごとの `version`、`research_config_hash`（設定の SHA-256）、参照 snapshot の SHA-256 を残します。
  shadow の設定は Champion の `config_hash`（`logic/model_registry.HASH_CONFIGS`）に**入れません**。
- **ルールを変えたら `version` を上げる。** 既存の集計と混ざらないようにするためです。

## 流れ

```
[通常pipeline] build_predictions → snapshot凍結（既存・変更なし）
      └→ research.prerace_capture   発走前のレースだけ data/shadow/prerace/{race_id}/{時刻}_{phase}.json に追記
[成績集計]     build_results（既存・変更なし）
      └→ research.settle            data/shadow/races/{race_id}.json と data/shadow/summary.json
[CI]           research.decision_log validate（チャットChappy decision log の構造と規約）
```

どちらのステップも `continue-on-error: true` で、失敗しても予想本体と成績集計は止まりません。

`prerace_capture` は、本番と同じ `build_predictions.prepare_horses`（今回 build_race から切り出した関数）を使って、キャラ別の選定順を再計算します。
再計算した marks が snapshot と `num / score / odds / top3_score / top3_rank` の全列で一致しない場合は、`fidelity.recomputed_matches_snapshot=false` と不一致の列（`mismatches`）を残します。その場合、選定順は採点に使わず、snapshot 側（丸めたスコア）へフォールバックします（fail-closed）。
オッズまで比べるのは、固定3人の sel / value が単勝オッズにも依存するためです。スコアが同じでもオッズだけ更新された raw は不一致として扱います。

`settle` で使う入力（evidence）は次の優先順です。

1. `prerace_record_matched`：発走前記録があり、snapshot の SHA-256 が一致
2. `prerace_record_unmatched`：発走前記録はあるが、snapshot の版が違う
3. `derived_from_snapshot`：発走前記録が無く、凍結 snapshot から同じ規則で再構成

3 の場合、選定順の相関は計算できず（null）、較正には snapshot に丸めて保存されたスコア（0.1刻み）を使います。

---

## 1. 確率較正モニター（calibration-monitor-v1）

- **対象：** 各レースの全出走馬（勝ち馬＝1クラス）。
- **指標：** `log_loss = −ln p(勝ち馬)`、`brier = Σ(p_i − y_i)²`。

| モデル | 中身 | リーク対策 |
|---|---|---|
| `p_current` | 本番の `softmax(score/T_current)` | — |
| `q_market` | 単勝オッズの支持率 | — |
| `p_T{t}` | 事前固定の温度候補（4, 5, 6, 7, 8, 10, 12） | 結果を見て候補を選ばない |
| `p_entropy_match` | H(p)=H(q) となる温度 | 発走前のオッズだけで決める |
| `p_walk_forward` | 過去レースで平均 log loss が最小の温度 | **発走時刻が厳密に前**のレースだけ（同時刻の他場も除外）。8レース未満なら作らない |

- **評価対象の馬：** score と単勝オッズが両方ある馬に限り、p・q とも同じ集合で再正規化します。
  対象がフィールドの80%未満、または勝ち馬が対象外なら、そのレースは評価しません。
- **累積：** モデルごとの平均 log loss / Brier と、「log loss − 市場」の平均と標準誤差（同一レースでの対応比較）。

出力例（9/26 阪神11R、retrospective）：
```json
"models": {
  "p_current":       {"log_loss": 2.6025,  "brier": 0.941254, "p_winner": 0.074088, "temperature": 10.0},
  "q_market":        {"log_loss": 3.032525,"brier": 1.001028, "p_winner": 0.048194, "temperature": null},
  "p_T6":            {"log_loss": 2.690721,"brier": 1.013479, "p_winner": 0.067832, "temperature": 6.0},
  "p_entropy_match": {"log_loss": 2.608355,"brier": 0.951856, "p_winner": 0.073656, "temperature": 8.722416}
},
"walk_forward": {"history_races": 5, "temperature": null}
```

> retrospective 6レースでは、`p_current` の方が市場より log loss が小さく出ています（平均 −0.15、標準誤差 0.25）。
> 監査レポートの「p は平たすぎる」という見立てを、**現時点のデータは支持していません**。どちらも n=6 では判断できないので、forward で決めます。

## 2. Chappy shadow portfolio（chappy-shadow-v1）

| 案 | 4頭の選び方 | 9点pattern・金額 |
|---|---|---|
| `current_auto` | 現行（役割スコアで重複なし4頭）。手動カードのレースでは、凍結された roles から再構成 | 現行 |
| `integrated_top4_same_pattern` | `signal_board.integrated` の上位1〜4位を、役割順（win_anchor→support→top3_edge→long_edge）へそのまま割り当て | **現行と同一** |

統合（6シグナル）も点構成も同じなので、差は「役割スコアでの4頭選抜」だけです。

**記録する項目**
- レースごと：役割、買い目馬、払戻、的中点数、3着内カバー数、現行との重複頭数、軸への金額比率
- 累積：ROI、最高払戻の1レースを除いた ROI、的中レース数、同一レースでの払戻差（平均と標準誤差）

出力例（9/26 阪神11R、retrospective）：
```json
"integrated_top4_same_pattern": {
  "roles": {"win_anchor": 6, "support": 11, "top3_edge": 12, "long_edge": 8},
  "spent": 1000, "payout": 3970, "hit_bets": 1, "top3_capture": 2,
  "overlap_with_current": 3, "anchor_stake_share": 0.8
}
```

## 3. 固定3人の多様性モニター（diversity-monitor-v1）

ペア（ケイ-哲、ケイ-源、哲-源）ごとに、次を記録します。

- `sel_spearman`：選定順の順位相関（発走前記録が必要）
- `axis_match`：軸が同じか
- `jaccard`：買い目に出る馬の集合の Jaccard 係数
- `redundancy_flag`：軸一致かつ Jaccard ≥ 0.6

**観測のみです。似ていても何も変えません。**

```json
{"pair": "tetsu-gen", "sel_spearman": null, "axis": [9, 11], "axis_match": false, "jaccard": 0.6, "redundancy_flag": false}
```

## 4. 順位ズレ馬の前向き記録（rank-gap-v1）

- **定義：** base順位 ≤ 5 かつ 単勝人気順位 ≥ 6。
  どちらも snapshot の marks の**順位だけ**で決めるので、温度に依存しません。
- **期待値：** 単勝 q から Harville で出す3着内確率の和です。人気薄を低めに出す既知の偏りがあります。
- 自動購入条件にも、Chappy の加点にも**使いません**。

```json
{"num": 9, "name": "ルシュヴァルドール", "base_rank": 3, "market_rank": 14, "win_odds": 26.5,
 "q": 0.030007, "market_top3_prob": 0.097703, "top3": true}
```

累積では、次を記録します。

- レース数
- 登録のあったレース数
- 登録頭数
- 3着内に来た数
- 期待値
- 差（観測 − 期待）

## 5. チャットChappy decision log（chat-chappy-decision-v1）

- **保存先：** `data/chappy_decisions/{race_id}/{YYYYmmddTHHMMSS}.json`
- 1案1ファイルの追記のみで、既存ファイルは書き換えません。
- 正式Chappyカードの上書き（`data/chappy_manual/`）とは別物で、shadow 採点にだけ使います。

### 手順
1. `python -m research.decision_log template --race 20261004-nakayama-11 --out data/chappy_decisions/20261004-nakayama-11/20261004T150500.json`
   - snapshot から全出走馬の base順位・Top3順位・人気順位・固定3人の採用・順位ズレを埋めたひな形ができます。
2. `decision`（keep / drop）、`reason_code`、`bets`（各点に `hypothesis_tag`）を書きます。
3. `python -m research.decision_log validate <path>` で検証します（CI でも実行されます）。
4. **発走前に** GitHub へコミットします。

### 規約（validate が強制するもの）
- 出走馬を全頭並べます。順位・固定3人・順位ズレの列は snapshot と一致している必要があります（書き換え不可）。
- **順位ズレ馬**と、**固定3人が買っている馬**を `drop` するときは、`reason_code` が必須です（固定語彙）。
  「必ず買え」という制約ではありません。
  語彙: `insufficient_ability_evidence` / `insufficient_top3_evidence` / `data_missing` / `duplicate_rationale` / `condition_mismatch` / `budget` / `market_price_too_low` / `other`（`other` は note が必須）
- `keep` の馬は必ずどこかの買い目に入れます。`drop` の馬は買い目に入れられません。
- 券種はワイド・馬連・3連複、100円単位、合計1000円です。
- 各点に `hypothesis_tag`（`win_anchor` / `top3_value` / `ability_partner` / `rank_gap` / `insurance` / `other`）を付けます。
- `created_at` は発走時刻より前である必要があります。

### 発走前性の機械検証（`verify` と settle）
1. `created_at` < 発走時刻
2. **このファイルがGitに最初に追加されたコミットのコミット時刻** < 発走時刻
3. `snapshot_ref.sha256` と一致する snapshot の版が、現行または Git履歴にある
   - snapshot は発走前の再実行で上書きされるので、一致する版を Git履歴から取り出して JSON に戻し、**その参照版そのもの**と候補馬の事実（base順位・Top3順位・人気順位・固定3人・順位ズレ）を照合します（`facts_checked_against`: `current` / `git_history`）。
   - 参照先は log 側のパスを信用せず、`race_id` から決まる `data/snapshots/{race_id}.json` だけを見ます。

3つがそろったものだけを `prerace_verified=true` として累積に入れます。
`run_results` は全履歴（`fetch-depth: 0`）を取って検証します。

GitHub の API / Web（ChatGPT のコネクタ経由）で作られたコミットは、コミット時刻を GitHub が付けます。
手元からの `git push` はコミット時刻を偽装できるので、その場合は該当コミットの CI 実行時刻（GitHub Actions）と照合してください。

### 例（抜粋）
```json
{
  "schema_version": "chat-chappy-decision-v1",
  "race_id": "20261004-nakayama-11",
  "created_at": "2026-10-04T15:05:00+09:00",
  "author": "ChatGPT",
  "snapshot_ref": {"path": "data/snapshots/20261004-nakayama-11.json", "sha256": "…", "frozen_at": "…",
                   "model_id": "win-v1-speed-guard", "config_hash": "…"},
  "rank_gap_definition": {"version": "rank-gap-v1", "max_base_rank": 5, "min_market_rank": 6},
  "fixed_cards": {"kei": [1, 2, 3], "tetsu": [1, 2, 4], "gen": [3, 4, 5]},
  "candidates": [
    {"num": 3, "name": "…", "base_rank": 3, "top3_rank": 3, "market_rank": 8, "win_odds": 30.0,
     "in_fixed_cards": ["kei", "gen"], "rank_gap": true, "decision": "keep", "reason_code": null, "note": ""},
    {"num": 5, "name": "…", "base_rank": 5, "top3_rank": 5, "market_rank": 7, "win_odds": 25.0,
     "in_fixed_cards": ["gen"], "rank_gap": true, "decision": "drop",
     "reason_code": "insufficient_ability_evidence", "note": "近3走の同距離で着差が大きい"}
  ],
  "bets": [
    {"type": "ワイド", "horses": [1, 2], "amt": 400, "hypothesis_tag": "win_anchor"},
    {"type": "ワイド", "horses": [1, 3], "amt": 300, "hypothesis_tag": "rank_gap"},
    {"type": "3連複", "horses": [1, 2, 4], "amt": 300, "hypothesis_tag": "ability_partner"}
  ],
  "total": 1000,
  "summary": "…"
}
```

settle では、検証を通った案について次を記録します。

- 払戻
- 3着内カバー数
- **落とした馬のうち3着内に来た馬**（`dropped_top3_finishers`）

---

## 6. Forward diagnostics（forward-diagnostics-v1）

仕様: `docs/audit/FORWARD_DIAGNOSTICS_SPEC_20260927.md`。実装: `research/forward_diagnostics.py`、設定: `config/forward_diagnostics.json`。

- 出力は `data/shadow/diagnostics/{race_id}.json` と `data/shadow/diagnostics_summary.json` だけ。予想pipelineは読まない。
- **fail-closed:** 採点時の snapshot と SHA-256 が一致する、発走前（`captured_at < 発走`）の prerace record があるレースだけを診断する。無ければ `status=unavailable`（`prerace_record_unmatched` / `no_prerace_record`）で、snapshot や結果から選定を作り直さない。prerace record の fidelity が false なら `*_sel` だけ unavailable。
- 発走前: 固定3人の軸・軸依存額（軸を含む bet の額 / 固定3人の総額。3連複も軸を含めば数える）・sel 順位相関・買い目馬 Jaccard、speed_quality 要約と `model_running_as_designed`（design-check-v1）、各順位（base / Top3 / 各キャラ sel / Chappy integrated / roles / 買い目馬）。
- 結果確定後（監査専用）: 各順位の recall@4 / @6、カードごとの conversion（直接2頭馬券＝ワイド・馬連。3連複は数えない。同じ組は1つに正規化）、3連複化、Chappy の integrated → role → ticket 遷移、失敗段階ラベル。
- PASS カードは購入額・conversion の分母に入れない。

失敗段階ラベル（`cut_k=4`、`seen_k=6`）:

| ラベル | 単位 | 条件 |
|---|---|---|
| `feature_miss` | 馬 | どの順位（base / Top3 / 各 sel / integrated）でも6位以内にいない |
| | カード | そのカードの上流の順位（固定3人: base / Top3 / 自分の sel、Chappy: base / Top3 / integrated）で6位以内にいない |
| `base_rank_cut` / `top3_rank_cut` | 馬 | その順位で5〜6位（Top6 にはいたが Top4 圏外） |
| `character_selection_cut` | 固定3人カード | 上流で6位以内なのに買い目に不在 |
| `chappy_integration_cut` | Chappy | base / Top3 で6位以内、integrated で6位圏外、role にも不在 |
| `chappy_role_cut` | Chappy | integrated で6位以内なのに role に不在 |
| `ticket_conversion_loss` | カード | 3着内の2頭を選んでいたのに、その組の直接馬券が無い／3頭とも選んでいたのに3連複が無い（Chappy は role にいたのに買い目に不在も含む） |
| `no_structural_miss_detected` | 馬以外 | 上のどれにも当たらない |

ラベルは観測記録で、単一原因の断定ではない。少数標本のうちは良し悪しの判定に使わない。

---

## PR #12（Context Layers 監査基盤）との関係

- PR #12 は、Context 証跡（Prediction / Context / Odds を SHA-256 で束ねる manifest）と、`anomaly_report` の race_audits を担当します。
- 本基盤は、**モデル側の前向き検証**（較正・Chappy 構造・多様性・順位ズレ・チャットChappyの記録）を担当します。
- 役割が重ならないよう、`anomaly_review` と `audit_manifest` には触れていません。workflow でも別のステップです（pipeline では予想ビルド直後、results では Challenger 採点の後）。
- 同じ考え方（1観測1ファイルの追記、SHA-256、発走時刻ガード）は共有しています。
  判定には既存の `snapshots.post_datetime` をそのまま使っています。
- PR #12 がマージされた後は、decision log の `snapshot_ref` と同じ時点の manifest を突き合わせることで、証跡をさらに強化できます（今回は依存させていません）。

## 既知の限界

- 判定に意味がある件数になるまで時間がかかります（週2レース）。較正は1レース=全出走馬なので最も早く、ROI比較は数百レース規模が必要です。
- retrospective は仮説を作ったデータを含むので、判定に使いません。
- 発走前記録が無いレース（この基盤の導入前、またはステップ失敗時）では、選定順の相関が欠けます。
