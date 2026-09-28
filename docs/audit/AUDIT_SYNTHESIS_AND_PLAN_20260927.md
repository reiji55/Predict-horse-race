# 9/27 失敗監査 — ChatGPT × Claude 突合と次アクション

作成: 2026-09-27  
目的: 9/27 の結果に合わせて Champion を後付け調整するのではなく、独立監査の一致点・相違点を固定し、修正可能な設計/データ品質問題と forward 検証が必要な仮説を分離する。

## 0. ルール

- Champion の weights / λ / temperature / ticket template は、単日の結果だけでは変更しない。
- 既知問題の再現と純粋なデータ品質/計測問題は、結果と無関係な再現性があれば優先度を上げる。
- モデル出力を変える修正は Challenger / shadow で先に比較する。
- UI は「未較正のモデル内確率」をユーザーの実績的中率として見せない。
- キャラ一致は独立証拠の一致とみなさない。相関が高い場合は疑似コンセンサスとして扱う。

## 1. ChatGPT と Claude が独立に一致した指摘

### Critical 共通認識

1. **固定3キャラの疑似コンセンサス**
   - 9/27 は両レースで固定3人の軸が同一。
   - Claude 集計では固定3人 1,500円中 1,400円（93%）が1頭の着順に依存。
   - ケイ/哲の順位相関も高く、3人一致を「3つの独立した証拠」と読むことはできない。
   - 本来の「予想幅」を作る設計目的に対し、現状は十分に機能していない。

2. **speed が両レースで無効**
   - 阪神: qualified 0/16、coverage 0。
   - 中山: qualified 1/16、coverage 0.0625。
   - speed 無効時は残存因子へ重みが再配分され、特にケイ/哲の差が小さくなる。
   - 9/27 は「speed + aptitude + human」の設計どおりのモデルではなかった。

3. **馬選定から馬券化への conversion loss**
   - 阪神の源: 1着 #1 と3着 #8 を選定していたが、1-8 の券が無い。
   - 中山でも Top3/候補側で拾えていた馬が軸依存テンプレにより券にならない例がある。
   - 「馬を拾えなかった失敗」と「拾ったが券にできなかった失敗」を別指標で記録すべき。

4. **Chappy の missing signal = 0**
   - 中山2着 #6 は Top3/Recent が高い一方、条件データ欠損が condition=0 として入る。
   - 「悪い」と「未知」が同じ0なのは構造上の問題。
   - 既知問題の再現であり、Challenger 優先度を上げる。

5. **hit_pct は未較正**
   - 現在の hit_pct は未較正 win-p と Harville 近似による「カード内1点以上成立のモデル内推定」。
   - 実測の的中率ではない。
   - Chappy は6シグナル統合で選ぶ一方、hit_pct は common win-p で採点しており、モデル意味が一致しない。
   - ユーザー向け表示は実績ベースへ切り替え、hit_pct は研究用に残す。

6. **脚質/展開は候補だが、9/27だけで実装しない**
   - 中山の逃げ切りは仮説生成には使えるが、1レースの後知恵で Champion に追加しない。
   - データ収集は observe-only で開始可能。

## 2. Claude が独自に深掘りした重要点

### C2: 芝 speed 基盤の不足が想定以上に大きい
- Claude の再計算では、現行 base_times の芝データが中山中心で、阪神芝の過去走 67 本中 60 本に基準タイムが無い。
- 「9/27だけ speed が無効」ではなく、芝で speed が構造的に機能しにくい可能性。
- ケイの「スピード型」というキャラ定義そのものが芝では成立していない恐れがある。
- **最優先のデータ品質課題。**

### I4: 順位正規化で小差が軸入れ替えに増幅
- 中山のケイは sel 差約0.007で軸が #9 から #10 に入れ替わった。
- p-q の小さな差が value 順位化で離散的に増幅される。
- 「堅実型」が妙味順位に予想以上に引っ張られる構造を forward 比較する必要。

### I5: aptitude に近走重み/クラス補正が無い
- #16 の事例から生まれた新規仮説。
- 構造は事実だが、有効性は未検証。
- 過去検証 + forward Challenger が必要で、即修正は禁止。

### Otori gate の未較正 p 依存
- hit_pct_proxy だけでなく market_roi_veto 等も未較正 p に依存。
- 現状 concentration_shift=0 なので賭け金への直接影響はないが、鳳ラベル/高確信表現には影響する。
- UI/内部ロジック双方の監査対象。

## 3. ChatGPT 側で強く出していた点

1. **Top4だけでなく候補集合 Top6 までを別評価する**
   - 「モデルが全く見えていない」のか「圧縮で落ちた」のかを区別する。
   - horse selection recall@K と ticket conversion を別採点する。

2. **データ品質を推定確率と切り離して記録する**
   - speed unavailable のような品質低下がある日に、確率だけを強く見せない。
   - UIは実績値へ切り替えることで大部分を解消。

3. **manual Chat Chappy は multi-pillar の仮説を維持**
   - 単一◎集中を避ける考え方自体は保持するが、Champion rule へ昇格させない。
   - 手動判断は decision log で根拠を残す。

## 4. 意見が割れた/修正した点

### Chappy role layer
- 以前 ChatGPT は「integration より role conversion が問題」と強めに見た。
- 9/27 forward では、阪神で現行 role が2着 #4 を拾い、integrated top4 shadow は #4 を落とした。
- **結論: role layer を主犯扱いしない。**
- 引き続き shadow 比較で観測し、現時点では優先度を下げる。

### rank-gap
- 発走前には有望な監視仮説だったが、9/27 forward は阪神 #9 / 中山 #10 とも3着外。
- **結論: 採用も棄却もしない。observe-only 継続。**

