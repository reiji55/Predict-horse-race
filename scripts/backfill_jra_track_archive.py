"""
JRA公式の「含水率・クッション値」の過去データ（年ごとのページ → 開催ごとのPDF）を集めて、1日1行の表にする。

  出典：https://www.jra.go.jp/keiba/baba/archive/{年}.html
        含水率は 2018-07-27 から、芝のクッション値は 2020-09-11 から公開
  出力：data/track/jra_track_archive.csv
        year, venue, kai, day_no, date, weekday, course, cushion_time, cushion, moisture_time,
        turf_goal, turf_turn4, dirt_goal, dirt_turn4, source

測るのは原則として開催日の早朝（金曜は午前）なので、その日のレースより前に分かる情報。
PDFそのものは保存しない（数値だけを表にする）。間隔は scraper.common.http の3〜5秒をそのまま守る。

GitHub Actions の手動 workflow（.github/workflows/manual_jra_track_archive.yml）で動かす。
PRでは数ファイルだけ読んで、読み取りが正しいかを確かめる（--sample）。

使い方：
  python -m scripts.backfill_jra_track_archive                 # 2018〜今年の全部
  python -m scripts.backfill_jra_track_archive --sample        # 確認用に3ファイルだけ
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import logging
import re
import sys
import unicodedata
from pathlib import Path

logger = logging.getLogger("scripts.backfill_jra_track_archive")
ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "track" / "jra_track_archive.csv"
BASE = "https://www.jra.go.jp/keiba/baba/archive/"
FIELDS = ["year", "venue", "kai", "day_no", "date", "weekday", "course", "cushion_time", "cushion", "moisture_time",
          "turf_goal", "turf_turn4", "dirt_goal", "dirt_turn4", "source"]
VENUES = {"sapporo": "札幌", "hakodate": "函館", "fukushima": "福島", "niigata": "新潟", "tokyo": "東京",
          "nakayama": "中山", "chukyo": "中京", "kyoto": "京都", "hanshin": "阪神", "kokura": "小倉"}
PDF_RE = re.compile(r'href="([^"]*?(\d{4})pdf/([a-z]+)(\d{2})\.pdf)"')
ROW_START = re.compile(r"(?:第\s*(\d+)\s*日\s*)?(\d{1,2})月\s*(\d{1,2})日\s*([月火水木金土日])曜日")
TOKEN = re.compile(r"\d{1,2}:\d{2}|\d+\.\d|[－―\-]")


def _num(tok: str) -> float | None:
    return float(tok) if re.fullmatch(r"\d+\.\d", tok) else None


def parse_pdf_text(text: str, year: int, venue: str, kai: int, source: str = "") -> list[dict]:
    """PDFから取り出した文字列を1日1行に分ける。読めない行は飛ばす（全体は止めない）。"""
    text = unicodedata.normalize("NFKC", text)
    starts = list(ROW_START.finditer(text))
    rows = []
    for i, m in enumerate(starts):
        chunk = text[m.end(): starts[i + 1].start() if i + 1 < len(starts) else len(text)]
        course_m = re.match(r"\s*([A-D])(?![a-z])", chunk)
        course = course_m.group(1) if course_m else ""
        toks = TOKEN.findall(chunk[course_m.end():] if course_m else chunk)
        times = [k for k, t in enumerate(toks) if ":" in t]
        cushion_time = cushion = moisture_time = None
        if len(times) >= 2 and times[1] == times[0] + 2:
            cushion_time, cushion = toks[times[0]], _num(toks[times[0] + 1])
            moisture_time, vals = toks[times[1]], toks[times[1] + 1: times[1] + 5]
        elif len(times) >= 1:
            moisture_time, vals = toks[times[0]], toks[times[0] + 1: times[0] + 5]
        else:
            continue
        vals = [_num(v) for v in vals] + [None] * (4 - len(vals))
        if all(v is None for v in vals) and cushion is None:
            continue
        month, day = int(m.group(2)), int(m.group(3))
        try:
            date = datetime.date(year, month, day).isoformat()
        except ValueError:
            continue
        rows.append({"year": year, "venue": venue, "kai": kai, "day_no": m.group(1) or "", "date": date,
                     "weekday": m.group(4), "course": course, "cushion_time": cushion_time or "",
                     "cushion": cushion, "moisture_time": moisture_time or "", "turf_goal": vals[0],
                     "turf_turn4": vals[1], "dirt_goal": vals[2], "dirt_turn4": vals[3], "source": source})
    return rows


def pdf_links(year: int) -> list[tuple[str, str, int]]:
    """年のページから（PDFのURL, 競馬場, 回）を取る。"""
    from scraper.common.http import get as http_get
    resp = http_get(f"{BASE}{year}.html")
    if resp is None:
        logger.warning("年のページを取得できません: %s", year)
        return []
    resp.encoding = resp.apparent_encoding
    out = []
    for href, y, ven, kai in PDF_RE.findall(resp.text):
        if ven in VENUES and int(y) == year:
            url = href if href.startswith("http") else BASE + href.split("archive/")[-1].lstrip("/")
            out.append((url, VENUES[ven], int(kai)))
    return sorted(set(out))


def pdf_text(url: str) -> str | None:
    from pypdf import PdfReader
    from scraper.common.http import get as http_get
    resp = http_get(url)
    if resp is None:
        return None
    reader = PdfReader(io.BytesIO(resp.content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="JRAの含水率・クッション値の過去データを表にする")
    ap.add_argument("--sample", action="store_true", help="確認用に3ファイルだけ読んで結果を表示（保存しない）")
    ap.add_argument("--first-year", type=int, default=2018)
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    if args.sample:
        checks = [(2025, f"{BASE}2025pdf/nakayama01.pdf", "中山", 1), (2021, f"{BASE}2021pdf/tokyo05.pdf", "東京", 5),
                  (2019, f"{BASE}2019pdf/hanshin05.pdf", "阪神", 5)]
        report = []
        for year, url, ven, kai in checks:
            txt = pdf_text(url)
            rows = parse_pdf_text(txt or "", year, ven, kai, url)
            report.append({"pdf": url, "text_chars": len(txt or ""), "rows": len(rows), "first_rows": rows[:3]})
        print(json.dumps(report, ensure_ascii=False, indent=1))
        first = report[0]["first_rows"][0] if report[0]["first_rows"] else {}
        # PDFの1行目（2025年1回中山 1月4日）：B 10:30 10.1 10:30 13.4 13.7 2.1 2.1
        ok = (first.get("date") == "2025-01-04" and first.get("course") == "B" and first.get("cushion") == 10.1
              and [first.get(k) for k in ("turf_goal", "turf_turn4", "dirt_goal", "dirt_turn4")] == [13.4, 13.7, 2.1, 2.1])
        print("SAMPLE_CHECK", "OK" if ok else "NG")
        return 0 if ok else 1

    rows: list[dict] = []
    this_year = datetime.date.today().year
    for year in range(args.first_year, this_year + 1):
        links = pdf_links(year)
        logger.info("%d年：PDF %d 件", year, len(links))
        for url, ven, kai in links:
            txt = pdf_text(url)
            got = parse_pdf_text(txt or "", year, ven, kai, url)
            if not got:
                logger.warning("行を読めません: %s", url)
            rows.extend(got)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["date"], r["venue"])):
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in FIELDS})
    tmp.replace(out)
    logger.info("完了：%d 行（%s）", len(rows), out)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
