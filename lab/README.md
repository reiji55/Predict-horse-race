# lab — AI競馬確率研究環境

週末3CARDS（本番アプリ）とは分離した研究環境です。
目的・設計・ロードマップは `docs/RESEARCH_DESIGN_v0.md`、過去データの取得は `docs/DATA_ACQUISITION_v0.md` を参照してください。

## 隔離ルール

- lab から本番（`logic/` `scraper/` `results/` `research/` `config/` `data/` `raw/` と既存 workflow）へは**書き込まない**。
- Champion `win-v1-speed-guard` と scenario-frame/v1 には変更を加えない。既存の snapshot は読むだけ。
- 浦和Chappyプロジェクトのデータ・コード・設定は持ち込まない。対象は JRA 平地競走のみ。
- 取得したデータは解析済みの JSON/CSV として `lab/data/` にコミットする。netkeiba のページの HTML とキャッシュはコミットしない。
- netkeiba からの取得は GitHub Actions の lab 専用 workflow で、本番と同じマナー（間隔3秒以上・UAを偽らない・並列にしない）で行う。

## 役割

- Claude: 実装・データ処理
- ChatGPT: 研究方針・仮説・独立検証・コード監査
