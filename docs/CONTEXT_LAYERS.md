# Context Layers v1 — 当日情報を壊さず増やす設計

## 目的

競走結果を「人気薄が来た＝運」で終わらせず、

- 事前に観測できた基礎能力
- 当日に観測できたコンディション
- 発走直前に観測できた生体/市場シグナル
- 発走後にしか分からない出来事

を分離して、**事前に拾えたはずの情報だけを後方検証する**。

「原因が存在する」ことと「発走前に予測可能だった」ことは別なので、
結果を見た後の説明を予測力として数えない。

## 3レイヤー

### Layer 1 — 基礎能力（本番で使用中）

既存の Champion / Challenger が担当。

- Speed
- Aptitude
- Human
- Top3
- 市場との乖離
- Race regime

ここだけが現在の買い目へ直接影響する。

### Layer 2 — 当日コンディション（observe_only）

v1で収集開始:

- 当日馬体重
- 過去走の馬体重
- 各馬自身の馬体重中央値からの逸脱
- 前走からの日数 / quick return / layoff
- JRA公式の芝クッション値
- 芝/ダート含水率
- 単勝オッズの時系列変化

**まだbase_score / p / 妙味 / 買い目には加点しない。**

馬体重は絶対値や「前走比-10kg」だけでは判定せず、
その馬自身の過去レンジを基準にする。

### Layer 3 — パドック/直前生体所見（observe_only）

構造化する項目:

- gait_symmetry
- stride_fluency
- sweat
- agitation
- coat_condition
- confidence
- notes

生データの動画をそのまま予想モデルへ流し込まない。
人間・ChatGPT Vision・将来の許諾済みCV処理が、
同じ0〜1特徴量へ変換した後で保存する。

JRA/配信者の映像を無断でダウンロード・再配布する仕組みは持たない。
ユーザーが利用権を持つ画像/動画、ユーザー提供素材、または許諾済み入力から
特徴量を抽出する経路を前提にする。

## 情報量をどう処理するか

1レースを18頭としても、通常対象は1日2〜3レース程度。
構造化後は1頭あたり数十個以下の数値なので計算量は極小。

重いのは動画そのもの。
将来の自動動画処理では、

video
→ frame sampling
→ horse identification / tracking
→ pose/keypoint extraction
→ short-window aggregation
→ 5〜10個程度の特徴量
→ context_layers

という前処理を行い、LLMが長時間動画全体を毎回読む構成にはしない。

## 発走前凍結

予想snapshotとcontext snapshotは分離する。

- `data/snapshots/{race_id}.json`
  - 印・買い目・妙味
  - 結果を見た後には書き換えない

- `data/context_snapshots/{race_id}/{timestamp}_{phase}.json`
  - 発走前に増えた当日情報
  - pipeline / lateを別ファイル化
  - 1観測1ファイルでpush競合を避ける

直前contextが増えても、既に作ったカードは変更しない。

## 馬体重

既存の出馬表パーサーは当日馬体重を既に取得できる。

今回さらに過去5走ページの

`3-3 (39.1) 510(0)`

から historical body weight を保存する。

特徴量は:

- current
- last
- historical median
- change_from_last
- deviation_from_median
- deviation_pct
- robust_z (MAD)
- unusual flag

初期閾値は研究用で、予想への減点根拠ではない。

## JRA馬場定量値

JRA公式の馬場情報から、明確に読めるものだけ取得する。

- cushion value
- turf moisture: goal / turn4
- dirt moisture: goal / turn4

HTML変更などで値が明確に読めない場合は null。
基準目盛り等を実測値と推測して埋めない。

## オッズ推移

既存 odds_history を以下へ圧縮:

- first_odds
- last_odds
- odds_ratio
- support_shift = log(first / last)

support_shift > 0 は「時間とともに支持が強くなった」を表すだけで、
「情報通が買った」とは解釈しない。

## パドック

`data/paddock/{race_id}.json` を共通入力契約にする。

現段階:
- 手動観察
- ユーザー提供画像/動画をChatGPT Visionで評価
- 許諾済み外部CV出力

将来:
- horse tracking
- gait keypoints
- 左右差
- 歩幅/歩調の安定性
- 頭頸部の動き
- 発汗/落ち着き等の視覚特徴

を自動化する余地を残す。

## 自己成長ループ

レース後 `results.anomaly_review` が、

- 3着以内
- 事前モデル順位 >= 6位 または市場人気 >= 6位

の馬を「想定外好走馬」として抽出する。

同時に、その馬の発走前contextを添える。

例:

- model_miss
- market_miss
- body_weight unusual?
- layoff?
- track metrics?
- late odds support?
- paddock observation?

これらはすべて **hypothesis_only**。

同じ仮説が繰り返し現れたら、
新しい特徴量をChallengerへ実装して発走前検証する。

## 昇格ルール

observe_only
→ evidence accumulates
→ Challenger
→ pre-race frozen live test
→ enough common-race comparisons
→ reproducible improvement
→ Champion candidate

自動でChampionの係数を書き換えない。
