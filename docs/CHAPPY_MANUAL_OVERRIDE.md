# Chappy manual override

Chappyは通常、`logic/chappy.py` の動的シグナル統合で1000円カードを作る。

ただし、ChatGPTの会話で人間の文脈判断まで含めて買い目を組んだ場合は、
`data/chappy_manual/{race_id}.json` を置くとそのカードを**自動案より優先**して採用できる。

## 目的

2026-09-22 JRAアニバーサリーSでは、発走前の会話で次のように判断した。

- 12 = Win軸
- 14 = Top3妙味軸
- 3 = 能力側の補助軸
- 保険は薄く、14絡みへ資金を広げる
- 1000円だから「保険 + 本線 + Edge Wide + 3連複」を同時に持てる

実結果は 14→3→12 で、事前提示カードは1000円→11280円相当になった。

ただしこれは**設計の発想元になったレース**なので、
Top3/Chappyの将来成績を評価する検証データには使わない。
「こういう手動判断を構造化して保存したい」という要件定義の例としてだけ残す。

## ファイル形式

例:

```json
{
  "race_id": "20260927-nakayama-11",
  "author": "ChatGPT",
  "created_at": "2026-09-27T13:45:00+09:00",
  "roles": {
    "win_anchor": 12,
    "support": 3,
    "top3_edge": 14,
    "long_edge": 11
  },
  "rationale": [
    "12はWin側の軸",
    "14は同コース同距離の反復実績に対して市場価格が高い",
    "保険ワイドを100円に抑え、14絡みへ厚く配分"
  ],
  "bets": [
    {"type":"ワイド","horses":[12,14],"amt":200},
    {"type":"ワイド","horses":[3,12],"amt":100},
    {"type":"ワイド","horses":[3,14],"amt":100},
    {"type":"ワイド","horses":[10,14],"amt":100},
    {"type":"ワイド","horses":[11,12],"amt":100},
    {"type":"馬連","horses":[3,12],"amt":100},
    {"type":"3連複","horses":[3,12,14],"amt":100},
    {"type":"3連複","horses":[5,12,14],"amt":100},
    {"type":"3連複","horses":[10,12,14],"amt":100}
  ]
}
```

合計は必ず1000円、各amtは100円単位。

## 重要な運用ルール

1. 必ず発走前に作る。
2. 発走後に過去レースへmanual overrideを追加しない。
3. manual overrideも通常snapshotと同様に発走前freezeされる。
4. `source="manual_chat"` として結果に残す。
5. 自動Chappyと手動Chappyを後から分離集計できるようsourceを消さない。
6. 結果を見てrationaleやbetsを書き換えない。

## 鳳

manual overrideを置いても、鳳への昇格は原則として同じhigh-conviction gateを通す。
つまり手動カードだから鳳になるわけではない。

現時点の鳳gateは暫定で、以下を全て要求する。

- 妙味メーター閾値
- Chappy conviction
- data quality
- 参考的中率proxy
- 式別オッズ完全取得
- 未校正market EVが元本割れしていないこと（大きさは信用せずveto用途）

確率校正後は、真のP(hit) / portfolio EVに置き換える予定。
