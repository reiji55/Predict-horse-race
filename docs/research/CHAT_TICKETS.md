# チャットで出した券の記録（chat-ticket-v1）

- 書いた日：2026-11-03
- 書いた人：Claude
- 位置づけ：[EDGE_LOOP.md](EDGE_LOOP.md) §8.1 の「チャットで出した券の記録」。observe-only。
- 変えないもの：予想・正式成績・UI・Champion・固定3人・Chappy・scenario-frame/v1・事前登録済みの評価（portfolio-shadow-v1・edge-v1 を含む）。

## 1. 何のための記録か

開催日にチャットで出す本線（`edge-v0` = portfolio-shadow-v1 の `x_out_2`）の参考券を、出した時点で main にコミットしておく。

- 後から「チャットで出した券は、決め方どおりで、発走前に出ていた」と確かめられるようにする。
- 本線の成績そのものは、これまでどおり portfolio-shadow-v1 が発走前の最終の凍結版から計算する（この記録が無くても数えられる）。この記録の的中・払戻は報告だけで、portfolio-shadow-v1・edge-v1 の判定には入れない。
- チャットで出しても、ここに記録の無い券（合成 w = 0.3 などの未登録の参考）は、成績を数えない。

## 2. 開催日の手順

1. `git fetch origin main` で、main の今の版を取る。
2. `python -m research.chat_tickets make --race-id 20261107-tokyo-11 20261107-kyoto-11` を実行する。
   - main の今の版（`--rev`、既定は `origin/main`）を一時の作業ツリーに出し、発走前の最終の凍結版から、portfolio-shadow-v1 の登録コミットの engine（`research/portfolio.py` の `portfolio_race`）で `x_out_2` の券を計算する。
   - 計算は portfolio-shadow-v1 と同じ契約（発走前のオッズの時点の確認など）。除外になったレースや、`x_out_2` の組が選べないレースは見送り（`action: pass`、理由つき）として記録する。
   - もう発走しているレース、発走時刻が分からないレースは記録を作らない。
   - 記録は `data/chat_tickets/{race_id}/{YYYYMMDDTHHMMSS}.json`（JST の作成時刻）に書く。
3. 書いた記録を、発走前に **main に直接**コミットする（複数のレースを1つのコミットにしてよい）。ブランチに置いて後でマージすると、main に入った時刻で判定される。
4. チャットでは、記録の券だけを出す。記録の置き場所も添える。
5. 凍結版が更新された後（12:47 の定時実行など）に出し直すときは、もう一度 `make` して新しい記録をコミットする。前の記録は消さない・書き換えない。

## 3. 記録の中身

| キー | 中身 |
|---|---|
| `schema` | `chat-ticket-v1` |
| `race_id`・`version`・`strategy` | レース、`edge-v0`、`x_out_2` |
| `created_at` | 記録を作った時刻（JST） |
| `computed_from` | 計算に使った main の版（`rev`）、engine（`portfolio-shadow-v1`）とその登録コミット |
| `pinned`・`post_at` | 計算が見た発走前の凍結版（コミットと `frozen_at`）、発走時刻 |
| `action`・`reason` | `bet` か `pass`（見送りの理由） |
| `tickets`・`unit_yen`・`stake` | 3連複の組（馬番を小さい順、組も辞書順）、100円、投資 |
| `p_hit`・`x`・`proven_optimal` | 市場の確率での的中確率、軸 X、最適と示せたか |

数値・時刻・コミットだけを書く。取得元の本文・URL・リクエスト固有の値は書かない（`validate` で URL が無いことを確かめる）。

## 4. 確かめること

- **CI（`validate`）**：形・置き場所とファイル名（作成時刻）・券の並びと重複・投資の計算・見送りの理由・URL が無いこと・作成が発走前であること。Git の履歴は見ない。
- **成績集計（`audit`、`run_results.yml`、全履歴）**：履歴は、登録と同じく **main の first-parent** だけを見る（2026-11-03 の ChatGPT のレビュー）。
  - 対象は、main の first-parent 履歴に一度でも現れた記録のすべて（後で消したものも含む）と、作業ツリーにあるもの。main に無い記録は `not_in_main` で失格。
  - 記録が main に初めて現れたコミット（ブランチからのマージなら、そのマージコミット）の時刻が、発走時刻より前。ブランチで発走前にコミットしても、main に入ったのが発走後なら `committed_after_post`。
  - main に初めて現れた後に、main で一度も変えていない。消したら `deleted_after_commit`、書き換えたら（元に戻しても）`modified_after_commit`。中身は、main に初めて現れた版で確かめる。
  - 記録の `pinned` が、そのレースの発走前の版の1つ。`computed_from.rev` が、記録が main に初めて現れたコミットの祖先。
  - 決め方どおり：`computed_from.rev` の版から計算し直して、`action`・`tickets`・`pinned`・`p_hit`・`x`・`proven_optimal` が一致する（小数は 6 桁で比べる）。違う値の名前は `rule_differs` に残す。一度確かめた結果は `data/research/chat_tickets/verified.json` に残し、記録が変わらない限り計算し直さない。
  - 作成時刻と、main に初めて現れた時刻の差（`created_to_main_seconds`）は、事実として残すだけ（判定には使わない）。
  - レースごとに、発走前に main に最後に入った有効な記録を「出した券」とし、結果が出ていれば的中・払戻（報告だけ）を、本線の記録（portfolio-shadow-v1 の `x_out_2`）とは券と凍結版が同じかを残す。
  - 規約に合わない記録や、同じ凍結版なのに本線の記録と券が違うときは、警告にして終了コード 1（出力はコミットしてから失敗にする）。
- 出力は `data/research/chat_tickets/`（`summary.json`・`races/{race_id}.json`・`verified.json`）にだけ書く。

## 5. 既知の限界

- 時刻は Git のコミットの時刻（committer date）で、操作する人が設定できる。暗号的に発行時刻を保証するものではない。GitHub への push の記録（Actions の実行や PR の時刻）は、必要なときの追加の証跡として使う。

- チャットで出した券と記録が一致しているかは、チャットの文面を残さないので機械では確かめられない。記録の置き場所をチャットに添えて、人が照合できるようにする。
- 凍結版が後から更新されると、チャットで出した券と、本線の正式な記録（発走前の最終の凍結版）の券が違うことがある（`same_pinned: false`）。これは警告にしない。
