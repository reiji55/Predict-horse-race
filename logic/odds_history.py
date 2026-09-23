"""単勝オッズの時系列観測をレース単位で保存する。

目的は「直前資金を今すぐ賢い資金として使う」ことではない。
まず発走前に観測された市場分布を時系列で残し、後から
- 13時→直前で人気がどう動いたか
- 動きと実際の着順/モデル成績に関係があったか
を検証できるようにする。

data/odds_history/{race_id}.json は予想ロジックとは独立した観測ログ。
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "odds_history"
JST = datetime.timezone(datetime.timedelta(hours=9))


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


def build_observation(race: dict[str, Any], phase: str,
                      observed_at: datetime.datetime | None = None) -> dict[str, Any] | None:
    rows = _rows_from_race(race)
    if not rows:
        return None
    now = observed_at or datetime.datetime.now(JST)
    return {
        "observed_at": now.isoformat(timespec="seconds"),
        "source_time": race.get("odds_updated_at"),
        "phase": phase,
        "odds": rows,
    }


def append_observation(race: dict[str, Any], phase: str,
                       observed_at: datetime.datetime | None = None,
                       directory: Path | None = None) -> bool:
    """1観測を追記。新しい情報が増えたときだけ True。"""
    race_id = race.get("id")
    observation = build_observation(race, phase, observed_at)
    if not race_id or observation is None:
        return False

    directory = directory or HISTORY_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{race_id}.json"

    payload: dict[str, Any] = {
        "race_id": race_id,
        "source_ref": (race.get("source_refs") or {}).get("netkeiba"),
        "post_time": race.get("post_time"),
        "observations": [],
    }
    if path.exists():
        with path.open(encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            payload.update(loaded)
            payload.setdefault("observations", [])

    sig = _signature(observation)
    if any(_signature(row) == sig for row in payload["observations"]):
        return False

    payload["source_ref"] = payload.get("source_ref") or (race.get("source_refs") or {}).get("netkeiba")
    payload["post_time"] = race.get("post_time") or payload.get("post_time")
    payload["observations"].append(observation)
    payload["observations"].sort(key=lambda row: row.get("observed_at") or "")

    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return True


def append_current_run(raw: dict[str, Any], phase: str = "pipeline",
                       observed_at: datetime.datetime | None = None,
                       directory: Path | None = None) -> dict[str, int]:
    """通常pipelineで今まさに取得したレースだけを履歴へ追加する。"""
    collection = raw.get("collection_report") or {}
    built_ids = {
        item.get("race_id") for item in collection.get("built", [])
        if item.get("race_id")
    }
    races = raw.get("races", [])
    if built_ids:
        races = [race for race in races if race.get("id") in built_ids]

    added = skipped = 0
    for race in races:
        if append_observation(race, phase, observed_at, directory):
            added += 1
        else:
            skipped += 1
    return {"added": added, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="単勝オッズ観測履歴を保存")
    parser.add_argument("--raw", required=True, help="raw/{week_id}.json")
    parser.add_argument("--phase", default="pipeline")
    args = parser.parse_args()

    with open(args.raw, encoding="utf-8") as f:
        raw = json.load(f)

    report = append_current_run(raw, phase=args.phase)
    print(f"odds history: added={report['added']} skipped={report['skipped']}")


if __name__ == "__main__":
    main()
