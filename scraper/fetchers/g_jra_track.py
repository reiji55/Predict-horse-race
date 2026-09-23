"""JRA公式の開催当日馬場情報を低頻度で取得する。

目的:
- 「良/稍重/重/不良」だけでなく、含水率などの定量値を観測保存する。
- v1では予想スコアへ使わず context_layers の診断値にする。

JRAの馬場情報トップは開催中の競馬場を最大3ページで表示する:
  /keiba/baba/
  /keiba/baba/index2.html
  /keiba/baba/index3.html

HTML構造が変わっても誤値を作らないよう、明確に読めた項目だけ返す。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from scraper.common.http import get as http_get

logger = logging.getLogger("scraper.g_jra_track")

URLS = (
    "https://www.jra.go.jp/keiba/baba/",
    "https://www.jra.go.jp/keiba/baba/index2.html",
    "https://www.jra.go.jp/keiba/baba/index3.html",
)

VENUE_RE = re.compile(r"馬場情報[（(]([^）)]+?)競馬場[）)]")
DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")
DECIMAL_RE = re.compile(r"^\d{1,2}\.\d$")


def _f(value: str) -> float | None:
    try:
        return float(value.strip().replace("%", ""))
    except (TypeError, ValueError):
        return None


def _section_between(soup: BeautifulSoup, heading_text: str, stop_text: str) -> list[Any]:
    heading = next(
        (tag for tag in soup.find_all(["h2", "h3", "h4"]) if heading_text in tag.get_text(" ", strip=True)),
        None,
    )
    if heading is None:
        return []
    nodes = []
    for tag in heading.find_all_next():
        if tag is heading:
            continue
        if tag.name in ("h2", "h3", "h4") and stop_text in tag.get_text(" ", strip=True):
            break
        nodes.append(tag)
    return nodes


def parse_baba_html(html: str, source_url: str | None = None) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    venue_m = VENUE_RE.search(title)
    if not venue_m:
        # titleが簡略化されているケースの保険として本文冒頭も見る。
        venue_m = VENUE_RE.search(soup.get_text(" ", strip=True)[:1000])
    if not venue_m:
        return None
    venue = venue_m.group(1)

    text = soup.get_text(" ", strip=True)
    # ページ内に年付きの日付が複数（前日測定・更新日など）あると、どれが開催日か
    # 断定できない。推測で選ぶと前開催の値を当日に誤添付しうるので None にする。
    dates = {
        f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        for m in DATE_RE.finditer(text)
    }
    date = next(iter(dates)) if len(dates) == 1 else None
    if len(dates) > 1:
        logger.warning("JRA馬場ページに日付が複数あり開催日を断定できません venue=%s dates=%s",
                       venue, sorted(dates))

    moisture: dict[str, list[float]] = {}
    ambiguous_surfaces: set[str] = set()
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if len(cells) < 3 or cells[0] not in ("芝", "ダート"):
            continue
        a, b = _f(cells[1]), _f(cells[2])
        # 含水率として有り得ない値（列ずれ等）は採らない。
        if a is None or b is None or not (0 < a < 50 and 0 < b < 50):
            continue
        # 同じ馬場の行が複数（金曜測定と当日測定など）あれば、どれが当日か断定できない。
        if cells[0] in moisture and moisture[cells[0]] != [a, b]:
            ambiguous_surfaces.add(cells[0])
        moisture[cells[0]] = [a, b]
    for surface in ambiguous_surfaces:
        logger.warning("含水率の行が複数あり断定できません venue=%s surface=%s", venue, surface)
        moisture.pop(surface, None)

    cushion_value = None
    cushion_time = None
    section = _section_between(soup, "芝のクッション値", "含水率")
    if section:
        # 実測値は小数1桁。基準目盛り 12/10/8/7（整数）は候補にしない。
        decimal_candidates: list[float] = []
        time_candidates: set[str] = set()
        for tag in section:
            txt = tag.get_text(" ", strip=True)
            # 子要素を持つタグは子孫の文字列を重複して含むので、時刻は葉だけから拾う。
            if not tag.find(True):
                time_candidates.update(TIME_RE.findall(txt))
            if DECIMAL_RE.fullmatch(txt):
                val = _f(txt)
                if val is not None and 4 <= val <= 15:
                    decimal_candidates.append(val)
            # select/optionにselected値があるページではそちらを最優先。
            if tag.name == "option" and tag.has_attr("selected"):
                val = _f(txt)
                if val is not None and 4 <= val <= 15:
                    cushion_value = val
        if cushion_value is None and len(set(decimal_candidates)) == 1:
            cushion_value = decimal_candidates[0]
        # 測定時刻も、区間内に1つだけのときに限り値へ紐づける。
        if cushion_value is not None and len(time_candidates) == 1:
            cushion_time = next(iter(time_candidates))

    course_usage = None
    turf_state = None
    headings = soup.find_all(["h2", "h3", "h4"])
    for tag in headings:
        label = tag.get_text(" ", strip=True)
        if label == "使用コース":
            nxt = tag.find_next()
            while nxt and nxt.name in ("a", "span"):
                nxt = nxt.find_next()
            if nxt:
                course_usage = nxt.get_text(" ", strip=True) or None
        elif label == "芝の状態":
            nxt = tag.find_next()
            while nxt and nxt.name in ("a", "span"):
                nxt = nxt.find_next()
            if nxt:
                turf_state = nxt.get_text(" ", strip=True) or None

    if not moisture and cushion_value is None:
        logger.warning("JRA馬場ページから定量値を読めませんでした venue=%s url=%s", venue, source_url)
        return {
            "venue": venue, "date": date, "source_url": source_url,
            "cushion": None, "moisture": {}, "course_usage": course_usage,
            "turf_state": turf_state,
        }

    return {
        "venue": venue,
        "date": date,
        "source_url": source_url,
        "cushion": {
            "value": cushion_value,
            "measured_at": cushion_time,
        } if cushion_value is not None else None,
        "moisture": {
            surface: {"goal": vals[0], "turn4": vals[1]}
            for surface, vals in moisture.items()
        },
        "course_usage": course_usage,
        "turf_state": turf_state,
    }


def fetch_active_track_metrics() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for url in URLS:
        resp = http_get(url)
        if resp is None:
            continue
        resp.encoding = resp.apparent_encoding
        parsed = parse_baba_html(resp.text, source_url=url)
        if parsed and parsed.get("venue"):
            out[parsed["venue"]] = parsed
    return out
