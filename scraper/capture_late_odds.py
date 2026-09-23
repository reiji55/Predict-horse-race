"""発走前の直近単勝オッズを軽量取得して時系列保存する。

通常pipelineの全スクレイプは行わない。既にrawにある当日の11Rを対象に、
単勝オッズAPIだけを1レース1回取得する。

発走時刻を過ぎたレースは必ずスキップする。
このログは将来の「直前オッズ変動」研究用で、現時点の予想ロジックには使わない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
from pathlib import Path
from typing import Any

from logic import odds_history, snapshots
from scraper.fetchers import b2_odds

logger = logging.getLogger("scraper.capture_late_odds")

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
JST = datetime.timezone(datetime.timedelta(hours=9))


def _date_key(date_str: str) -> str:
    return date_str.replace("-", "")


def capture(raw: dict[str, Any], date_str: str,
            now: datetime.datetime | None = None,
            directory: Path | None = None) -> dict[str, Any]:
    now = now or datetime.datetime.now(JST)
    key = _date_key(date_str)
    report: dict[str, Any] = {"captured": [], "skipped": [], "failed": []}

    for race in raw.get("races", []):
        race_id = race.get("id") or ""
        if not race_id.startswith(key):
            continue

        post_at = snapshots.post_datetime(race)
        if post_at is None:
            report["skipped"].append({"race_id": race_id, "reason": "unknown_post_time"})
            continue
        if now >= post_at:
            report["skipped"].append({"race_id": race_id, "reason": "already_posted"})
            continue

        source_ref = (race.get("source_refs") or {}).get("netkeiba")
        if not source_ref:
            report["skipped"].append({"race_id": race_id, "reason": "missing_source_ref"})
            continue

        try:
            fetched = b2_odds.fetch_win_odds(source_ref)
        except (RuntimeError, ValueError, json.JSONDecodeError):
            logger.exception("直前単勝オッズを取得できませんでした: %s", race_id)
            report["failed"].append({"race_id": race_id})
            continue

        by_num = fetched.get("by_num") or {}
        observed_race = {
            "id": race_id,
            "source_refs": race.get("source_refs"),
            "post_time": race.get("post_time"),
            "odds_updated_at": fetched.get("official_datetime"),
            "entries": [
                {
                    "num": num,
                    "win_odds": row.get("win_odds"),
                    "popularity": row.get("popularity"),
                }
                for num, row in sorted(by_num.items())
            ],
        }
        added = odds_history.append_observation(
            observed_race, phase="late", observed_at=now, directory=directory
        )
        report["captured"].append({
            "race_id": race_id,
            "source_time": fetched.get("official_datetime"),
            "added": added,
            "minutes_to_post": round((post_at - now).total_seconds() / 60, 1),
        })

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="発走前の直近単勝オッズを時系列保存")
    parser.add_argument("--week", required=True, help="例: 2026-W39")
    parser.add_argument("--date", help="YYYY-MM-DD。省略時はJSTの今日")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    date_str = args.date or datetime.datetime.now(JST).date().isoformat()
    path = RAW_DIR / f"{args.week}.json"
    if not path.exists():
        raise SystemExit(f"{path} がありません。通常pipelineが先に成功している必要があります。")

    with path.open(encoding="utf-8") as f:
        raw = json.load(f)

    report = capture(raw, date_str)
    logger.info(
        "late odds: captured=%d skipped=%d failed=%d",
        len(report["captured"]), len(report["skipped"]), len(report["failed"]),
    )
    for row in report["captured"]:
        logger.info(
            "%s: postまで%s分 source_time=%s added=%s",
            row["race_id"], row["minutes_to_post"], row["source_time"], row["added"],
        )

    # 当日の対象があるのに全取得が失敗したときだけ異常終了。1場だけ失敗なら残りは保存する。
    attempted = len(report["captured"]) + len(report["failed"])
    if attempted and not report["captured"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
