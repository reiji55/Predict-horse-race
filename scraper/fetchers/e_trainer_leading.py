"""
Eページ：調教師リーディング

出典：取得項目_共通内部フォーマット仕様_v1.md §1.1（表の# E行）／§2.6（trainer_stats の型）
取得タイミング：当日朝 / 1週あたり1ページ
取れるもの：全厩舎の成績（着度数）

TODO（実装フェーズ・HTMLサンプル入手後）：
- netkeiba調教師リーディングページのURL形式
- 着度数のパース
- 出走馬の trainer_ref とリーディング表の紐付けキー
"""
from __future__ import annotations

from typing import Any


def fetch_trainer_leading(period: str = "2026") -> dict[str, dict[str, Any]]:
    """
    調教師リーディング（全厩舎分・全体成績）を一括取得する。

    period: 集計期間（年など）
    戻り値: {trainer_ref: trainer_stats(dict)} のマップ
            trainer_stats は 取得項目仕様§2.6 の型（scope="overall"）
    """
    raise NotImplementedError("HTMLサンプル入手後に実装（取得項目仕様§1.1 Eページ）")
