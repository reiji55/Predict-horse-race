# チャット実験（chat-experiment-record-v1）

チャット（ChatGPT など）で作った買い目のロジックを、**本番に入れる前に、前向きに試す**ための記録です。
実装: `research/chat_experiment.py`、設定: `config/chat_experiment.json`、ルールのひな形: `CHAT_EXPERIMENT_RULE_TEMPLATE.md`。

## なぜ要るか

結果を見たあとで作ったロジックは、その結果に合うようにできています（後付け）。
効くかどうかは、**まだ結果の出ていないレース**で、**ルールを変えずに**試さないと分かりません。
そのために、次の3つを Git の記録で示せる形で残します。

1. ルールは最初のレースより前に決めていて、そのあと変えていない
2. 各レースの買い目は、発走前に、そのルールどおりに作った
3. 比べる相手（現行のカード）は、同じ時点の発走前の情報から作られたもの

## 原則

- **本番に混ぜない。** 予想・正式成績（`data/results.json`）・UI は読みも書きもしません。採点は `data/shadow/chat_experiments/` にだけ書きます。
- **ルールは書き換えない。** 変えたくなったら新しい version（`rule_v2.md`）を足します。集計は version ごとに分かれ、混ざりません。
- **何レースで評価するかを先に決める。** ルールに書いたレース数に達するまで、結論を出しません。少ないレース数では偶然の影響が大きいためです。
- **見送り（pass）も記録する。** 買わないレースも、ルールどおりの判断として数えます。

## 置き場所

| もの | パス | いつ |
|---|---|---|
| ルール | `data/chat_experiments/{experiment_id}/rule_{version}.md` | 最初のレースより前に1回だけ追加。以後は触らない |
| 記録（1レース1案） | `data/chat_experiments/{experiment_id}/races/{race_id}/{YYYYmmddTHHMMSS}.json` | 各レースの発走前。出し直すときは新しいファイルを足す |
| 採点 | `data/shadow/chat_experiments/{experiment_id}/summary.json` | `run_results` が自動で作る |

- `experiment_id` は英小文字・数字・ハイフン（例 `scenario-frame`）、`version` は `v1`, `v2` …。
- 対象は、パイプラインが予想したレース（`data/snapshots/{race_id}.json` があるレース）だけです。比べる相手のカードと発走時刻を、その snapshot から取るためです。
- ファイル名の時刻は、作った時刻（日本時間）です（例 `20261011T143000.json`）。

## 手順

1. **ルールを書く。** `CHAT_EXPERIMENT_RULE_TEMPLATE.md` を写して埋め、`rule_v1.md` として**最初のレースより前に**コミットします。
2. **パイプラインを回す。** その日の snapshot（`data/snapshots/{race_id}.json`）ができます。
3. **記録を作る。** snapshot を見て、出走馬の全頭に枠（ルールで決めたラベル）と根拠を付け、ルールどおりに買い目を組みます。
   `snapshot_ref.frozen_at` には、見た snapshot の `frozen_at` をそのまま写します。
4. **発走前にコミットする。** GitHub の Web 画面（Add file → Create new file）か ChatGPT のコネクタで、`main` に直接コミットします。
   - 手元からの `git push` は使いません。コミット時刻を偽装できるので、証拠になりません。
   - ブランチを経由するなら、マージはマージコミットで行います（スカッシュ・リベースはコミット時刻が変わります）。
5. **出し直すとき**（パイプラインを回し直してオッズが変わった、など）は、新しいファイルを足します。発走前にコミットされた最後の案が採点されます。
6. 結果が出たら、`run_results` の「チャット実験の採点（observe-only）」が自動で採点します。

手元での確認:
- `python -m research.chat_experiment validate [path]` … 構造と規約（CI でも実行）
- `python -m research.chat_experiment verify [path]` … それに加えて、Git の記録で発走前性
- `python -m research.chat_experiment settle --results data/race_results.json` … 採点

## 記録の形式

```json
{
  "schema_version": "chat-experiment-record-v1",
  "experiment_id": "scenario-frame",
  "rule_ref": {"path": "data/chat_experiments/scenario-frame/rule_v1.md", "version": "v1"},
  "race_id": "20261011-tokyo-11",
  "created_at": "2026-10-11T14:30:00+09:00",
  "author": "ChatGPT",
  "snapshot_ref": {"path": "data/snapshots/20261011-tokyo-11.json", "frozen_at": "2026-10-11T13:20:05+09:00"},
  "baseline_char": "gen",
  "budget": 500,
  "action": "bet",
  "pass_reason": null,
  "candidates": [
    {"num": 1, "frames": ["先行残り"], "evidence": "前走は同じ距離で逃げて0.2秒差。今回も単騎の見込み"},
    {"num": 2, "frames": [], "evidence": ""},
    {"num": 3, "frames": ["差し届く"], "evidence": "上がり最速が直近3走で2回"}
  ],
  "bets": [
    {"type": "ワイド", "horses": [1, 3], "amt": 300, "frame_tag": "先行残り"},
    {"type": "馬連", "horses": [1, 3], "amt": 200, "frame_tag": "差し届く"}
  ],
  "summary": "…"
}
```

