"""
Race Performance v1（class＋margin の成績内容因子）。Challenger race-performance-v1 だけが使う。

事前登録: docs/research/RACE_PERFORMANCE_PREREG_V1.md / config/race_performance_v1.json（PR #28）
- 式・パラメータは登録した設定ファイルから読むだけで、ここに数値を書かない（登録と食い違わないため）。
- Champion・既存の Challenger はこのモジュールを呼ばない（logic/build_predictions.compute_model_base_scores）。
- 入力は raw の past_runs（いまの予想の入力そのまま）と、今日のレースの surface・距離・日付だけ。
  finish・オッズ・研究用の長期履歴は読まない。

--- 1頭の RPS（文書 §4） ---

    有効走   … past_runs を先頭から見て、除外理由（exclusion_reasons の順）に1つも当たらない走
    v_run    = class_strength[class] × exp(−max(0, margin_sec) / tau_sec)
    w_recency = recency_by_input_position[入力位置]      # 無効な走を詰めて位置をずらさない
    w_dist   = 1 − |run.dist − today.dist| / dist_decay_m
    rps_raw  = Σ(v_run × w_recency × w_dist) / Σ(w_recency × w_dist)   # 有効走が min_usable_runs 未満なら null

--- レース単位（文書 §5.2） ---

RPS が null でない馬が min_horses_with_rps 頭未満、またはレース内の母標準偏差が 0 なら、
そのレースでは因子ごと無効（factor_active=false）。合成は Champion と同じ3因子の重みそのものに戻す。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import statistics
from typing import Any

FACTOR_RAW_KEY = "race_performance_raw"

INACTIVE_TOO_FEW = "fewer_than_min_horses_with_rps"
INACTIVE_ZERO_SD = "zero_sd"


def canonical_sha256(payload: Any) -> str:
    """キーの順番・空白に依存しない SHA-256（model_registry._canonical_sha256 と同じ正規化）。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def race_date(race: dict[str, Any]) -> datetime.date | None:
    """race id の先頭8桁（YYYYMMDD）。読めなければ None（そのレースの RPS は全馬 null）。"""
    race_id = str(race.get("id") or "")
    if len(race_id) < 8 or not race_id[:8].isdigit():
        return None
    try:
        return datetime.datetime.strptime(race_id[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _run_date(value: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value or "").replace("/", "-"))
    except ValueError:
        return None


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def exclusion_reason(position: int, run: Any, today: dict[str, Any], race_day: datetime.date,
                     config: dict[str, Any]) -> str | None:
    """その走を除外する理由。config の exclusion_reasons と同じ順に見て、最初に当たったものだけを返す。"""
    usable = config["usable_run"]
    run = run if isinstance(run, dict) else {}
    if position >= config["inputs"]["max_runs"]:
        return "beyond_max_runs"
    run_day = _run_date(run.get("date"))
    if run_day is None:
        return "date_invalid"
    if run_day == race_day:
        return "same_day"
    if run_day > race_day:
        return "future"
    flat = usable["flat_surfaces"]
    if today.get("surface") not in flat:
        return "today_not_flat"
    if run.get("surface") not in flat:
        return "run_not_flat"
    if run.get("surface") != today.get("surface"):
        return "surface_mismatch"
    if not _finite_number(run.get("dist")) or not _finite_number(today.get("dist")):
        return "dist_missing"
    if abs(run["dist"] - today["dist"]) > usable["max_abs_dist_diff_m"]:
        return "dist_out_of_range"
    if run.get("class") not in config["class_strength"]:
        return "class_unknown"
    margin = run.get("margin_sec")
    if margin is None:
        return "margin_missing"
    if not _finite_number(margin):
        return "margin_invalid"
    return None


def run_value(cls: str, margin_sec: float, config: dict[str, Any]) -> float:
    """v_run = class_strength × exp(−max(0, margin) / tau)。負の margin は 0 として扱う。"""
    m = max(0.0, float(margin_sec))
    return config["class_strength"][cls] * math.exp(-m / config["margin"]["tau_sec"])


def horse_rps(past_runs: list[Any] | None, today: dict[str, Any], race_day: datetime.date,
              config: dict[str, Any]) -> dict[str, Any]:
    """1頭の RPS と監査用の記録（文書 §6 の馬ごとの項目）。"""
    runs = list(past_runs or [])
    recency = config["weights"]["recency_by_input_position"]
    decay = config["weights"]["dist_decay_m"]
    excluded: dict[str, int] = {}
    usable: list[dict[str, Any]] = []
    for position, run in enumerate(runs):
        reason = exclusion_reason(position, run, today, race_day, config)
        if reason is not None:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        margin = run["margin_sec"]
        usable.append({
            "position": position,
            "date": run.get("date"),
            "class": run["class"],
            "margin_sec": margin,
            "dist": run["dist"],
            "margin_clamped_negative": margin < 0,
            "v_run": run_value(run["class"], margin, config),
            "w_recency": recency[position],
            "w_dist": 1 - abs(run["dist"] - today["dist"]) / decay,
        })

    rps = None
    if len(usable) >= config["aggregate"]["min_usable_runs"]:
        numerator = sum(u["v_run"] * u["w_recency"] * u["w_dist"] for u in usable)
        denominator = sum(u["w_recency"] * u["w_dist"] for u in usable)
        rps = numerator / denominator
    return {
        "rps_raw": rps,
        "n_input_runs": len(runs),
        "n_usable": len(usable),
        "excluded_by_reason": excluded,
        "margin_clamped_negative_runs": sum(1 for u in usable if u["margin_clamped_negative"]),
        "usable_runs": usable,
    }


def factor_activation(values: list[float | None], config: dict[str, Any]) -> tuple[bool, str | None, float | None]:
    """(factor_active, factor_inactive_reason, レース内の母標準偏差)。文書 §5.2。"""
    rule = config["integration"]["factor_activation"]
    available = [v for v in values if v is not None]
    sd = statistics.pstdev(available) if available else None
    if len(available) < rule["min_horses_with_rps"]:
        return False, INACTIVE_TOO_FEW, sd
    if rule["requires_positive_sd"] and sd == 0:
        return False, INACTIVE_ZERO_SD, sd
    return True, None, sd


def apply(race: dict[str, Any], horses: list[dict[str, Any]], config: dict[str, Any],
          ref: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    各馬に race_performance_raw を書き込み、レース単位の記録（race_performance_quality）を返す。

    horses は build_predictions.prepare_horses の出力（各馬の `_past_runs` が raw の past_runs そのもの）。
    raw は書き換えない。factor_active=false のときも各馬の値は監査用に残す（合成には使わない）。
    """
    course = race.get("course") or {}
    today = {"surface": course.get("surface"), "dist": course.get("dist")}
    race_day = race_date(race)

    rows = []
    for horse in horses:
        if race_day is None:
            record = {"rps_raw": None, "n_input_runs": len(horse.get("_past_runs") or []), "n_usable": 0,
                      "excluded_by_reason": {}, "margin_clamped_negative_runs": 0, "usable_runs": []}
        else:
            record = horse_rps(horse.get("_past_runs"), today, race_day, config)
        horse[FACTOR_RAW_KEY] = record["rps_raw"]
        rows.append({"num": horse.get("num"), **record})

    active, reason, sd = factor_activation([r["rps_raw"] for r in rows], config)
    totals: dict[str, int] = {}
    for row in rows:
        for key, count in row["excluded_by_reason"].items():
            totals[key] = totals.get(key, 0) + count
    available = sum(1 for r in rows if r["rps_raw"] is not None)
    return {
        "version": config["version"],
        "config_sha256": (ref or {}).get("config_sha256") or canonical_sha256(config),
        "input_raw_hash": canonical_sha256(race),
        "race_date": race_day.isoformat() if race_day else None,
        "field_size": len(rows),
        "rps_available_horses": available,
        "rps_coverage": available / len(rows) if rows else 0.0,
        "rps_sd": sd,
        "factor_active": active,
        "factor_inactive_reason": reason,
        "excluded_by_reason_total": totals,
        "horses": rows,
    }