## 5. 今すぐ直してよいもの（Champion予測を変えない）

### A. UI
1. 予想カードの「想定的中率 / 参考的中率」をユーザー向けから外す。
2. 代わりに実績ベース:
   - **実績的中率 33%**
   - **2 / 6 レース的中**
   - **検証数: 6レース**
   - 注記: 「これまでの予想で、買い目のうち1点以上が的中したレースの割合」
3. 回収率は別表示し、「当てる頻度」と「収益性」を分離。
4. 内部 hit_pct は削除せず研究/較正用に保存。

### B. 計測 / 監査
5. キャラ横断の疑似コンセンサス指標を保存:
   - 軸一致数
   - 1頭あたり総賭け金集中率
   - 選定順位相関
   - 買い目馬集合 Jaccard
6. conversion loss を保存:
   - selected_horses
   - selected_top3_count
   - pair_coverage_ratio
   - winning_pair_present_but_not_ticketed
7. candidate recall@K:
   - base/Top3/integrated/character selection の Top4 / Top6 に実着Top3が何頭入っていたか
8. speed coverage をレースごとに明示:
   - available / qualified / total / used / reason
9. Otori gate の各判定を継続ログし、未較正p由来の判定を区別する。

## 6. Challenger として実装するもの（Championは変えない）

### P0 — speed data foundation
1. 芝の base_times coverage を全競馬場・主要距離で棚卸し。
2. 取得元・算出方法・バージョン・更新日を固定。
3. base_times_v2 を Champion とは分離して作成。
4. 同じ発走前snapshotから speed-v2 Challenger を生成。
5. 旧Championと以下を比較:
   - log loss / Brier（十分な件数後）
   - 実着Top3 recall@K
   - キャラ間順位相関
   - ROI/的中は参考として別管理
6. 十分な forward sample まで Champion へ昇格しない。

### P1 — missing-neutral Chappy Challenger
- current: missing=0
- challenger: 「未知」を0点扱いしない。
- 方針は恣意的な0.5固定ではなく、欠損マスク + 観測信号への重み再配分 + data_quality penalty を第一候補とする。
- current と同じレースでshadow比較。

### P1 — character independence Challenger
- 無理に「違う馬を選ばせる」のではなく、各キャラの目的関数をより明確に分離。
- ケイ: 当てやすさ/再現性優先。小さなvalue順位差で軸が入れ替わらない設計候補。
- 哲: 能力とvalueの中間。
- 源: EV/人気薄/適性を優先。
- 成功条件:
  - ケイ/哲/源の順位相関が下がる
  - ただし individual performance を悪化させない
  - 「多様性のためだけの逆張り」は禁止。

### P1 — coverage-aware ticket Challenger
- horse selection と ticket construction を分離。
- 選んだ馬集合に対する pair coverage を計測。
- 500円/1000円固定のまま、軸1頭への過集中を緩めるテンプレ候補をshadow化。
- 特に哲/源の「選んだのに直接券が無い」を減らせるか検証。

### P2 — Otori gate v2 shadow
- 未較正 hit_pct_proxy を gate から外した版、または calibration 完了まで gate を研究用に限定した版を比較。
- market_roi_veto も p calibration とセットで評価。
- 鳳/高確信表示を「確率が正しい」と誤認させない。

## 7. observe-only で今から収集するもの

1. 脚質 / 通過順 / 前半3F / 上がり / 逃げ先行馬数 / ペース競合
2. 馬場状態 × 脚質
3. クラス補正・近走重みを検討できる過去走情報
4. 直前オッズ鮮度と最終観測時刻
5. 騎手/厩舎データ coverage
6. 馬体重 robust-z のサンプル数と安定性

これらは Champion に入れず、まず feature availability と結果相関を観測する。

## 8. forward 検証の昇格ルール

- 単日・数レースのROIで昇格しない。
- 比較対象は必ず同一レース・同一発走前情報の Champion vs Challenger。
- 事前登録したルール/バージョンを固定。
- 少なくとも以下を見る:
  - log loss / Brier（確率を使うモデル）
  - Top3 recall@4 / recall@6
  - card hit rate
  - ROI
  - pair coverage / conversion loss
  - character rank correlation
  - data quality coverage
- 変更ごとに一度に複数の要因を混ぜない。
- 昇格時は「何が改善したから昇格したか」を記録。

## 9. 実行順

### Phase 1 — 今すぐ
1. Claude監査と本突合レポートをmainに固定。
2. ユーザー向け hit_pct 表示を実績的中率へ変更。
3. diversity / concentration / conversion-loss / recall@K のobserve-only計測仕様を確定。

### Phase 2 — 次の実装PR
4. Claudeに「計測のみ」PRを実装してもらう。
5. ChatGPTが独立レビュー。
6. merge後、次レースからforward計測。

### Phase 3 — データ基盤
7. 芝speed coverage棚卸しとbase_times_v2設計。
8. pace/style等のobserve-only取得。
9. odds freshness / human coverage の監査。

### Phase 4 — Challenger
10. speed-v2。
11. missing-neutral Chappy。
12. character-independence。
13. coverage-aware ticket。
14. Otori gate v2。

### Phase 5 — 継続評価
15. 各レース後に anomaly + conversion + diversity + calibration を自動採点。
16. 週次で Challenger leaderboard を更新。
17. 十分なforward sample後にのみ Champion 昇格判断。

## 10. 9/27の扱い

9/27 は「モデルを今日の結果へ合わせる材料」ではなく、
**既知問題の再現確認と、今後のforward実験を決める起点**として保存する。
