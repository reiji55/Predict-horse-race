"""
レート制限付きHTTP取得ヘルパー。

出典：取得項目_共通内部フォーマット仕様_v1.md §1.1「マナー設計」
- リクエスト間隔は最低3秒、ランダムゆらぎを入れる
- User-Agent を明示、リトライは1回まで
- パース失敗時は該当フィールドをnullにして続行し、ログに残す（全体を止めない）

各フェッチャー（scraper/fetchers/*.py）はこのモジュール経由でのみHTTP取得を行う。
"""
from __future__ import annotations

import logging
import random
import time

import requests

logger = logging.getLogger("scraper.http")

MIN_INTERVAL_SEC = 3.0
JITTER_SEC = 2.0  # 3〜5秒程度のゆらぎ
USER_AGENT = "weekend3cards-personal-use-bot/1.0 (individual, non-commercial)"
MAX_RETRIES = 1
TIMEOUT_SEC = 15

_last_request_at: float | None = None


def _wait_for_rate_limit() -> None:
    global _last_request_at
    if _last_request_at is not None:
        elapsed = time.monotonic() - _last_request_at
        wait_needed = MIN_INTERVAL_SEC - elapsed
        if wait_needed > 0:
            time.sleep(wait_needed)
    time.sleep(random.uniform(0, JITTER_SEC))


def _request(method: str, url: str, **kwargs) -> requests.Response | None:
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)

    attempts = MAX_RETRIES + 1
    for attempt in range(1, attempts + 1):
        _wait_for_rate_limit()
        global _last_request_at
        _last_request_at = time.monotonic()
        try:
            resp = requests.request(method, url, headers=headers, timeout=TIMEOUT_SEC, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.warning("取得失敗 (試行%d/%d) %s url=%s error=%s", attempt, attempts, method, url, exc)
    logger.error("取得を諦めました %s url=%s", method, url)
    return None


def get(url: str, **kwargs) -> requests.Response | None:
    """
    レート制限・UA明示・1回リトライ付きのGET。
    失敗時は None を返す（呼び出し側＝各フェッチャーが該当フィールドをnullにして続行する）。
    """
    return _request("GET", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response | None:
    """
    GETと同じマナー（間隔・UA・リトライ）を守るPOST。
    netkeibaのレース検索（`?pid=race_search_detail`）のようにフォーム送信が要る経路で使う。
    """
    return _request("POST", url, **kwargs)
