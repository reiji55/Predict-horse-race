"""JRA公式の当日馬場定量値を raw の対象レースへ添付する。"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from scraper.fetchers import g_jra_track

logger = logging.getLogger("scraper.capture_track_context")


def attach(raw: dict[str, Any], dates: list[str],
           metrics_by_venue: dict[str, dict[str, Any]]) -> dict[str, Any]:
    targets = set(dates)
    captured = []
    skipped = []
    for race in raw.get("races") or []:
        race_date = race.get("date")
        if race_date not in targets:
            continue
        venue = race.get("venue")
        metrics = metrics_by_venue.get(venue)
        if not metrics:
            skipped.append({"race_id": race.get("id"), "reason": "venue_not_found"})
            continue
        # JRAページの日付が取れていて対象日と違うなら、前開催の値を誤添付しない。
        if metrics.get("date") and metrics["date"] != race_date:
            skipped.append({"race_id": race.get("id"), "reason": "date_mismatch"})
            continue
        race["track_metrics"] = metrics
        captured.append(race.get("id"))
    raw["track_context_report"] = {
        "requested_dates": list(dates),
        "captured": captured,
        "skipped": skipped,
    }
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(description="JRA馬場定量値をrawへ添付")
    parser.add_argument("--raw", required=True)
    parser.add_argument("--dates", nargs="+", required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    path = Path(args.raw)
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)

    try:
        metrics = g_jra_track.fetch_active_track_metrics()
    except Exception:
        # observe-onlyなので、馬場情報取得だけで本番予想を止めない。
        logger.exception("JRA馬場情報の取得に失敗しました。track_metricsなしで続行します")
        metrics = {}

    attach(raw, args.dates, metrics)
    with path.open("w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.write("\n")
    logger.info("track context: venues=%s", sorted(metrics))


if __name__ == "__main__":
    main()
