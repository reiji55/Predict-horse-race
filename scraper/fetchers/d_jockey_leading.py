"""
Dページ：騎手リーディング（場別）

出典：取得項目_共通内部フォーマット仕様_v1.md §1.1（表の# D行）／§2.6（jockey_stats の型）
取得タイミング：当日朝 / 1週あたり1〜2ページ
取れるもの：全騎手の当該場成績（着度数）を1ページでまとめて取得
  → 個別ページでなくリーディングページ一括方式（取得項目仕様§4-4で確定）

TODO（実装フェーズ・HTMLサンプル入手後）：
- netkeiba騎手リーディングページのURL形式（場別パラメータ）
- 着度数（1着/2着/3着/騎乗数）のパース
- 出走馬の jockey_ref とリーディング表の紐付けキー
"""
from __future__ import annotations

from typing import Any


def fetch_jockey_leading(venue_jp: str, period: str = "2026") -> dict[str, dict[str, Any]]:
    """
    指定場の騎手リーディング（全騎手分）を一括取得する。

    venue_jp: "小倉" のような日本語場名
    period: 集計期間（年など）
    戻り値: {jockey_ref: jockey_stats(dict)} のマップ
            jockey_stats は 取得項目仕様§2.6 の型
            （scope="venue", venue=venue_jp, period, starts, wins, seconds, thirds）
    """
    raise NotImplementedError("HTMLサンプル入手後に実装（取得項目仕様§1.1 Dページ）")
