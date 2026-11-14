"""
開催日ごとに、その日の全レースの「結果」または「出馬表」を取って data/picks/days/{日付}.json に置く。

- 結果が出ているレースは結果ページ（着順・タイム・通過順・上がり・馬体重・ラップ）を取る。
- まだ結果が無いレースは出馬表（出走馬・騎手・調教師・斤量・馬体重）を取る。
- すでに結果を保存済みのレースは取り直さない（1レース1〜2ページ、間隔は scraper.common.http の3〜5秒）。
- ページそのもの（HTML）は保存しない。表のセルの文字と、馬・騎手・調教師のIDだけを保存する。
- 予想の計算はここではしない（計算は検証用のコード側で行う）。

    python -m picks.fetch_day --dates 2026-11-14,2026-11-15
    python -m picks.fetch_day --dates 2026-11-15 --limit 3      （確認用：先頭3レースだけ）
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scraper.common.http import get as http_get
from scraper.fetchers import a_race_list, b_shutuba

logger = logging.getLogger("picks.fetch_day")
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "picks" / "days"
RESULT_URL = "https://race.netkeiba.com/race/result.html?race_id={race_id}"


def _ref(a, pattern: str) -> str | None:
    if a is None or not a.get("href"):
        return None
    m = re.search(pattern, a["href"])
    return m.group(1) if m else None


def _table_cells(table) -> list[list[str]]:
    rows = []
    for tr in table.select("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"], recursive=False)]
        if cells:
            rows.append(cells)
    return rows


def parse_result_page(html: str, race_id: str) -> dict[str, Any] | None:
    """結果ページから、着順表の全セル・ID・ラップ・通過順を取り出す。着順表に馬がいなければ None。"""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table#All_Result_Table")
    if table is None:
        return None
    rows = []
    for tr in table.select("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 10:
            continue
        rows.append({
            "cells": [td.get_text(" ", strip=True) for td in tds],
            "horse_id": _ref(tr.select_one('a[href*="/horse/"]'), r"/horse/(\w+)"),
            "jockey_id": _ref(tr.select_one('a[href*="/jockey/"]'), r"/jockey/(?:result/recent/)?(\w+)"),
            "trainer_id": _ref(tr.select_one('a[href*="/trainer/"]'), r"/trainer/(?:result/recent/)?(\w+)"),
        })
    if not rows:
        return None
    meta = b_shutuba._parse_race_meta(soup, race_id)
    text = lambda sel: (soup.select_one(sel).get_text(" ", strip=True) if soup.select_one(sel) else None)
    extra = {}
    for t in soup.select("table"):
        cls = " ".join(t.get("class") or [])
        if re.search(r"Haron|Corner|Lap", cls, re.I):
            extra[cls] = _table_cells(t)
    return {
        "race_id": race_id, "status": "result", "meta": meta,
        "header": [th.get_text(" ", strip=True) for th in table.select("tr th")],
        "race_data01": text(".RaceData01"), "race_data02": text(".RaceData02"),
        "title": soup.title.get_text(strip=True) if soup.title else None,
        "rows": rows, "tables": extra,
        "table_classes": sorted({" ".join(t.get("class") or []) for t in soup.select("table")}),
    }


def fetch_race(race_id: str) -> dict[str, Any] | None:
    resp = http_get(RESULT_URL.format(race_id=race_id))
    if resp is not None:
        resp.encoding = resp.apparent_encoding or resp.encoding
        got = parse_result_page(resp.text, race_id)
        if got is not None and any(r["cells"][0].isdigit() for r in got["rows"]):
            return got
    resp = http_get(b_shutuba.SHUTUBA_URL_TMPL.format(race_id=race_id))
    if resp is None:
        return None
    resp.encoding = resp.apparent_encoding or resp.encoding
    soup = BeautifulSoup(resp.text, "lxml")
    race = b_shutuba.parse_shutuba_html(resp.text, race_id)
    text = lambda sel: (soup.select_one(sel).get_text(" ", strip=True) if soup.select_one(sel) else None)
    return {"race_id": race_id, "status": "entry", "meta": {k: v for k, v in race.items() if k != "entries"},
            "race_data01": text(".RaceData01"), "race_data02": text(".RaceData02"),
            "title": soup.title.get_text(strip=True) if soup.title else None, "entries": race["entries"]}


def run(dates: list[str], limit: int | None) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {}
    for date in dates:
        path = OUT_DIR / f"{date}.json"
        day = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"date": date, "races": {}}
        try:
            entries = a_race_list.fetch_race_list(date)
        except Exception as exc:  # 開催の無い日・取得失敗
            report[date] = {"error": str(exc)[:200]}
            continue
        ids = [e.source_ref for e in entries if e.source_ref]
        day["list"] = {e.source_ref: {"venue": e.venue, "race_no": e.race_no, "name": e.name, "post_time": e.post_time,
                                      "surface": e.surface, "dist": e.dist, "heads": e.heads} for e in entries if e.source_ref}
        todo = [r for r in ids if day["races"].get(r, {}).get("status") != "result"]
        if limit:
            todo = todo[:limit]
        n = {"listed": len(ids), "result": 0, "entry": 0, "failed": 0}
        for rid in todo:
            try:
                got = fetch_race(rid)
            except Exception as exc:
                logger.warning("取得失敗 %s %s", rid, exc)
                got = None
            if got is None:
                n["failed"] += 1
                continue
            got["fetched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            day["races"][rid] = got
            n[got["status"]] += 1
        day["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        path.write_text(json.dumps(day, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        n["stored_result"] = sum(1 for r in day["races"].values() if r["status"] == "result")
        n["stored_entry"] = sum(1 for r in day["races"].values() if r["status"] == "entry")
        report[date] = n
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", required=True, help="YYYY-MM-DD をカンマ区切り")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    rep = run([d.strip() for d in a.dates.split(",") if d.strip()], a.limit)
    (ROOT / "data" / "picks" / "last_fetch_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1))
