"""「想定外に好走した馬」を自動で研究キューへ送る。

これは自動学習・自動チューニングではない。
発走前にモデル/市場が低評価だったのに上位へ来た馬を抽出し、
発走前に凍結済みのcontext（馬体重・休養・馬場・オッズ推移・パドック）を
同じ場所に束ねて「何を見落とした可能性があるか」を人間/AIが後から検証しやすくする。

相関を原因と断定しない。すべて hypothesis_only。
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Any

from logic import context_layers, refresh_context

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = ROOT / "data" / "results.json"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "anomaly_report.json"
JST = datetime.timezone(datetime.timedelta(hours=9))


def _model_ranks(marks: list[dict[str, Any]]) -> dict[int, int]:
    # marks は発走前base_score降順で保存されている。念のためscoreで並べ直す。
    usable = [m for m in marks if m.get("num") is not None and m.get("score") is not None]
    usable.sort(key=lambda m: float(m["score"]), reverse=True)
    return {int(m["num"]): rank for rank, m in enumerate(usable, start=1)}


def _market_ranks(marks: list[dict[str, Any]]) -> dict[int, int]:
    usable = [m for m in marks if m.get("num") is not None and m.get("odds") not in (None, 0)]
    usable.sort(key=lambda m: float(m["odds"]))
    return {int(m["num"]): rank for rank, m in enumerate(usable, start=1)}


def _mark_by_num(marks: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(m["num"]): m for m in marks if m.get("num") is not None}


def _horse_context(context: dict[str, Any] | None, num: int) -> dict[str, Any] | None:
    if not context:
        return None
    layer2 = context.get("layer2") or {}
    horse = next((h for h in layer2.get("horses") or [] if h.get("num") == num), None)
    movement = next(
        (h for h in (layer2.get("odds_movement") or {}).get("horses") or []
         if h.get("num") == num),
        None,
    )
    paddock_payload = ((context.get("layer3") or {}).get("paddock") or {})
    paddock_row = next(
        (h for h in paddock_payload.get("horses") or [] if h.get("num") == num),
        None,
    )
    return {
        "body_weight": (horse or {}).get("body_weight"),
        "rest": (horse or {}).get("rest"),
        "track_metrics": layer2.get("track_metrics"),
        "odds_movement": movement,
        "paddock": paddock_row,
        "context_version": context.get("version"),
    }


def build_report(results: dict[str, Any], config: dict[str, Any] | None = None,
                 context_snapshot_directory: Path | None = None) -> dict[str, Any]:
    config = config or context_layers.load_config()
    cfg = config.get("anomaly_review") or {}
    finish_max = int(cfg.get("finish_max", 3))
    model_rank_min = int(cfg.get("model_rank_min", 6))
    market_rank_min = int(cfg.get("market_rank_min", 6))

    anomalies = []
    races_scanned = 0
    for race in results.get("results") or []:
        if race.get("evaluation_scope") == "manual_chat":
            continue
        races_scanned += 1
        race_id = race.get("race_id")
        meta = race.get("meta") or {}
        marks = meta.get("marks") or []
        model_ranks = _model_ranks(marks)
        market_ranks = _market_ranks(marks)
        by_num = _mark_by_num(marks)

        context_snapshot = refresh_context.latest_context_snapshot(
            race_id or "", context_snapshot_directory
        )
        frozen_context = (
            (context_snapshot or {}).get("context_layers")
            or race.get("context_layers")
        )

        for finish_pos, num in enumerate((race.get("finish") or [])[:finish_max], start=1):
            try:
                num = int(num)
            except (TypeError, ValueError):
                continue
            model_rank = model_ranks.get(num)
            market_rank = market_ranks.get(num)
            reasons = []
            if model_rank is not None and model_rank >= model_rank_min:
                reasons.append("model_miss")
            if market_rank is not None and market_rank >= market_rank_min:
                reasons.append("market_miss")
            if not reasons:
                continue

            mark = by_num.get(num) or {}
            anomalies.append({
                "race_id": race_id,
                "venue": meta.get("venue"),
                "race_no": meta.get("race_no"),
                "race_name": meta.get("name"),
                "finish": finish_pos,
                "num": num,
                "name": mark.get("name"),
                "model_rank": model_rank,
                "market_rank": market_rank,
                "pre_race_odds": mark.get("odds"),
                "score": mark.get("score"),
                "reasons": reasons,
                "race_regime": race.get("race_regime"),
                "context_observed_at": (context_snapshot or {}).get("observed_at"),
                "context": _horse_context(frozen_context, num),
                "interpretation": "hypothesis_only",
            })

    by_reason: dict[str, int] = {}
    for row in anomalies:
        for reason in row["reasons"]:
            by_reason[reason] = by_reason.get(reason, 0) + 1

    return {
        "generated_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
        "version": config.get("version"),
        "status": "hypothesis_only",
        "rules": {
            "finish_max": finish_max,
            "model_rank_min": model_rank_min,
            "market_rank_min": market_rank_min,
        },
        "summary": {
            "races_scanned": races_scanned,
            "anomalies": len(anomalies),
            "by_reason": by_reason,
        },
        "anomalies": anomalies,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="想定外好走馬の研究キューを生成")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    with open(args.results, encoding="utf-8") as f:
        results = json.load(f)
    report = build_report(results)

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(
        f"anomaly review: races={report['summary']['races_scanned']} "
        f"anomalies={report['summary']['anomalies']}"
    )


if __name__ == "__main__":
    main()
