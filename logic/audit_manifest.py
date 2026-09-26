"""Context Layers監査用の発走前manifest。

Prediction snapshot / Context snapshot / Odds observation を、発走前の同一時点から
参照できるように束ね、各ファイルの SHA-256 と発走までの残り時間を保存する。

重要:
- 予想ロジックには一切影響しない（observe_only）。
- manifest 自体も1観測1ファイルで追記し、既存ファイルを書き換えない。
- これはリポジトリ内の整合性を検証する内部監査証跡であり、外部タイムスタンプ機関による
  第三者証明ではない。Git履歴と併用して「何がいつ存在していたか」を追跡する。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from logic import context_layers, odds_history, snapshots

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
PREDICTION_SNAPSHOT_DIR = ROOT / "data" / "snapshots"
CONTEXT_SNAPSHOT_DIR = ROOT / "data" / "context_snapshots"
ODDS_HISTORY_DIR = ROOT / "data" / "odds_history"
MANIFEST_DIR = ROOT / "data" / "audit_manifests"
JST = snapshots.JST


def _parse_dt(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("/", "-"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def _minutes_to_post(post_at: datetime.datetime,
                     observed_at: datetime.datetime | None) -> float | None:
    if observed_at is None:
        return None
    return round((post_at - observed_at).total_seconds() / 60.0, 3)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def classify_odds_freshness(minutes_to_post: float | None,
                            config: dict[str, Any] | None = None) -> dict[str, Any]:
    """最終オッズの時刻品質。

    source_time を優先して評価する。5〜15分前が target_window。
    5分未満もデータとしては新鮮なので stale にはせず very_late と分ける。
    15〜30分前は acceptable、30分超は stale。
    """
    cfg = (config or {}).get("audit") or {}
    target_min = float(cfg.get("odds_target_min_minutes", 5))
    target_max = float(cfg.get("odds_target_max_minutes", 15))
    stale_after = float(cfg.get("odds_stale_after_minutes", 30))

    if minutes_to_post is None:
        status = "unknown"
        fresh = False
        target = False
    elif minutes_to_post < 0:
        status = "after_post"
        fresh = False
        target = False
    elif minutes_to_post < target_min:
        status = "very_late"
        fresh = True
        target = False
    elif minutes_to_post <= target_max:
        status = "target_window"
        fresh = True
        target = True
    elif minutes_to_post <= stale_after:
        status = "acceptable"
        fresh = True
        target = False
    else:
        status = "stale"
        fresh = False
        target = False

    return {
        "status": status,
        "fresh": fresh,
        "target_window": target,
        "minutes_to_post": minutes_to_post,
        "target_min_minutes": target_min,
        "target_max_minutes": target_max,
        "stale_after_minutes": stale_after,
    }


def _latest_prediction(race: dict[str, Any], post_at: datetime.datetime,
                       as_of: datetime.datetime,
                       directory: Path) -> tuple[Path, dict[str, Any]] | None:
    path = directory / f"{race.get('id')}.json"
    payload = _read_json(path)
    if not payload or payload.get("pre_race") is not True:
        return None
    frozen_at = _parse_dt(payload.get("frozen_at"))
    if frozen_at is None or frozen_at >= post_at or frozen_at > as_of:
        return None
    return path, payload


def _latest_context(race: dict[str, Any], post_at: datetime.datetime,
                    as_of: datetime.datetime,
                    directory: Path) -> tuple[Path, dict[str, Any]] | None:
    race_dir = directory / str(race.get("id") or "")
    if not race_dir.is_dir():
        return None
    candidates: list[tuple[datetime.datetime, Path, dict[str, Any]]] = []
    for path in sorted(race_dir.glob("*.json")):
        payload = _read_json(path)
        if not payload or payload.get("pre_race") is not True:
            continue
        observed = _parse_dt(payload.get("observed_at"))
        if observed is None or observed >= post_at or observed > as_of:
            continue
        candidates.append((observed, path, payload))
    if not candidates:
        return None
    _, path, payload = max(candidates, key=lambda row: row[0])
    return path, payload


def _latest_odds(race: dict[str, Any], post_at: datetime.datetime,
                 as_of: datetime.datetime,
                 directory: Path) -> tuple[Path, dict[str, Any]] | None:
    race_dir = directory / str(race.get("id") or "")
    if not race_dir.is_dir():
        return None
    candidates: list[tuple[datetime.datetime, Path, dict[str, Any]]] = []
    for path in sorted(race_dir.glob("*.json")):
        payload = _read_json(path)
        if not payload:
            continue
        observed = _parse_dt(payload.get("observed_at"))
        source_at = odds_history.parse_source_time(payload.get("source_time"))
        if observed is None or observed >= post_at or observed > as_of:
            continue
        if source_at is not None and source_at >= post_at:
            continue
        candidates.append((observed, path, payload))
    if not candidates:
        return None
    _, path, payload = max(candidates, key=lambda row: row[0])
    return path, payload


def _artifact(path: Path, payload: dict[str, Any],
              post_at: datetime.datetime, time_key: str) -> dict[str, Any]:
    observed = _parse_dt(payload.get(time_key))
    return {
        "path": _display_path(path),
        "sha256": _sha256(path),
        time_key: payload.get(time_key),
        "minutes_to_post": _minutes_to_post(post_at, observed),
    }


def _context_completeness(payload: dict[str, Any] | None) -> dict[str, Any]:
    context = (payload or {}).get("context_layers") or {}
    layer2 = context.get("layer2") or {}
    horses = layer2.get("horses") or []
    current = [
        row for row in horses
        if ((row.get("body_weight") or {}).get("current") is not None)
    ]
    odds_rows = ((layer2.get("odds_movement") or {}).get("horses") or [])
    paddock = ((context.get("layer3") or {}).get("paddock"))
    total = len(horses)
    return {
        "horse_rows": total,
        "body_weight_current": len(current),
        "body_weight_current_coverage": (
            round(len(current) / total, 4) if total else None
        ),
        "track_metrics_present": layer2.get("track_metrics") is not None,
        "odds_movement_horses": len(odds_rows),
        "paddock_observed": paddock is not None,
    }


def build_manifest(race: dict[str, Any], now: datetime.datetime,
                   phase: str,
                   prediction_directory: Path | None = None,
                   context_directory: Path | None = None,
                   odds_directory: Path | None = None,
                   config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    post_at = snapshots.post_datetime(race)
    if post_at is None or now >= post_at:
        return None

    prediction = _latest_prediction(
        race, post_at, now, prediction_directory or PREDICTION_SNAPSHOT_DIR
    )
    context = _latest_context(
        race, post_at, now, context_directory or CONTEXT_SNAPSHOT_DIR
    )
    odds = _latest_odds(
        race, post_at, now, odds_directory or ODDS_HISTORY_DIR
    )

    pred_info = None
    if prediction:
        path, payload = prediction
        pred_info = _artifact(path, payload, post_at, "frozen_at")

    context_info = None
    context_payload = None
    if context:
        path, context_payload = context
        context_info = _artifact(path, context_payload, post_at, "observed_at")

    odds_info = None
    freshness = classify_odds_freshness(None, config)
    if odds:
        path, payload = odds
        odds_info = _artifact(path, payload, post_at, "observed_at")
        source_at = odds_history.parse_source_time(payload.get("source_time"))
        source_minutes = _minutes_to_post(post_at, source_at)
        odds_info.update({
            "source_time": payload.get("source_time"),
            "source_minutes_to_post": source_minutes,
            "rows": len(payload.get("odds") or []),
        })
        freshness = classify_odds_freshness(
            source_minutes if source_minutes is not None
            else odds_info.get("minutes_to_post"),
            config,
        )

    completeness = _context_completeness(context_payload)
    completeness.update({
        "prediction_snapshot_present": pred_info is not None,
        "context_snapshot_present": context_info is not None,
        "odds_snapshot_present": odds_info is not None,
    })

    return {
        "version": "context-audit-manifest-v1",
        "race_id": race.get("id"),
        "post_time": race.get("post_time"),
        "observed_at": now.isoformat(timespec="seconds"),
        "phase": phase,
        "pre_race": True,
        "proof_scope": "internal_hash_manifest",
        "artifacts": {
            "prediction": pred_info,
            "context": context_info,
            "odds": odds_info,
        },
        "odds_freshness": freshness,
        "context_completeness": completeness,
    }


def capture(raw: dict[str, Any], now: datetime.datetime | None = None,
            phase: str = "audit",
            output_directory: Path | None = None,
            prediction_directory: Path | None = None,
            context_directory: Path | None = None,
            odds_directory: Path | None = None,
            config: dict[str, Any] | None = None) -> dict[str, Any]:
    now = now or datetime.datetime.now(JST)
    output_directory = output_directory or MANIFEST_DIR
    config = config or context_layers.load_config()
    report: dict[str, Any] = {"added": [], "skipped": []}

    for race in raw.get("races") or []:
        race_id = race.get("id")
        if not race_id:
            continue
        manifest = build_manifest(
            race, now, phase,
            prediction_directory=prediction_directory,
            context_directory=context_directory,
            odds_directory=odds_directory,
            config=config,
        )
        if manifest is None:
            report["skipped"].append({"race_id": race_id, "reason": "not_pre_race"})
            continue

        race_dir = output_directory / race_id
        race_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(JST).strftime("%Y%m%dT%H%M%S")
        path = race_dir / f"{stamp}_{phase}.json"
        if path.exists():
            report["skipped"].append({"race_id": race_id, "reason": "duplicate_time"})
            continue
        with path.open("w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.write("\n")
        report["added"].append(race_id)
    return report


def load_manifests(race_id: str, directory: Path | None = None) -> list[dict[str, Any]]:
    race_dir = (directory or MANIFEST_DIR) / race_id
    if not race_dir.is_dir():
        return []
    rows = []
    for path in sorted(race_dir.glob("*.json")):
        payload = _read_json(path)
        if payload:
            rows.append(payload)
    return sorted(rows, key=lambda row: row.get("observed_at") or "")


def latest_manifest(race_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    rows = []
    for payload in load_manifests(race_id, directory):
        post_at = snapshots.post_datetime({
            "race_id": race_id,
            "post_time": payload.get("post_time"),
        })
        observed = _parse_dt(payload.get("observed_at"))
        if (payload.get("pre_race") is True and post_at is not None
                and observed is not None and observed < post_at):
            rows.append(payload)
    return rows[-1] if rows else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Context Layers発走前監査manifestを保存")
    parser.add_argument("--week", required=True)
    parser.add_argument("--phase", default="audit")
    args = parser.parse_args()

    path = RAW_DIR / f"{args.week}.json"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    report = capture(raw, phase=args.phase)
    print(f"audit manifest: added={len(report['added'])} skipped={len(report['skipped'])}")


if __name__ == "__main__":
    main()
