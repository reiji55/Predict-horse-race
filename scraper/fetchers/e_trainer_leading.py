"""
Eページ：調教師リーディング

出典：取得項目_共通内部フォーマット仕様_v1.1.md §1.1（表の# E行）／§2.6（trainer_stats の型）
取得タイミング：当日朝 / 1週あたり1ページ
取れるもの：全厩舎の成績（着度数）

--- 実装の状況 ---

`https://db.sp.netkeiba.com/trainer/trainer_leading.html` の実サンプルで確認したとおり、
**Dと完全に同じ作り**で `category` が `trainer` になるだけ（インラインJSの
`var category = 'trainer';` 以外はD版と同一）。実装は `scraper/common/leading_api.py` に
共通化してある（D・Eで共有）。APIの仕様・**着度数が載っていないので率から逆算していること**は
そちらのdocstringを参照。
"""
from __future__ import annotations

from typing import Any

from scraper.common import leading_api

CATEGORY = "trainer"


def fetch_trainer_leading(period: str = "2026") -> dict[str, dict[str, Any]]:
    """
    調教師リーディング（全厩舎分・全体成績）を一括取得する。

    period: 集計期間（年）
    戻り値: {trainer_ref: trainer_stats(dict)} のマップ
            trainer_stats は 取得項目仕様§2.6 の型（scope="overall"）
    """
    return leading_api.fetch_leading(CATEGORY, period)
