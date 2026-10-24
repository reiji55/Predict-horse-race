"""
netkeibaの調教ページ（race.netkeiba.com/race/oikiri.html）の、有料会員でなくても見られる部分を読む。

2026-10-24 の確認（manual_training_check）で、無料で見られるのは
  td.Training_Critic …… 短い調教評価（例「動き軽快」）
  td[class^=Rank_] …… その横の記号（評価のランクとみられる）
で、調教タイムは見られない（会員向け）。

予想ロジック（logic/）はまだ使わない。過去分をためて、式に足して効くかを検証する（docs/TRAINING_EVAL.md）。
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from bs4 import BeautifulSoup

logger = logging.getLogger("scraper.training")
OIKIRI_URL_TMPL = "https://race.netkeiba.com/race/oikiri.html?race_id={race_id}"
_HORSE_ID_RE = re.compile(r"/horse/(\d+)")


def parse_oikiri_html(html: str) -> tuple[dict[str, dict[str, Any]], int]:
    """
    {馬ID: {"critic": 評価の文字, "grade": 記号}} と、表の馬の行の数を返す。
    表が無ければ ({}, 0)。馬IDが読めない行は飛ばすが、行の数には入れる（取りこぼしを見えるようにする）。
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.OikiriTable")
    if table is None:
        return {}, 0
    out: dict[str, dict[str, Any]] = {}
    n_rows = 0
    for tr in table.find_all("tr"):
        info = tr.select_one("td.Horse_Info")
        if info is None:
            continue
        n_rows += 1
        link = info.select_one('a[href*="/horse/"]')
        m = _HORSE_ID_RE.search(link.get("href", "")) if link is not None else None
        if m is None:
            continue
        critic_td = tr.select_one("td.Training_Critic")
        grade_td = next((td for td in tr.find_all("td")
                         if any(c.startswith("Rank_") for c in td.get("class", []))), None)
        critic = critic_td.get_text(" ", strip=True) if critic_td is not None else ""
        grade = grade_td.get_text(" ", strip=True) if grade_td is not None else ""
        out[m.group(1)] = {"critic": critic or None, "grade": grade or None}
    return out, n_rows


def summarize(by_horse: dict[str, dict[str, Any]], n_rows: int) -> dict[str, Any]:
    """確認用のまとめ（記号の内訳、評価の文字の多い順。馬名などは出さない）。"""
    grades = Counter(v["grade"] for v in by_horse.values())
    critics = Counter(v["critic"] for v in by_horse.values())
    return {"n_rows": n_rows, "n_horses": len(by_horse),
            "n_with_grade": sum(1 for v in by_horse.values() if v["grade"]),
            "n_with_critic": sum(1 for v in by_horse.values() if v["critic"]),
            "grades": dict(grades.most_common()), "top_critic": dict(critics.most_common(6))}


def fetch_oikiri_html(race_id: str) -> str:
    from scraper.common.http import get as http_get
    url = OIKIRI_URL_TMPL.format(race_id=race_id)
    resp = http_get(url)
    if resp is None:
        raise RuntimeError(f"調教ページの取得に失敗しました: {url}")
    resp.encoding = resp.apparent_encoding
    return resp.text
