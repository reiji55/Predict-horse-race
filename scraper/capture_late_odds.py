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
from typing import Any, Callable

from logic import condition_history, odds_history, snapshots
from scraper.fetchers import b2_odds, b_shutuba

logger = logging.getLogger("scraper.capture_late_odds")

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
JST = datetime.timezone(datetime.timedelta(hours=9))


def _date_key(date_str: str) -> str:
    return date_str.replace("-", "")


def _system_clock() -> datetime.datetime:
    return datetime.datetime.now(JST)


def capture(raw: dict[str, Any], date_str: str,
            now: datetime.datetime | None = None,
            directory: Path | None = None,
            clock: Callable[[], datetime.datetime] | None = None) -> dict[str, Any]:
    # 1レースごとに時計を読み直す。courtesy spacing(3秒)やリトライで取得が長引いても、
    # 実行開始時刻のまま発走判定・observed_at 記録をしないため。
    if clock is None:
        clock = (lambda: now) if now is not None else _system_clock
    key = _date_key(date_str)
    report: dict[str, Any] = {
        "captured": [], "skipped": [], "failed": [],
        "body_weight": {"captured": [], "skipped": [], "failed": []},
    }

    for race in raw.get("races", []):
        race_id = race.get("id") or ""
        if not race_id.startswith(key):
            continue

        post_at = snapshots.post_datetime(race)
        if post_at is None:
            report["skipped"].append({"race_id": race_id, "reason": "unknown_post_time"})
            continue
        if clock() >= post_at:
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
        # observed_at は取得完了後の時刻。取得中に発走時刻を跨いだら保存しない（fail-closed）。
        observed_at = clock()
        status = odds_history.append_observation(
            observed_race, phase="late", observed_at=observed_at, directory=directory
        )
        if status == odds_history.AFTER_POST:
            report["skipped"].append({"race_id": race_id, "reason": "posted_during_fetch"})
            continue
        if status == odds_history.NO_ODDS:
            # APIは返ったが単勝が1件も読めない。取得失敗として数える。
            report["failed"].append({"race_id": race_id, "reason": "no_win_odds"})
            continue
        report["captured"].append({
            "race_id": race_id,
            "source_time": fetched.get("official_datetime"),
            "added": status == odds_history.ADDED,
            "status": status,
            "minutes_to_post": round((post_at - observed_at).total_seconds() / 60, 1),
        })

        # 同じ直前枠で出馬表を1回だけ再取得し、計量済みなら馬体重も保存する。
        # 予想へはまだ加点しない。後方検証用の観測ログ。
        try:
            latest_race = b_shutuba.fetch_shutuba(source_ref)
        except RuntimeError:
            logger.warning("直前馬体重を取得できませんでした: %s", race_id, exc_info=True)
            report["body_weight"]["failed"].append({"race_id": race_id})
        else:
            latest_race["id"] = race_id
            latest_race["post_time"] = race.get("post_time")
            latest_race["source_refs"] = race.get("source_refs")
            body_at = clock()
            bw_status = condition_history.append_body_weight_observation(
                latest_race, phase="late", observed_at=body_at,
            )
            bucket = (
                "captured" if bw_status in (condition_history.ADDED, condition_history.DUPLICATE)
                else "skipped"
            )
            report["body_weight"][bucket].append({
                "race_id": race_id,
                "status": bw_status,
                "minutes_to_post": round((post_at - body_at).total_seconds() / 60, 1),
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
        # Actionsの開始遅延で、その日の通常pipelineより先に動くことがある。
        # 観測できないだけなので赤にはせず、何もせず終わる。
        logger.warning("%s がありません。通常pipelineがまだ走っていないため今回は観測しません。", path)
        return

    with path.open(encoding="utf-8") as f:
        raw = json.load(f)

    report = capture(raw, date_str)
    logger.info(
        "late odds: captured=%d skipped=%d failed=%d / body_weight captured=%d skipped=%d failed=%d",
        len(report["captured"]), len(report["skipped"]), len(report["failed"]),
        len(report["body_weight"]["captured"]), len(report["body_weight"]["skipped"]),
        len(report["body_weight"]["failed"]),
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
