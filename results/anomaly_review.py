"""「想定外に好走した馬」とContext Layersの監査証跡を結果後に自動生成する。

これは自動学習・自動チューニングではない。
発走前にモデル/市場が低評価だったのに上位へ来た馬を抽出し、発走前に凍結済みの
context（馬体重・休養・馬場・オッズ推移・パドック）を束ねる。

加えて、各レースについて Prediction / Context / Odds の発走前証跡、最終オッズの鮮度、
Contextの欠損率、実着上位馬にどの情報が付いていたかを race_audits に残す。

相関を原因と断定しない。すべて hypothesis_only。
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Any

from logic import audit_manifest, context_layers, refresh_context

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = ROOT / "data" / "results.json"
DEFAULT_OUTPUT = ROOT / "data" / "research" / "anomaly_report.json"
JST = datetime.timezone(datetime.timedelta(hours=9))


def _model_ranks(marks: list[dict[str, Any]]) -> dict[int, int]:
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


def _audit_row(race: dict[str, Any], meta: dict[str, Any],
               model_ranks: dict[int, int], market_ranks: dict[int, int],
               by_num: dict[int, dict[str, Any]],
               frozen_context: dict[str, Any] | None,
               context_snapshot: dict[str, Any] | None,
               config: dict[str, Any],
               context_snapshot_directory: Path | None,
               audit_manifest_directory: Path | None,
               prediction_snapshot_directory: Path | None,
               odds_history_directory: Path | None,
               finish_max: int) -> dict[str, Any]:
    race_id = race.get("race_id")
    manifest = audit_manifest.latest_manifest(
        race_id or "", audit_manifest_directory
    )

    if manifest:
        evidence = {
            "status": "manifest",
            "manifest_observed_at": manifest.get("observed_at"),
            "proof_scope": manifest.get("proof_scope"),
            "artifacts": manifest.get("artifacts") or {},
            "odds_freshness": manifest.get("odds_freshness") or {},
            "context_completeness": manifest.get("context_completeness") or {},
        }
    else:
        audit_race = {
            "id": race_id,
            "post_time": (
                meta.get("post_time")
                or (context_snapshot or {}).get("post_time")
            ),
        }
        evidence = audit_manifest.evidence_from_files(
            audit_race,
            prediction_directory=prediction_snapshot_directory,
            context_directory=context_snapshot_directory,
            odds_directory=odds_history_directory,
            config=config,
        )
        evidence["proof_scope"] = "derived_legacy_no_manifest"

    finishers = []
    for finish_pos, raw_num in enumerate((race.get("finish") or [])[:finish_max], start=1):
        try:
            num = int(raw_num)
        except (TypeError, ValueError):
            continue
        mark = by_num.get(num) or {}
        finishers.append({
            "finish": finish_pos,
            "num": num,
            "name": mark.get("name"),
            "model_rank": model_ranks.get(num),
            "market_rank": market_ranks.get(num),
            "context": _horse_context(frozen_context, num),
        })

    return {
        "race_id": race_id,
        "venue": meta.get("venue"),
        "race_no": meta.get("race_no"),
        "race_name": meta.get("name"),
        "context_observed_at": (context_snapshot or {}).get("observed_at"),
        "evidence": evidence,
        "finishers": finishers,
        "interpretation": "hypothesis_only",
    }


def build_report(results: dict[str, Any], config: dict[str, Any] | None = None,
                 context_snapshot_directory: Path | None = None,
                 audit_manifest_directory: Path | None = None,
                 prediction_snapshot_directory: Path | None = None,
                 odds_history_directory: Path | None = None) -> dict[str, Any]:
    config = config or context_layers.load_config()
    cfg = config.get("anomaly_review") or {}
    finish_max = int(cfg.get("finish_max", 3))
    model_rank_min = int(cfg.get("model_rank_min", 6))
    market_rank_min = int(cfg.get("market_rank_min", 6))

    anomalies = []
    race_audits = []
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

        race_audits.append(_audit_row(
            race, meta, model_ranks, market_ranks, by_num,
            frozen_context, context_snapshot, config,
            context_snapshot_directory, audit_manifest_directory,
            prediction_snapshot_directory, odds_history_directory,
            finish_max,
        ))

        for finish_pos, raw_num in enumerate((race.get("finish") or [])[:finish_max], start=1):
            try:
                num = int(raw_num)
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

    audit_summary = {
        "races": len(race_audits),
        "with_manifest": 0,
        "legacy_derived": 0,
        "odds_fresh": 0,
        "odds_target_window": 0,
        "odds_stale": 0,
        "body_weight_full_coverage": 0,
        "missing_context_snapshot": 0,
    }
    for row in race_audits:
        evidence = row.get("evidence") or {}
        status = evidence.get("status")
        if status == "manifest":
            audit_summary["with_manifest"] += 1
        elif status == "derived_from_pre_race_artifacts":
            audit_summary["legacy_derived"] += 1

        freshness = evidence.get("odds_freshness") or {}
        if freshness.get("fresh") is True:
            audit_summary["odds_fresh"] += 1
        if freshness.get("target_window") is True:
            audit_summary["odds_target_window"] += 1
        if freshness.get("status") == "stale":
            audit_summary["odds_stale"] += 1

        completeness = evidence.get("context_completeness") or {}
        if completeness.get("body_weight_current_coverage") == 1.0:
            audit_summary["body_weight_full_coverage"] += 1
        if completeness.get("context_snapshot_present") is False:
            audit_summary["missing_context_snapshot"] += 1

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
            "audit": audit_summary,
        },
        "race_audits": race_audits,
        "anomalies": anomalies,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="想定外好走馬とContext監査証跡を生成")
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
        f"anomalies={report['summary']['anomalies']} "
        f"manifests={report['summary']['audit']['with_manifest']}"
    )


if __name__ == "__main__":
    main()
