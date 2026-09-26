"""前向き検証（shadow / observe-only）の研究パッケージ。

ここに置くコードは、Champion・Challenger・自動Chappy・固定3人の**予想と買い目を一切変えない**。
発走前に凍結された snapshot（data/snapshots/）と、同じ発走前に記録した shadow 記録
（data/shadow/prerace/）だけを入力にし、結果確定後に data/shadow/ へ研究用の集計を書く。

- 正式な成績（data/results.json）・UI には混ぜない。
- 実験ごとに version と config_hash を残し、どのルールで作った数字かを再現できるようにする。
- 設定は config/shadow_research.json。Champion の config_hash（logic/model_registry.HASH_CONFIGS）には含めない。

設計と出力例: docs/SHADOW_RESEARCH.md
"""
