# lab — AI競馬確率研究環境

週末3CARDS（本番アプリ）とは分離した研究環境です。
目的・設計・ロードマップは `docs/RESEARCH_DESIGN_v0.md` を参照してください。

## 隔離ルール

- lab から本番（`logic/` `scraper/` `results/` `research/` `config/` `data/` `raw/` と既存 workflow）へは**書き込まない**。
- Champion `win-v1-speed-guard` と scenario-frame/v1 には変更を加えない。既存の snapshot は読むだけ。
- 浦和Chappyプロジェクトのデータ・コード・設定は持ち込まない。対象は JRA 平地競走のみ。
- 大容量・再配布できないデータは `lab/data/` に置き、Git には入れない。Git にはコード・設定・事前登録・集計結果・データの SHA-256 だけを残す。

## 役割

- Claude: 実装・データ処理
- ChatGPT: 研究方針・仮説・独立検証・コード監査
