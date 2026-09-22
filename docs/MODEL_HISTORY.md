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
