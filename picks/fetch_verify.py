"""
検証用：開催日の全レースについて、発走前の情報（出馬表・過去5走）と確定結果を1ファイルにまとめる。
過去5走は当日以降の走を落とす（結果の混入を防ぐ）。予想の計算はしない。

    python -m picks.fetch_verify --date 2026-11-21
"""
from __future__ import annotations

import argparse, datetime, json, logging
from pathlib import Path

from scraper.common.http import get as http_get
from scraper.fetchers import a_race_list, b_shutuba, c2_shutuba_past
from picks.fetch_day import RESULT_URL, parse_result_page, result_is_final

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "picks" / "verify"
log = logging.getLogger("picks.fetch_verify")


def one(rid: str, date: str) -> dict:
    out: dict = {"race_id": rid}
    try:
        resp = http_get(b_shutuba.SHUTUBA_URL_TMPL.format(race_id=rid))
        resp.encoding = resp.apparent_encoding or resp.encoding
        race = b_shutuba.parse_shutuba_html(resp.text, rid)
        out["meta"] = {k: v for k, v in race.items() if k != "entries"}
        out["entries"] = race["entries"]
    except Exception as exc:
        out["shutuba_error"] = str(exc)[:200]
    try:
        past = c2_shutuba_past.parse_shutuba_past_html(c2_shutuba_past.fetch_shutuba_past_html(rid), n_runs=6,
                                                       read_pedigree=False)
        out["past"] = {h: [r for r in v["past_runs"] if r.get("date") and r["date"] < date][:5] for h, v in past.items()}
    except Exception as exc:
        out["past_error"] = str(exc)[:200]
    try:
        resp = http_get(RESULT_URL.format(race_id=rid))
        resp.encoding = resp.apparent_encoding or resp.encoding
        got = parse_result_page(resp.text, rid)
        if got is not None:
            got["final"] = result_is_final(got)
            out["result"] = got
    except Exception as exc:
        out["result_error"] = str(exc)[:200]
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    a = ap.parse_args()
    entries = a_race_list.fetch_race_list(a.date)
    day = {"date": a.date, "list": {}, "races": {}}
    for e in entries:
        if not e.source_ref:
            continue
        day["list"][e.source_ref] = {"venue": e.venue, "race_no": e.race_no, "name": e.name, "grade": e.grade,
                                     "surface": e.surface, "dist": e.dist, "heads": e.heads, "post_time": e.post_time}
        day["races"][e.source_ref] = one(e.source_ref, a.date)
        log.info("done %s", e.source_ref)
    day["fetched_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{a.date}.json").write_text(json.dumps(day, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
