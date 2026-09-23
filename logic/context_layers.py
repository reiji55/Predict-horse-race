"""予想を壊さずに増やす「当日コンテキスト」観測レイヤー。

Layer 1 = 既存の基礎能力モデル（speed / aptitude / human / Top3）
Layer 2 = 当日コンディション（馬体重・休養・馬場定量値・オッズ推移）
Layer 3 = 直前生体所見（パドック）

v1 は observe_only。計算結果を predictions/snapshot に残すが、
base_score / p / 妙味 / 買い目には一切加点しない。
"""
from __future__ import annotations

import datetime
import json
import math
import statistics
from pathlib import Path
from typing import Any

from logic import odds_history, paddock

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "context_layers.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as f:
        return json.load(f)


def _weight_value(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def body_weight_profile(entry: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    current = _weight_value(entry.get("body_weight"))
    history = [
        v for v in (_weight_value(run.get("body_weight")) for run in (entry.get("past_runs") or []))
        if v is not None
    ]
    min_history = int(config.get("min_history", 3))
    out: dict[str, Any] = {
        "current": current,
        "history": history,
        "n_history": len(history),
        "last": history[0] if history else None,
        "median": None,
        "change_from_last": None,
        "deviation_from_median": None,
        "deviation_pct": None,
        "robust_z": None,
        "unusual": False,
        "quality": "insufficient" if len(history) < min_history else "usable",
    }
    if current is None or not history:
        return out

    median = float(statistics.median(history))
    out["median"] = round(median, 2)
    out["change_from_last"] = round(current - history[0], 2)
    out["deviation_from_median"] = round(current - median, 2)
    out["deviation_pct"] = round((current - median) / median, 6) if median else None

    if len(history) >= min_history:
        abs_dev = [abs(x - median) for x in history]
        mad = float(statistics.median(abs_dev))
        if mad > 0:
            robust_z = (current - median) / (1.4826 * mad)
            out["robust_z"] = round(robust_z, 4)
        pct_flag = (
            out["deviation_pct"] is not None
            and abs(out["deviation_pct"]) >= float(config.get("unusual_abs_pct", 0.04))
        )
        z_flag = (
            out["robust_z"] is not None
            and abs(out["robust_z"]) >= float(config.get("robust_z_threshold", 2.5))
        )
        out["unusual"] = bool(pct_flag or z_flag)
    return out


def rest_profile(entry: dict[str, Any], race_date: str | None,
                 config: dict[str, Any]) -> dict[str, Any]:
    latest = next((r for r in (entry.get("past_runs") or []) if r.get("date")), None)
    if not latest or not race_date:
        return {"days": None, "bucket": "unknown"}
    try:
        today = datetime.date.fromisoformat(race_date)
        previous = datetime.date.fromisoformat(str(latest["date"]))
    except ValueError:
        return {"days": None, "bucket": "unknown"}
    days = (today - previous).days
    if days < 0:
        return {"days": days, "bucket": "invalid"}
    if days <= int(config.get("quick_return_max_days", 14)):
        bucket = "quick_return"
    elif days >= int(config.get("layoff_min_days", 56)):
        bucket = "layoff"
    else:
        bucket = "normal"
    return {"days": days, "bucket": bucket}


def odds_movement_summary(race_id: str,
                          directory: Path | None = None) -> dict[str, Any]:
    observations = odds_history.load_observations(race_id, directory)
    if len(observations) < 2:
        return {"n_observations": len(observations), "horses": []}

    def map_obs(obs: dict[str, Any]) -> dict[int, float]:
        return {
            int(row["num"]): float(row["win_odds"])
            for row in obs.get("odds") or []
            if row.get("num") is not None and row.get("win_odds") not in (None, 0)
        }

    first, last = map_obs(observations[0]), map_obs(observations[-1])
    rows = []
    for num in sorted(set(first) & set(last)):
        a, b = first[num], last[num]
        rows.append({
            "num": num,
            "first_odds": a,
            "last_odds": b,
            "odds_ratio": round(b / a, 6) if a else None,
            # 正なら支持が強まった（オッズが下がった）
            "support_shift": round(math.log(a / b), 6) if a > 0 and b > 0 else None,
        })
    return {
        "n_observations": len(observations),
        "first_observed_at": observations[0].get("observed_at"),
        "last_observed_at": observations[-1].get("observed_at"),
        "horses": rows,
    }


def build_context(race: dict[str, Any], config: dict[str, Any] | None = None,
                  odds_directory: Path | None = None,
                  paddock_directory: Path | None = None) -> dict[str, Any]:
    config = config or load_config()
    bw_cfg = config.get("body_weight") or {}
    rest_cfg = config.get("rest") or {}
    race_date = race.get("date")
    if not race_date:
        race_id = str(race.get("id") or "")
        if len(race_id) >= 8 and race_id[:8].isdigit():
            race_date = f"{race_id[:4]}-{race_id[4:6]}-{race_id[6:8]}"

    horses = []
    for entry in race.get("entries") or []:
        horses.append({
            "num": entry.get("num"),
            "name": entry.get("name"),
            "body_weight": body_weight_profile(entry, bw_cfg),
            "rest": rest_profile(entry, race_date, rest_cfg),
        })

    paddock_payload = paddock.load_verified(
        race, config.get("paddock") or {}, directory=paddock_directory
    )

    return {
        "version": config.get("version"),
        "mode": config.get("mode", "observe_only"),
        "layer1": {"status": "active", "source": "existing_prediction_model"},
        "layer2": {
            "status": "observe_only",
            "track_metrics": race.get("track_metrics"),
            "odds_movement": odds_movement_summary(race.get("id") or "", odds_directory),
            "horses": horses,
        },
        "layer3": {
            "status": "observe_only" if paddock_payload else "unobserved",
            "paddock": paddock_payload,
        },
    }