| 項目 | 内容 |
|---|---|
| `created_at` | 作った時刻（日本時間、`+09:00` 付き）。発走より前 |
| `snapshot_ref.frozen_at` | 見た snapshot の `frozen_at` を**そのまま**写す。同じ時刻の版を Git 履歴から探して照合する |
| `baseline_char` | 比べる相手（`kei` / `tetsu` / `gen` / `chappy`）。**ルールで1つに決め、version の中では変えない** |
| `budget` | 100円単位・1000円以下。`bet` なら買い目の合計と一致 |
| `action` | `bet`（買う）/ `pass`（見送り。`bets` は `[]`、`pass_reason` が必須） |
| `candidates` | snapshot の出走馬を**全頭**（取消・除外も含む）。`frames` は枠のラベルのリストで、どの枠にも入らない馬は `[]`。枠に入れた馬は `evidence` が必須（入れない馬は省いてよい） |
| `bets` | ワイド・馬連・3連複、100円単位。**買えるのは枠に入れた馬だけ**。`frame_tag` は、どの枠から作った券か |

枠のラベル（`frames`・`frame_tag`）は、ルールファイルに書いた文字列をそのまま使います（ルールに無いラベルはエラー）。

## 検証

`validate`（CI。Git の時刻は見ない）:
- 上の表の規約
- 出走馬の網羅を、記録が見た版の snapshot と照合する（版が見つからないときは照合を省いて注意を出す）
- `created_at` < 発走時刻
- ルールファイルがあり、使った枠のラベルがルールに書かれている
- 置き場所が `experiment_id`・`race_id` と合っている
- クエリ（`?` 以降）・`#`・ユーザー情報つきの URL が無い（公開リポジトリのため）

`verify` と採点（`run_results` は全履歴を取って検証します）:
1. `validate` に通る
2. 記録に触れたコミット（追加・修正）が、すべて発走時刻より前
3. ルールファイルに触れたコミットは**追加の1回だけ**で、その時刻 ≤ 記録の最初のコミット時刻。書き換え・削除があると、その version の記録はすべて検証に通りません
4. `frozen_at` が同じ snapshot の版が Git 履歴にあり、発走前に凍結された版で、そのコミット時刻 ≤ 記録の最初のコミット時刻

すべて通った記録（`prerace_verified`）だけを採点します。

## 採点の出力（`summary.json`）

- **version ごとに分けます**（`versions.v1`, `versions.v2` …）。
- レースごとに、発走前にコミットされた最後の案を採点します（それより前の案は `superseded`）。

| 項目 | 内容 |
|---|---|
| `experiment` | 購入額・払戻・回収率・的中レース数・見送り数・3着内の馬を買い目に入れた数 |
| `primary_baseline` | 記録が指定した比べる相手のカード（同じレース・記録が見たのと同じ版の snapshot） |
| `cards` | 固定3人・Chappy の全カード（参考） |
| `paired_payout_diff_vs_primary_baseline` | レースごとの払戻の差（実験 − 比べる相手） |
| `roi_ex_max_payout_race` | 払戻が最大のレースを除いた回収率（1本の大当たりに引っ張られていないか） |
| `top3_finishers` | 3着内の馬のうち、どれかの枠に入っていた割合（買い目とは別に、枠の当たり方を見る） |
| `by_frame_tag` | 枠ごとの券の数・的中・購入額・払戻 |
| `races[].bet_horses_finish` | 買った馬の着順（軸が大敗したのか、相手が抜けたのか） |
| `rejected_records` | 検証に通らなかった記録と理由（採点には使っていない） |
| `deleted_records` | Git で削除された記録。`after_post: true` は発走後に消したもの |

根拠の文章（`evidence`・`summary`・`pass_reason`）は写しません。

## 公開リポジトリの注意

- リポジトリは公開です。記録に、URL のクエリ（`?` 以降）・`#`・ログイン情報、サイトの文章の丸写し、個人的な情報を書かないでください。
- 根拠は自分の言葉で短く書きます。

## 既知の限界

- 少ないレース数では、良いロジックでも負け、悪いロジックでも勝ちます。ルールに書いたレース数に達するまで、結論を出しません。
- `created_at` は自己申告です。証拠になるのは Git のコミット時刻です。
- ルールの文章の解釈の幅は、機械では検証できません。条件・同点のときの決め方・頭数の上限を、できるだけ具体的に書きます。
- 比べる相手のカードも、まだ前向きの実績が少ないモデルです。「現行より良いか」の比較であって、「儲かるか」の証明ではありません。
