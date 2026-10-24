"""
netkeibaの調教（追い切り）ページが、有料会員でなくてもどこまで見られるかを確かめる。

  対象：https://race.netkeiba.com/race/oikiri.html?race_id={レースID}
  出すもの：表が見つかったか、行数、列の見出し、列ごとに「数字が入っているか」「文字が入っているか」、
            会員向けの鍵（プレミアム・ログイン等の文言）があるか。
  ページの中身（馬名・タイム・評価の文章）そのものは出さない・保存しない（netkeibaの著作物）。

使い方：
  python -m scripts.check_training_page 202605040811 202609040811
"""
from __future__ import annotations

import json
import re
import sys

from bs4 import BeautifulSoup

URL = "https://race.netkeiba.com/race/oikiri.html?race_id={race_id}"
LOCK_WORDS = ["プレミアム", "ログイン", "会員登録", "有料", "スーパープレミアム"]
TIME_RE = re.compile(r"\d{1,2}\.\d")


def summarize(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""
    text = soup.get_text(" ", strip=True)
    tables = soup.find_all("table")
    best = max(tables, key=lambda t: len(t.find_all("tr")), default=None)
    out = {"title_has_oikiri": ("調教" in title or "追い切り" in title), "bytes": len(html), "n_tables": len(tables),
           "lock_words": {w: text.count(w) for w in LOCK_WORDS if w in text}}
    if best is None:
        return out
    out["table_class"] = " ".join(best.get("class", []))
    headers = [th.get_text(" ", strip=True) for th in best.find_all("th")]
    out["headers"] = headers[:20]
    rows = [tr for tr in best.find_all("tr") if tr.find("td")]
    out["n_rows"] = len(rows)
    cols: dict[int, dict] = {}
    for tr in rows:
        for j, td in enumerate(tr.find_all("td")):
            t = td.get_text(" ", strip=True)
            c = cols.setdefault(j, {"class": " ".join(td.get("class", [])), "filled": 0, "has_time": 0,
                                    "has_text": 0, "locked": 0})
            c["filled"] += bool(t)
            c["has_time"] += bool(TIME_RE.search(t))
            c["has_text"] += bool(re.search(r"[ぁ-んァ-ン一-龥]", t))
            c["locked"] += any(w in t for w in LOCK_WORDS) or bool(td.select("[class*=Premium], [class*=premium], [class*=Lock], [class*=lock]"))
    out["columns"] = [{"col": j, **cols[j]} for j in sorted(cols)]
    return out


def main(argv: list[str]) -> int:
    from scraper.common.http import get as http_get
    report = {}
    for race_id in argv or ["202605040811"]:
        resp = http_get(URL.format(race_id=race_id))
        if resp is None:
            report[race_id] = {"error": "取得失敗"}
            continue
        resp.encoding = resp.apparent_encoding
        report[race_id] = summarize(resp.text)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
