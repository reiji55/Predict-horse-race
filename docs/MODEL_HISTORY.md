# MODEL HISTORY

予測モデルの改修履歴。UI変更や単純な表示修正はここには載せず、
**予想手法・選定ロジック・確率/妙味・馬券生成に影響する変更**だけを記録する。

コードそのものの正本はGit。ここは「何を・なぜ変えたか」を後から判断するための索引。

| Model ID | Status | Introduced | Main change | Rollback / source |
|---|---|---|---|---|
| `win-v1-speed-guard` | Champion | 2026-09-21 | sparse base-times による非対称なspeed減点を防止。ワイド/3連複の相手は従来Win側選定。 | PR #2 / merge `ce1afd0f` |
| `top3-partner-v1` | Challenger | PR #3 review | Win Scoreを変えず、ワイド/3連複の相手だけTop3適性で選ぶ。Kei=insurance, Tetsu=balanced, Gen=edge。 | branch `chatgpt/top3-place-model-20260921` / PR #3 |
| `chappy-signal-v1` | Integration layer | PR #3 review | Win/Top3/条件/近況/市場を動的統合する1000円枠。手動ChatGPTカードで上書き可能。高確信時は同じ枠が鳳へ昇格。 | branch `chatgpt/top3-place-model-20260921` / PR #3 |
| pre-speed-guard | Archived reference | before 2026-09-21 | sparse base-times のままspeedを部分利用。データ欠損の非対称問題あり。 | commit before `ce1afd0f` |
| `race-performance-v1` | Challenger | 2026-10-09T01:09:54+09:00（PR #29 のマージ。`registered_at`） | 事前登録した Race Performance（class＋margin・1因子）を4番目の因子として足す。重み speed 0.3825 / aptitude 0.2550 / human 0.2125 / RPS 0.15。RPS が使える馬が2頭未満・sd=0 のレースは Champion と同じ3因子に戻る。キャラ固有の3因子の重み・T・λ・テンプレは同じ。UIには出さない。 | `docs/research/RACE_PERFORMANCE_PREREG_V1.md` / PR #28（事前登録）・PR #29 merge `6e30d69f` |

## Change log

### 2026-09-20〜21 — speed guard

Motivation:
- オールカマーで、関連する芝好走がbase_times不足で使えず、悪いダート走だけが指数化される馬が不当に減点された。

Decision:
- same-surface only
- min usable runs = 2
- race coverage < 50% ならspeedを全馬OFF
- coverage十分時の欠損は平均補完（z=0）

Status:
- Championへ統合済み。

### 2026-09-21 — Top3 partner model

Motivation:
- 神戸新聞杯で、Win候補の軸評価（1・4）は良かった一方、
  「勝ち切りは弱いが距離条件で繰り返し3着以内に残る馬」をワイド/3連複相手として拾う仕組みが弱かった。

Decision:
- Win Score / win p は変更しない。
- Top3 Scoreは**確率ではなく相手候補ランキング**として追加。
- 馬連は従来Win側。
- ワイド/3連複の相手だけTop3モデルを使用。
- 本番へ即置換せずChallengerとして同時運転。

Status:
- PR #3でClaudeレビュー待ち。
- 将来の評価は2026-09-21結果への適合ではなく、導入後の共通レースで行う。

## Rule for future entries

新しい予測手法を入れるときは最低限以下を書く:

1. model id
2. 変更日
3. 変更理由
4. 変更した仮説
5. Champion / Challenger / Archived
6. 起点commit / PR
7. 元に戻す方法
8. 採用判断に使う比較期間・指標

**数レースの勝ち負けだけでChampionを入れ替えない。**


### 2026-09-22 — Chappy 1000円統合判断 / 鳳state

Motivation:
- 9/22 JRAアニバーサリーSの発走前手動分析で、12をWin軸、14をTop3妙味軸、3を能力側補助軸として1000円を配分。
- 実結果14→3→12となり、事前提示カードは1000円→11280円相当。
- ただしこのレースは設計の発想元なので、検証データには使わない。強い初期シグナル/要件定義例としてのみ記録。

Decision:
- Chappyを固定weightキャラではなく、複数シグナルの動的統合レイヤーにする。
- 通常1000円。保険・本線・Edge Wide・3連複を併存。
- 強い同コース同距離反復実績がある時だけcondition weightを動的boost。
- ChatGPT会話で作った発走前manual cardをruntime override可能にする。
- 鳳は別モデル/別追加カードではなく、Chappyのhigh-conviction state。
- 鳳gateは妙味・conviction・data quality・参考hit proxy・式別オッズcomplete・market ROI vetoを全て要求。
- Chappy/Otoriは固定モデルChampion/Challenger比較から除外し、独立成績として追う。

Status:
- PR #3でClaudeレビュー待ち。
- 本番未マージ。


### 2026-10-09 — race-performance-v1（Challenger）

Motivation:
- 現行の指標は着順を頭数で正規化して見ており、過去走の「どのクラスで・勝ち馬からどれだけ離されたか」を直接は見ていない。

Hypothesis（事前登録 PR #28・`docs/research/RACE_PERFORMANCE_PREREG_V1.md`・設定 SHA-256 `592cf78f…`）:
- レース格（class）と勝ち馬との差（margin_sec）を1つの成績内容因子（RPS：直近5走を距離の近さと新しさで重み付け）として足すと、
  登録後のレースの勝率予測 log loss が Champion より下がる。

Decision:
- 式・パラメータ・重みは事前登録のまま。結果を見て変えない。長期履歴・オッズ・着順は使わない。
- RPS が使える馬が2頭未満・sd=0 のレースは因子ごと無効にして、Champion と同じ3因子に戻す。
- 設定の SHA-256・version・既存3因子の重みが登録と違えば、その Challenger だけ作らず `data/challengers/_failures/` に記録する。

Status / rollback:
- Challenger（UI・正式成績には出さない）。`config/models.json` の `race-performance-v1` を外せば止まる。Champion は変わらない。
- 登録日時（`registered_at`）は PR #29 のマージ時刻 2026-10-09T01:09:54+09:00。forward の境界も同じ時刻（事前登録・評価式の有効日時 2026-10-09T00:26:18+09:00 より遅い）。

Evaluation:
- forward のみ（境界は事前登録・評価式・Challenger 登録の遅い方）。主指標は paired log loss、主要な副指標は Brier。
- 40レースは最初の確認点で、昇格の閾値ではない。
