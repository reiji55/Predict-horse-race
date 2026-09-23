"""発走前に増えたコンディション情報を独立context snapshotとして凍結する。

予想snapshot（印・買い目・妙味・frozen_at）は一切変更しない。
13時の予想後に得られる馬体重/オッズ推移/パドック所見を、
data/context_snapshots/{race_id}/{timestamp}.json に新規ファイルとして残す。

1観測1ファイルなので通常pipelineと直前workflowが同時にpushしても
同じJSONを編集せず、後出しの買い目変更も起こらない。
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
from pathlib import Path
from typing import Any

from logic import condition_history, context_layers, snapshots

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
CONTEXT_SNAPSHOT_DIR = ROOT / "data" / "context_snapshots"
JST = snapshots.JST


def _overlay_latest_body_weights(race: dict[str, Any],
                                 directory: Path | None = None) -> dict[str, Any]:
    race = copy.deepcopy(race)
    latest = condition_history.latest_body_weights(race.get("id") or "", directory)
    for entry in race.get("entries") or []:
        num = entry.get("num")
        if num in latest:
            entry["body_weight"] = latest[num]
    return race


def load_context_snapshots(race_id: str,
                           directory: Path | None = None) -> list[dict[str, Any]]:
    race_dir = (directory or CONTEXT_SNAPSHOT_DIR) / race_id
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
    return sorted(rows, key=lambda x: x.get("observed_at") or "")


def latest_context_snapshot(race_id: str,
                            directory: Path | None = None) -> dict[str, Any] | None:
    rows = load_context_snapshots(race_id, directory)
    return rows[-1] if rows else None


def capture(raw: dict[str, Any], now: datetime.datetime | None = None,
            phase: str = "context",
            output_directory: Path | None = None,
            condition_directory: Path | None = None,
            odds_directory: Path | None = None,
            paddock_directory: Path | None = None) -> dict[str, Any]:
    now = now or datetime.datetime.now(JST)
    output_directory = output_directory or CONTEXT_SNAPSHOT_DIR
    report = {"added": [], "skipped": []}

    for race in raw.get("races") or []:
        race_id = race.get("id")
        if not race_id:
            continue
        post_at = snapshots.post_datetime(race)
        if post_at is None:
            report["skipped"].append({"race_id": race_id, "reason": "unknown_post_time"})
            continue
        if now >= post_at:
            report["skipped"].append({"race_id": race_id, "reason": "already_posted"})
            continue

        observed_race = _overlay_latest_body_weights(race, condition_directory)
        context = context_layers.build_context(
            observed_race,
            odds_directory=odds_directory,
            paddock_directory=paddock_directory,
        )
        payload = {
            "race_id": race_id,
            "post_time": race.get("post_time"),
            "observed_at": now.isoformat(timespec="seconds"),
            "phase": phase,
            "pre_race": True,
            "context_layers": context,
        }

        race_dir = output_directory / race_id
        race_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(JST).strftime("%Y%m%dT%H%M%S")
        path = race_dir / f"{stamp}_{phase}.json"
        if path.exists():
            report["skipped"].append({"race_id": race_id, "reason": "duplicate_time"})
            continue
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        report["added"].append(race_id)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="発走前のcontext snapshotを保存")
    parser.add_argument("--week", required=True)
    parser.add_argument("--phase", default="context")
    args = parser.parse_args()

    path = RAW_DIR / f"{args.week}.json"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    report = capture(raw, phase=args.phase)
    print(f"context snapshot: added={len(report['added'])} skipped={len(report['skipped'])}")


if __name__ == "__main__":
    main()
