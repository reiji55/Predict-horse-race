"""発走前に増えたコンディション情報だけを既存snapshotへ追記する。

カード・印・妙味・frozen_atは一切変更しない。
13時の予想後に得られる馬体重/オッズ推移/パドック所見を、
発走前に観測できた事実として context_layers だけ更新する。

これにより「直前情報は保存したいが、買い目の後出し変更は絶対しない」を両立する。
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


def refresh(raw: dict[str, Any], now: datetime.datetime | None = None,
            snapshot_directory: Path | None = None,
            condition_directory: Path | None = None,
            odds_directory: Path | None = None,
            paddock_directory: Path | None = None) -> dict[str, Any]:
    now = now or datetime.datetime.now(JST)
    snapshot_directory = snapshot_directory or snapshots.SNAPSHOT_DIR
    by_id = {r.get("id"): r for r in raw.get("races") or [] if r.get("id")}
    report = {"updated": [], "skipped": []}

    for race_id, race in by_id.items():
        path = snapshots.snapshot_path(race_id, snapshot_directory)
        if not path.exists():
            report["skipped"].append({"race_id": race_id, "reason": "no_snapshot"})
            continue
        try:
            with path.open(encoding="utf-8") as f:
                snapshot = json.load(f)
        except (OSError, json.JSONDecodeError):
            report["skipped"].append({"race_id": race_id, "reason": "unreadable_snapshot"})
            continue
        if not snapshot.get("pre_race"):
            report["skipped"].append({"race_id": race_id, "reason": "not_pre_race"})
            continue

        post_at = snapshots.post_datetime(snapshot)
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

        # ここで変更してよいのはcontext系だけ。買い目・印・妙味・frozen_atは保持する。
        snapshot["context_layers"] = context
        snapshot["context_refreshed_at"] = now.isoformat(timespec="seconds")
        with path.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
            f.write("\n")
        report["updated"].append(race_id)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="発走前snapshotのcontextだけ更新")
    parser.add_argument("--week", required=True)
    args = parser.parse_args()

    path = RAW_DIR / f"{args.week}.json"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    report = refresh(raw)
    print(f"context refresh: updated={len(report['updated'])} skipped={len(report['skipped'])}")


if __name__ == "__main__":
    main()
