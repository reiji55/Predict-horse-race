# Forward Diagnostics Spec — 9/27監査後の計測基盤

作成: 2026-09-27  
実装担当想定: Claude  
レビュー: ChatGPT  
モード: **observe-only / Champion出力不変**

## 1. 目的

今後の各レースで、外れたあとに目視で原因を探すのではなく、
以下を発走前snapshotと確定結果から自動判定する。

1. キャラが本当に別の仮説を見ているか（疑似コンセンサス）
2. 馬は拾えていたのに買い目化で落としたか（conversion loss）
3. Top4圧縮で落ちたのか、Top6にもいなかったのか
4. speed / data quality が十分だったか
5. Chappy integrated / role / ticket のどの段階で落ちたか

このPRでは**予測・選定・買い目・重みを一切変更しない**。

## 2. 出力先

正式成績・UIと分離する。

- race-level:
  - `data/shadow/diagnostics/{race_id}.json`
- cumulative:
  - `data/shadow/diagnostics_summary.json`

既存の `data/results.json`、`data/predictions.json`、カード、marks、UI表示値を変更しない。

## 3. 発走前証拠の拘束

- `data/shadow/prerace/{race_id}/...` のうち、settle対象snapshot SHAと一致する最新の発走前recordだけを使う。
- 一致するprerace recordが無ければ、再計算で埋めない。該当指標は `unavailable` としてfail-closed。
- post-time生成物をpre-race selectionの代用品にしない。
- snapshot SHA / capture timestamp / post time をrace diagnosticに保存。

## 4. Pre-race diagnostics

### 4.1 Fixed-three pseudo-consensus

ケイ/哲/源について保存:

```json
{
  "fixed_three": {
    "axes": {"kei": 1, "tetsu": 1, "gen": 1},
    "axis_counts": {"1": 3},
    "max_axis_agreement": 3,
    "dominant_axis": 1,
    "dominant_axis_exposure_yen": 1400,
    "total_stake_yen": 1500,
    "dominant_axis_exposure_ratio": 0.9333,
    "pairwise_rank_spearman": {},
    "bet_horse_jaccard": {},
    "redundancy_flag": true
  }
}
```

**axis exposure の定義**:
固定3カードの各betについて、dominant_axis を含むbetの購入額を合算 / 固定3人の総購入額。
3連複100円も、軸を含めば100円依存として数える。

これは「その馬に100円賭けた」という意味ではなく、
**その馬が必要な買い目に何円依存しているか**。

### 4.2 Speed / input quality

snapshotの `speed_quality` を複製せず参照要約:

- raw_available_horses
- qualified_horses
- total_horses
- coverage
- used
- reason

さらに race-level flag:

- `speed_degraded = used !== true`
- `model_running_as_designed = speed used + required input gates`

「model_running_as_designed」は後から拡張可能なversioned判定にする。

### 4.3 Candidate orders

発走前recordに存在する順位をそのまま保存/参照:

- base order
- Top3 order
- kei sel order
- tetsu sel order
- gen sel order
- Chappy integrated order
- Chappy role horses
- actual ticket unique horses

再計算とsnapshotが不一致ならprerace_captureの既存fidelityルールに従いfail-closed。

## 5. Post-race diagnostics

確定 `finish[:3]` を使って以下を測る。
これは**監査用だけ**で、将来予測の入力にはしない。

### 5.1 Recall@K

各rank sourceについて:

- recall_top3_at_4 = actual top3のうちTop4に含まれる頭数 / 3
- recall_top3_at_6
- count_at_4
- count_at_6

対象:
- base
- top3
- kei_sel
- tetsu_sel
- gen_sel
- chappy_integrated

例:
```json
"recall": {
  "base": {"at4": 0.3333, "count4": 1, "at6": 0.6667, "count6": 2}
}
```

### 5.2 Selection vs Ticket conversion

各カードについて:

- `selected_horses`: 当該カードに登場するunique horses
- `actual_top3_selected_count`
- `direct_pairs_available`: selected_horsesの全 unordered pair
- `direct_pairs_ticketed`: WIDE/馬連として直接購入したunique pair
- `direct_pair_coverage_ratio`
- `actual_top3_pairs_selected`
- `actual_top3_pairs_directly_ticketed`
- `winning_pair_present_but_not_ticketed`

ここで3連複はdirect pair ticketには数えない。
「2頭とも選んでいたが、その2頭だけで成立する馬券を持っていなかった」を測るため。

### 5.3 Trio conversion

- actual top3 3頭が selected_horses に全て存在したか
- その3連複を実際に買っていたか
- `winning_trio_selected_but_not_ticketed`

### 5.4 Failure stage

race/cardごとに後方監査ラベルを付ける。
単一原因とは限らないので配列にする。

候補:
- `feature_miss`
- `base_rank_cut`
- `top3_rank_cut`
- `character_selection_cut`
- `chappy_integration_cut`
- `chappy_role_cut`
- `ticket_conversion_loss`
- `no_structural_miss_detected`

判定ロジックをコードコメント/テストで明文化する。
「結果馬が何位ならmiss」といった閾値は、まず@4/@6の事前定義に限定する。

## 6. Chappy stage audit

Chappyだけは別に:

- integrated top4 / top6 actual-top3 recall
- role horses actual-top3 count
- ticket unique horses actual-top3 count
- integratedにいたがroleで消えた馬
- roleにいたがticket pair/trioで使い切れなかった馬

**注意:** roleを悪者と決めつけない。純粋にstage transitionを記録する。

## 7. Aggregate summary

`diagnostics_summary.json` に最低限:

- races
- fixed_three_axis_all_same_count / rate
- mean dominant_axis_exposure_ratio
- pairwise mean Spearman
- speed_used_races / rate
- source別 mean recall@4 / recall@6
- char別 mean direct_pair_coverage
- char別 conversion_loss_count
- Chappy stage-drop counts

少数標本で良し悪しを断定する文言は入れない。

## 8. Tests

必須fixture:

1. 阪神9/27
   - fixed3 dominant axis #1
   - high exposure
   - Gen selected #1/#8 but direct 1-8 absent → conversion loss
   - Chappy #1/#4 direct pair present

2. 中山9/27
   - fixed3 dominant axis #10
   - Top3 orderが #5/#6 を比較的上位に持つ
   - candidate recall@6 > recall@4 となるsourceを正しく記録
   - missing conditionそのものはこのPRで修正しない

3. snapshot SHA不一致
   - pre-race指標はfail-closed
   - post-raceデータからselectionを再構築しない

4. PASS card
   - stake/conversion denominatorに誤って加えない

5. 同一pair重複
   - unique pairとして1つに正規化

## 9. Workflow integration

`run_results.yml` のsettle後にdiagnostics生成を追加。

- 研究用なので `continue-on-error: true` 可
- ただしエラーをsilentにしない。ログにrace_idと理由。
- `git add data/` 既存フローで保存。
- prediction pipelineでは結果を読まない。

## 10. 非目標

このPRでは以下を禁止:

- weight変更
- λ変更
- temperature変更
- CARD_TEMPLATES変更
- Chappy role変更
- missing=0変更
- base_times追加
- pace/脚質モデル追加
- Otori gate変更
- UI変更

## 11. Acceptance criteria

- Champion `data/predictions.json` がPR前後でbyte-identicalになるfixture/testを置くか、既存不変テストを通す。
- 9/27両レースのdiagnostic fixtureが期待値どおり。
- post-race leakがprediction pathに入らない。
- CI全成功。
- 生成物は `data/shadow/diagnostics*` のみ。
