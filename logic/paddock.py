"""発走前パドック所見の保存・検証。

映像そのものをこのリポジトリへ無断保存/再配布する仕組みは持たない。
代わりに、人間・ChatGPT Vision・将来の許諾済みCV処理が共通で出力できる
少数の構造化特徴量を受け取る。

data/paddock/{race_id}.json 例:
{
  "race_id": "...",
  "observed_at": "2026-09-26T15:05:00+09:00",
  "source": "manual" | "chatgpt_vision" | "licensed_cv",
  "observer": "...",
  "horses": [
    {
      "num": 4,
      "gait_symmetry": 0.9,
      "stride_fluency": 0.8,
      "sweat": 0.2,
      "agitation": 0.1,
      "coat_condition": 0.8,
      "confidence": 0.8,
      "notes": "..."
    }
  ]
}

値は0〜1。現段階では予想スコアへ加点せず、観測・後方検証専用。
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

from logic import snapshots

logger = logging.getLogger("logic.paddock")

ROOT = Path(__file__).resolve().parent.parent
PADDOCK_DIR = ROOT / "data" / "paddock"
JST = snapshots.JST

FEATURES = ("gait_symmetry", "stride_fluency", "sweat", "agitation", "coat_condition")


def _parse_time(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=JST)


def _bounded(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, v))


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    out = {
        "race_id": payload.get("race_id"),
        "observed_at": payload.get("observed_at"),
        "source": payload.get("source") or "unknown",
        "observer": payload.get("observer"),
        "horses": [],
    }
    for row in payload.get("horses") or []:
        if row.get("num") is None:
            continue
        clean = {"num": int(row["num"])}
        for key in FEATURES:
            clean[key] = _bounded(row.get(key))
        clean["confidence"] = _bounded(row.get("confidence"))
        clean["notes"] = str(row.get("notes") or "")[:500]
        out["horses"].append(clean)
    return out


def validate_pre_race(payload: dict[str, Any], race: dict[str, Any],
                      max_minutes_before_post: int = 60) -> tuple[bool, str]:
    """発走後入力と、古すぎて当日直前所見と言えない入力を排除する。"""
    if payload.get("race_id") != race.get("id"):
        return False, "race_id_mismatch"
    observed_at = _parse_time(payload.get("observed_at"))
    post_at = snapshots.post_datetime(race)
    if observed_at is None:
        return False, "invalid_observed_at"
    if post_at is None:
        return False, "unknown_post_time"
    if observed_at >= post_at:
        return False, "after_post"
    minutes = (post_at - observed_at).total_seconds() / 60
    if minutes > max_minutes_before_post:
        return False, "too_early"
    return True, "ok"


def load_verified(race: dict[str, Any], config: dict[str, Any],
                  directory: Path | None = None) -> dict[str, Any] | None:
    race_id = race.get("id")
    if not race_id:
        return None
    path = (directory or PADDOCK_DIR) / f"{race_id}.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            payload = normalize(json.load(f))
    except (OSError, json.JSONDecodeError):
        logger.warning("パドック所見を読めませんでした: %s", path, exc_info=True)
        return None

    ok, reason = validate_pre_race(
        payload, race,
        max_minutes_before_post=int(config.get("max_minutes_before_post", 60)),
    )
    if not ok:
        logger.warning("%s: パドック所見を採用しません reason=%s", race_id, reason)
        return None
    return {**payload, "verified_pre_race": True}
