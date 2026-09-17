"""
Fページ：レース結果・払戻ページ

出典：取得項目_共通内部フォーマット仕様_v1.md §1.1（表の# F行）
取得タイミング：レース後 / 1週あたり4〜6ページ
取れるもの：確定着順、公式配当表 → results.json用（データスキーマ仕様v1.2 §5）

※ タスク6のスコープでは他フェッチャーと同じ枠組みで用意しておくが、
  results.json への変換自体はタスク7（成績集計）の仕事。

TODO（実装フェーズ・HTMLサンプル入手後）：
- netkeibaレース結果ページのURL形式
- 確定着順（馬番の配列）のパース
- 公式配当表（ワイド/馬連/3連複、dividends の型＝データスキーマ仕様§5）のパース
"""
from __future__ import annotations

from typing import Any


def fetch_results(race_source_ref: str) -> dict[str, Any]:
    """
    レース結果ページから確定着順・公式配当表を取得する。

    race_source_ref: netkeibaのレースID
    戻り値: {"finish": [...], "dividends": {...}}
            dividends の型は データスキーマ仕様v1.2 §5 を参照
    """
    raise NotImplementedError("HTMLサンプル入手後に実装（取得項目仕様§1.1 Fページ）")
