"""
results.json ビルドスクリプト（データスキーマ仕様_v1.2.md §5）

※ タスク7（成績集計）は今回のスコープ外。ディレクトリと入出力の型だけ先に置いておく。

流れ（引き継ぎ書v3 §4.1）：
  Fページ（scraper.fetchers.f_results）で確定着順・公式配当表を取得
  → dividends（買い目非依存の生データ）を保持
  → predictions.json の cards[] と突き合わせて的中判定・payout計算
  → 払戻の4不変条件を検証（データスキーマ仕様§5「払戻の計算ルール」）
  → results.json 書き出し

TODO（タスク7着手時）：
- 券種別集計フィールドの詳細（データスキーマ仕様§7の宿題）
- 4不変条件の検証実装
"""
from __future__ import annotations


def build_results(week_id: str) -> dict:
    raise NotImplementedError("タスク7（成績集計）着手時に実装")
