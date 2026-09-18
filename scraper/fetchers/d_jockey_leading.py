"""
Dページ：騎手リーディング

出典：取得項目_共通内部フォーマット仕様_v1.1.md §1.1（表の# D行）／§2.6（jockey_stats の型）
取得タイミング：当日朝 / 1週あたり1〜2ページ
取れるもの：全騎手の成績（着度数）を1ページでまとめて取得
  → 個別ページでなくリーディングページ一括方式（取得項目仕様§4-4で確定）

--- 実装の状況 ---

API呼び出しは `scraper/common/leading_api.py` に実装済み（D・Eで共有）。
**残るはレスポンスHTML断片のパーサーだけ**（`leading_api.parse_leading_html`）。
APIの仕様・発見の経緯はそちらのdocstringを参照。

--- ⚠ scope が "venue" から "overall" に変わった（OPEN_QUESTIONS B-3）---

取得項目仕様§2.6 は当初 騎手を `scope="venue"`（当該場成績）と定めていたが、
**netkeibaのリーディングに場別は存在しない**ことが実サンプルで確定した
（絞り込みは 全国/関東/関西＝所属、年、並び順のみ）。よって騎手も全国成績を使う。
仕様書§2.6 も改訂済み。`venue_jp` 引数は呼び出し側の互換のために残してあるが、
現状は**絞り込みには使われない**（将来、場別が取れる経路が見つかったときの受け口）。
"""
from __future__ import annotations

import logging
from typing import Any

from scraper.common import leading_api

logger = logging.getLogger("scraper.d_jockey_leading")

CATEGORY = "jockey"


def fetch_jockey_leading(venue_jp: str | None = None, period: str = "2026") -> dict[str, dict[str, Any]]:
    """
    騎手リーディング（全騎手分）を一括取得する。

    venue_jp: 当該場名。**現状は使われない**（場別リーディングが存在しないため。上記参照）
    period: 集計期間（年）
    戻り値: {jockey_ref: jockey_stats(dict)} のマップ
            jockey_stats は 取得項目仕様§2.6 の型（scope="overall", period, starts, wins, seconds, thirds）
    """
    if venue_jp:
        logger.debug("場別リーディングは存在しないため venue=%s は絞り込みに使いません", venue_jp)
    return leading_api.fetch_leading(CATEGORY, period)
