"""検証用：その日の全レースの払戻表（全券種）を data/picks/verify/{日付}_payout.json に保存する。"""
import argparse, json
from pathlib import Path
from bs4 import BeautifulSoup
from scraper.common.http import get as http_get
from picks.fetch_day import RESULT_URL

ROOT = Path(__file__).resolve().parent.parent

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--date", required=True); a = ap.parse_args()
    day = json.loads((ROOT / "data/picks/verify" / f"{a.date}.json").read_text(encoding="utf-8"))
    out = {}
    for rid in day["list"]:
        try:
            resp = http_get(RESULT_URL.format(race_id=rid)); resp.encoding = resp.apparent_encoding or resp.encoding
            soup = BeautifulSoup(resp.text, "lxml"); rows = []
            for t in soup.select("table.Payout_Detail_Table"):
                for tr in t.select("tr"):
                    cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                    if cells: rows.append(cells)
            out[rid] = rows
        except Exception as exc:
            out[rid] = {"error": str(exc)[:200]}
    (ROOT / "data/picks/verify" / f"{a.date}_payout.json").write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")

if __name__ == "__main__":
    main()
