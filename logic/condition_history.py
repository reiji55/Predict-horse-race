"""当日コンディション（現時点では馬体重）の発走前観測履歴。

通常rawの13時取得では馬体重が未計量のことがあるため、直前観測workflowで
出馬表をもう一度だけ取得し、body_weight が出た時点を保存する。
予想スコアにはまだ使わない。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from logic import snapshots

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "condition_history"
JST = snapshots.JST

ADDED = "added"
DUPLICATE = "duplicate"
NO_WEIGHT = "no_weight"
AFTER_POST = "after_post"
UNKNOWN_POST_TIME = "unknown_post_time"


def _rows(race: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for entry in race.get("entries") or []:
        bw = entry.get("body_weight")
        if entry.get("num") is None or not isinstance(bw, dict) or bw.get("value") is None:
            continue
        out.append({
            "num": int(entry["num"]),
            "body_weight": {
                "value": int(bw["value"]),
                "diff": int(bw["diff"]) if bw.get("diff") is not None else None,
            },
        })
    return sorted(out, key=lambda x: x["num"])


def _signature(obs: dict[str, Any]) -> str:
    return json.dumps(obs.get("horses") or [], ensure_ascii=False, sort_keys=True)


def load_observations(race_id: str, directory: Path | None = None) -> list[dict[str, Any]]:
    race_dir = (directory or HISTORY_DIR) / race_id
    if not race_dir.is_dir():
        return []
    rows = []
    for path in sorted(race_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                row = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return sorted(rows, key=lambda r: r.get("observed_at") or "")


def append_body_weight_observation(race: dict[str, Any], phase: str,
                                   observed_at: datetime.datetime | None = None,
                                   directory: Path | None = None) -> str:
    race_id = race.get("id")
    horses = _rows(race)
    if not race_id or not horses:
        return NO_WEIGHT

    now = observed_at or datetime.datetime.now(JST)
    post_at = snapshots.post_datetime(race)
    if post_at is None:
        return UNKNOWN_POST_TIME
    if now >= post_at:
        return AFTER_POST

    obs = {
        "race_id": race_id,
        "source_ref": (race.get("source_refs") or {}).get("netkeiba"),
        "post_time": race.get("post_time"),
        "observed_at": now.isoformat(timespec="seconds"),
        "phase": phase,
        "horses": horses,
    }
    sig = _signature(obs)
    if any(_signature(row) == sig for row in load_observations(race_id, directory)):
        return DUPLICATE

    race_dir = (directory or HISTORY_DIR) / race_id
    race_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(JST).strftime("%Y%m%dT%H%M%S")
    path = race_dir / f"{stamp}_{phase}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(obs, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return ADDED
