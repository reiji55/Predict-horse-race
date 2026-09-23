"""単勝オッズの時系列観測をレース単位で保存する。

目的は「直前資金を今すぐ賢い資金として使う」ことではない。
まず発走前に観測された市場分布を時系列で残し、後から
- 13時→直前で人気がどう動いたか
- 動きと実際の着順/モデル成績に関係があったか
を検証できるようにする。

保存先は data/odds_history/{race_id}/{観測時刻}_{phase}.json（1観測=1ファイル）。
通常pipelineと直前観測workflowは別々にcommit/pushするため、1レース1ファイルへ
追記する形だと両者が同じJSONを書き換えてrebaseが衝突する。1観測1ファイルなら
互いに新規ファイルを足すだけで、push競合時も `git pull --rebase` で必ず合流できる。

予想ロジックとは独立した観測ログで、p・妙味・カードには一切使わない。
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Any

from logic import snapshots

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "odds_history"
JST = datetime.timezone(datetime.timedelta(hours=9))

# 通常pipelineのrawがこれより古ければ「今回取った値」とみなさない。
# build_raw が途中で止まり前回のrawが残っているケースで、古いオッズに
# 新しい observed_at を付けて保存しないため。
MAX_RAW_AGE = datetime.timedelta(minutes=60)

ADDED = "added"
DUPLICATE = "duplicate"
NO_ODDS = "no_odds"
AFTER_POST = "after_post"
UNKNOWN_POST_TIME = "unknown_post_time"


def _rows_from_race(race: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for entry in race.get("entries", []):
        num = entry.get("num")
        odds = entry.get("win_odds")
        if num is None or odds is None:
            continue
        rows.append({
            "num": num,
            "win_odds": float(odds),
            "popularity": entry.get("popularity"),
        })
    return sorted(rows, key=lambda row: row["num"])


def _signature(observation: dict[str, Any]) -> tuple[Any, str]:
    """API側時刻＋馬番/オッズ本体が同じ観測は重複保存しない。"""
    return (
        observation.get("source_time"),
        json.dumps(observation.get("odds") or [], ensure_ascii=False, sort_keys=True),
    )


def parse_source_time(value: Any) -> datetime.datetime | None:
    """netkeiba の official_datetime（"2026-09-26 15:10:02"、JST・tz無し）を読む。"""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("/", "-"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def pre_race_status(race: dict[str, Any], observed_at: datetime.datetime,
                    source_time: Any = None) -> str | None:
    """発走前の観測と言えないなら理由を返す（fail-closed）。発走前なら None。"""
    post_at = snapshots.post_datetime(race)
    if post_at is None:
        return UNKNOWN_POST_TIME
    if observed_at >= post_at:
        return AFTER_POST
    # 取得開始は発走前でも、APIが返した時刻が発走後なら確定オッズ扱い。
    source_at = parse_source_time(source_time)
    if source_at is not None and source_at >= post_at:
        return AFTER_POST
    return None


def build_observation(race: dict[str, Any], phase: str,
                      observed_at: datetime.datetime | None = None) -> dict[str, Any] | None:
    rows = _rows_from_race(race)
    if not rows:
        return None
    now = observed_at or datetime.datetime.now(JST)
    return {
        "race_id": race.get("id"),
        "source_ref": (race.get("source_refs") or {}).get("netkeiba"),
        "post_time": race.get("post_time"),
        "observed_at": now.isoformat(timespec="seconds"),
        "source_time": race.get("odds_updated_at"),
        "phase": phase,
        "odds": rows,
    }


def load_observations(race_id: str, directory: Path | None = None) -> list[dict[str, Any]]:
    """1レースの観測を observed_at 順に返す（研究用の読み出し口）。"""
    race_dir = (directory or HISTORY_DIR) / race_id
    if not race_dir.is_dir():
        return []
    rows = []
    for path in sorted(race_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            rows.append(loaded)
    return sorted(rows, key=lambda row: row.get("observed_at") or "")


def append_observation(race: dict[str, Any], phase: str,
                       observed_at: datetime.datetime | None = None,
                       directory: Path | None = None) -> str:
    """1観測を保存し、結果（ADDED / DUPLICATE / NO_ODDS / AFTER_POST …）を返す。"""
    race_id = race.get("id")
    now = observed_at or datetime.datetime.now(JST)
    observation = build_observation(race, phase, now)
    if not race_id or observation is None:
        return NO_ODDS

    status = pre_race_status(race, now, observation["source_time"])
    if status is not None:
        return status

    directory = directory or HISTORY_DIR
    sig = _signature(observation)
    if any(_signature(row) == sig for row in load_observations(race_id, directory)):
        return DUPLICATE

    race_dir = directory / race_id
    race_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(JST).strftime("%Y%m%dT%H%M%S")
    path = race_dir / f"{stamp}_{phase}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(observation, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return ADDED


def _raw_fetched_at(raw: dict[str, Any]) -> datetime.datetime | None:
    value = raw.get("fetched_at")
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def append_current_run(raw: dict[str, Any], phase: str = "pipeline",
                       observed_at: datetime.datetime | None = None,
                       directory: Path | None = None,
                       now: datetime.datetime | None = None) -> dict[str, Any]:
    """通常pipelineで今まさに取得したレースだけを履歴へ追加する。

    観測時刻は「このスクリプトを動かした時刻」ではなく raw の fetched_at
    （オッズを取り終えた時刻）を使う。rawが古い／collection_reportが無い場合は
    今回取った値と確認できないので何も保存しない。
    """
    now = now or datetime.datetime.now(JST)
    report: dict[str, Any] = {"added": 0, "skipped": 0, "reasons": {}}

    collection = raw.get("collection_report")
    fetched_at = _raw_fetched_at(raw)
    if not isinstance(collection, dict):
        report["reasons"]["missing_collection_report"] = len(raw.get("races", []))
        return report
    if fetched_at is None or now - fetched_at > MAX_RAW_AGE:
        report["reasons"]["stale_raw"] = len(raw.get("races", []))
        return report

    built_ids = {
        item.get("race_id") for item in collection.get("built", [])
        if item.get("race_id")
    }
    races = [race for race in raw.get("races", []) if race.get("id") in built_ids]

    for race in races:
        status = append_observation(race, phase, observed_at or fetched_at, directory)
        if status == ADDED:
            report["added"] += 1
        else:
            report["skipped"] += 1
            report["reasons"][status] = report["reasons"].get(status, 0) + 1
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="単勝オッズ観測履歴を保存")
    parser.add_argument("--raw", required=True, help="raw/{week_id}.json")
    parser.add_argument("--phase", default="pipeline")
    args = parser.parse_args()

    with open(args.raw, encoding="utf-8") as f:
        raw = json.load(f)

    report = append_current_run(raw, phase=args.phase)
    print(f"odds history: added={report['added']} skipped={report['skipped']} "
          f"reasons={report['reasons']}")


if __name__ == "__main__":
    main()
