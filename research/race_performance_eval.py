"""
race-performance-v1 の forward 評価だけが使う集計と監査（PR-C2）。evaluation-only / observe-only。

事前登録: docs/research/RACE_PERFORMANCE_PREREG_V1.md §7（評価）・§12（監査項目）
集計の設定: config/model_evaluation_race_performance_v1_summary.json
    capture の照合キー（評価設定 config/model_evaluation_race_performance_v1.json の evaluation_config_hash）を
    変えないよう、評価設定とは別のファイルにした。

評価の定義（主指標・照合の契約・forward の境界）は変えない。research/model_evaluation.py から呼ぶ：
    capture_audit           … 発走前 capture の時点で残す監査値（speed と RPS のレース内相関・JRA 以外の重賞の有効走）
    snapshot_input_problem  … Challenger の snapshot と capture が同じ入力 raw から作られたか（§7.4 のモデルごとの照合）
    race_block              … レース単位の RPS の記録（因子の有効・無効、coverage、z の振れ、実効比重）
    summary_block           … 必ず併記する coverage、開催日単位の bootstrap、40レースの点検、§12 の監査の集計

このモジュールは予想経路（logic/・scraper/）から読まれない。出力は予想の入力にならない。
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Any

from logic import base_score, race_performance
from scraper.common import constants

GRADED_CLASSES = ("g1", "g2", "g3")


# ------------------------------------------------------------------ 発走前（capture の時点）

def capture_audit(race: dict[str, Any], horses: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """
    発走前 capture の時点で残す監査値（§12）。snapshot には各馬の speed や過去走の場が無いので、ここで残す。

    horses は prerace_capture._recompute_horses の出力（RPS を含めて再計算済み。各馬に speed_raw・
    speed_imputed・race_performance_raw・_past_runs がある）。

    - speed_rps_correlation：speed（補完でない値）と RPS の両方がある馬での、レース内の Pearson 相関。
      class は speed（基準タイムの class_offset）にも入っているので、2つの因子の重なりを見る。
      speed がレース全体で無効なら speed_not_used。
    - graded_usable_runs：RPS の有効走のうち重賞（g1〜g3）として数えた走。そのうち JRA 以外の場の走
      （地方交流の Jpn・海外）を non_jra_graded_usable_runs に数える。既存の正規化は Jpn を g と同格に丸めている。
    """
    speed_used = any(h.get("speed_raw") is not None for h in horses)
    pairs = [(float(h["speed_raw"]), float(h["race_performance_raw"])) for h in horses
             if h.get("speed_raw") is not None and not h.get("speed_imputed")
             and h.get("race_performance_raw") is not None]
    if not speed_used:
        correlation = {"status": "speed_not_used", "n": 0, "r": None}
    elif len(pairs) < 3:
        correlation = {"status": "too_few_horses", "n": len(pairs), "r": None}
    else:
        try:
            r = statistics.correlation([a for a, _ in pairs], [b for _, b in pairs])
            correlation = {"status": "computed", "n": len(pairs), "r": round(r, 6)}
        except statistics.StatisticsError:
            correlation = {"status": "constant_values", "n": len(pairs), "r": None}

    course = race.get("course") or {}
    today = {"surface": course.get("surface"), "dist": course.get("dist")}
    race_day = race_performance.race_date(race)
    usable = graded = non_jra = 0
    if race_day is not None:
        for horse in horses:
            runs = list(horse.get("_past_runs") or [])
            for run in race_performance.horse_rps(runs, today, race_day, config)["usable_runs"]:
                usable += 1
                if run["class"] in GRADED_CLASSES:
                    graded += 1
                    if runs[run["position"]].get("venue") not in constants.JRA_VENUES:
                        non_jra += 1
    return {"status": "recorded", "speed_rps_correlation": correlation, "usable_runs": usable,
            "graded_usable_runs": graded, "non_jra_graded_usable_runs": non_jra}


# ------------------------------------------------------------------ レース単位（結果確定後）

def snapshot_input_problem(snapshot: dict[str, Any], capture: dict[str, Any] | None) -> str | None:
    """
    Challenger の snapshot（ビルド時に記録した input_raw_hash）と、そのモデル自身の capture（capture 時に読んだ raw の
    input_raw_sha256）が同じ入力 raw から作られたか。§7.4 の「モデルごとの照合」の一部。

    capture_problem はモデル間で capture の raw が同じことを確かめる。これと合わせて、
    Challenger の snapshot・両モデルの capture が同じ入力 raw だとつながる。合わなければ評価しない（理由を残す）。
    """
    quality = snapshot.get("race_performance_quality")
    if not isinstance(quality, dict) or not quality.get("input_raw_hash"):
        return "race_performance_quality_missing"
    if capture is None:
        return "capture_missing"
    if quality["input_raw_hash"] != (capture.get("provenance") or {}).get("input_raw_sha256"):
        return "snapshot_input_raw_mismatch"
    return None


def max_abs_z(quality: dict[str, Any]) -> float | None:
    """RPS の z の最大の絶対値（本番と同じ z_standardize）。RPS がある馬が少ないほど大きく振れる（§12）。"""
    z = [v for v in base_score.z_standardize([h.get("rps_raw") for h in quality.get("horses") or []])
         if v is not None]
    return round(max(abs(v) for v in z), 6) if z else None


def effective_rps_weight(quality: dict[str, Any], speed_used: bool, config: dict[str, Any]) -> float:
    """
    合成での RPS の実効比重（レース単位の名目値。馬ごとの欠損による再正規化は含まない）。

    speed がレース全体で無効だと残りの因子で再正規化されるので、0.15 → 0.15 / 0.6175 ≈ 0.243 に上がる（§11 の8）。
    因子が無効なレースは 0。
    """
    if not quality.get("factor_active"):
        return 0.0
    w = config["integration"]["score_weights"]
    total = w["race_performance"] + w["aptitude"] + w["human"] + (w["speed"] if speed_used else 0.0)
    return round(w["race_performance"] / total, 6)


def race_block(snapshot: dict[str, Any], capture: dict[str, Any] | None, config: dict[str, Any]) -> dict[str, Any]:
    """レース単位の RPS の記録。値はすべて発走前の snapshot と capture から取る（結果の情報は入れない）。"""
    quality = snapshot.get("race_performance_quality") or {}
    speed_used = bool((snapshot.get("speed_quality") or {}).get("used"))
    return {
        "ref": snapshot.get("race_performance_ref"),
        "input_raw_hash": quality.get("input_raw_hash"),
        "factor_active": quality.get("factor_active"),
        "factor_inactive_reason": quality.get("factor_inactive_reason"),
        "field_size": quality.get("field_size"),
        "rps_available_horses": quality.get("rps_available_horses"),
        "rps_coverage": quality.get("rps_coverage"),
        "rps_sd": quality.get("rps_sd"),
        "max_abs_z": max_abs_z(quality),
        "excluded_by_reason_total": quality.get("excluded_by_reason_total") or {},
        "speed_used": speed_used,
        "effective_rps_weight": effective_rps_weight(quality, speed_used, config),
        # PR-C2 より前に取った capture には無い（その回は not_recorded のまま。後から作り直さない）
        "capture_audit": (capture or {}).get("race_performance_audit") or {"status": "not_recorded"},
    }


# ------------------------------------------------------------------ 累積

def _percentile(sorted_values: list[float], q: float) -> float:
    """線形補間の分位点（numpy の既定と同じ定義）。"""
    position = (len(sorted_values) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (position - lo)


def cluster_bootstrap(deltas: list[tuple[str, float]], settings: dict[str, Any]) -> dict[str, Any]:
    """
    レース単位の paired 差（Challenger − Champion）の平均を、まとまり（開催日）ごとに再抽出する bootstrap。

    同じ開催日のレースは一緒に抜き出すので、会場・開催日の中の相関を無視しない（§7.5）。
    乱数の種は設定で固定するので、同じデータからは同じ区間が出る。判定はしない（区間を記録するだけ）。
    """
    clusters: dict[str, list[float]] = {}
    for key, delta in deltas:
        clusters.setdefault(key, []).append(delta)
    keys = sorted(clusters)
    out: dict[str, Any] = {
        "cluster": settings["cluster"], "clusters": len(keys), "races": len(deltas),
        "replicates": settings["replicates"], "seed": settings["seed"], "interval_level": settings["interval"],
        "mean_delta": round(statistics.fmean(d for _, d in deltas), 6) if deltas else None,
    }
    if len(keys) < settings["min_clusters"]:
        return {**out, "status": "too_few_clusters", "interval": None}
    sums = [sum(clusters[k]) for k in keys]
    sizes = [len(clusters[k]) for k in keys]
    rng = random.Random(settings["seed"])
    stats = []
    for _ in range(settings["replicates"]):
        picks = [rng.randrange(len(keys)) for _ in keys]
        stats.append(sum(sums[i] for i in picks) / sum(sizes[i] for i in picks))
    stats.sort()
    lo, hi = (_percentile(stats, q) for q in settings["interval"])
    return {**out, "status": "computed", "interval": [round(lo, 6), round(hi, 6)]}


def _stats(values: list[float]) -> dict[str, Any]:
    return {"races": len(values),
            "mean": round(statistics.fmean(values), 6) if values else None,
            "min": round(min(values), 6) if values else None,
            "max": round(max(values), 6) if values else None}


def _count(values: list[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return dict(sorted(out.items()))


def summary_block(evaluated: list[dict[str, Any]], coverage: dict[str, Any], cfg: dict[str, Any],
                  settings: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """
    事前登録 §7 の「必ず併記」・不確実性・点検と、§12 の監査項目の集計。採否の判定はしない。

    evaluated は model_evaluation.evaluate_race の evaluated 行（Challenger の models に race_performance がある）。
    """
    prob_rows = [r for r in evaluated if r["probability"]["status"] == "evaluated"]
    blocks = [r["models"]["challenger"].get("race_performance") or {} for r in evaluated]
    audits = [b.get("capture_audit") or {} for b in blocks]
    recorded = [a for a in audits if a.get("status") == "recorded"]
    correlations = [a["speed_rps_correlation"] for a in recorded]
    computed_r = [c["r"] for c in correlations if c.get("status") == "computed"]
    active = [b for b in blocks if b.get("factor_active")]

    excluded: dict[str, int] = {}
    for block in blocks:
        for reason, count in (block.get("excluded_by_reason_total") or {}).items():
            excluded[reason] = excluded.get(reason, 0) + int(count)

    def deltas(metric: str) -> list[tuple[str, float]]:
        return [(str(r["race_id"])[:8], r["probability"]["models"]["challenger"][metric]
                 - r["probability"]["models"]["champion"][metric]) for r in prob_rows]

    checkpoint = int(cfg["first_checkpoint_paired_races"])
    return {
        "settings": {"version": settings["version"]},
        # 事前登録 §7.2：主指標と一緒に必ず出す
        "always_report": {
            "paired_valid_race_rate": coverage.get("coverage_rate"),
            "probability_evaluated_rate": round(len(prob_rows) / len(evaluated), 4) if evaluated else None,
            "score_coverage": _stats([float(r["probability"]["evaluation_set"]["coverage"]) for r in evaluated
                                      if r["probability"].get("evaluation_set")]),
            "rps_coverage": _stats([float(b["rps_coverage"]) for b in blocks if b.get("rps_coverage") is not None]),
        },
        # 事前登録 §7.5：開催日単位の bootstrap（判定はしない）
        "uncertainty": {metric: cluster_bootstrap(deltas(metric), settings["uncertainty"])
                        for metric in settings["uncertainty"]["metrics"]},
        # 事前登録 §7.5：最初の運用点検。昇格の閾値ではない
        "checkpoint": {
            "first_checkpoint_paired_races": checkpoint,
            "evaluated_pairs": len(evaluated),
            "probability_pairs": len(prob_rows),
            "reached": len(evaluated) >= checkpoint,
            "note": config["evaluation"]["checkpoint_note"],
        },
        # 事前登録 §12：forward で必ず見る監査項目
        "audit": {
            "factor_active_races": len(active),
            "factor_inactive_reasons": _count([b.get("factor_inactive_reason") for b in blocks
                                               if b.get("factor_active") is False]),
            "excluded_by_reason_total": dict(sorted(excluded.items())),
            "z_expansion": {
                "max_abs_z": _stats([float(b["max_abs_z"]) for b in active if b.get("max_abs_z") is not None]),
                "rps_available_horses": _stats([float(b["rps_available_horses"]) for b in active
                                                if b.get("rps_available_horses") is not None]),
            },
            "effective_rps_weight": {
                "speed_used": _stats([b["effective_rps_weight"] for b in active if b.get("speed_used")]),
                "speed_not_used": _stats([b["effective_rps_weight"] for b in active if not b.get("speed_used")]),
            },
            "speed_rps_correlation": {
                "races_recorded": len(recorded),
                "races_not_recorded": len(audits) - len(recorded),
                "status": _count([c.get("status") for c in correlations]),
                "r": _stats(computed_r),
            },
            "graded_usable_runs": {
                "races_recorded": len(recorded),
                "usable_runs": sum(int(a.get("usable_runs") or 0) for a in recorded),
                "graded_usable_runs": sum(int(a.get("graded_usable_runs") or 0) for a in recorded),
                "non_jra_graded_usable_runs": sum(int(a.get("non_jra_graded_usable_runs") or 0) for a in recorded),
            },
        },
        "cross_check_note": config["evaluation"]["cross_check"],
    }
