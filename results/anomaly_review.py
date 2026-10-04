"""「想定外に好走した馬」を自動で研究キューへ送り、レースごとの発走前証跡を監査する。

これは自動学習・自動チューニングではない。
発走前にモデル/市場が低評価だったのに上位へ来た馬を抽出し、
発走前に凍結済みのcontext（馬体重・休養・馬場・オッズ推移・パドック）を
同じ場所に束ねて「何を見落とした可能性があるか」を人間/AIが後から検証しやすくする。

加えて race_audits に、レースごとの発走前証跡（予想 snapshot・context・オッズ観測の有無と hash、
券種別の価格時刻と鮮度、馬体重・speed の coverage）を残す。発走前の manifest
（logic/audit_manifest.py）があればそれを、無ければ発走前のファイルだけから組み直したものを使う。
結果を使って予想を書き換えることはしない。

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


def _race_audit(race: dict[str, Any], meta: dict[str, Any],
                audit_config: dict[str, Any],
                audit_manifest_directory: Path | None,
                prediction_snapshot_directory: Path | None,
                context_snapshot_directory: Path | None,
                odds_history_directory: Path | None) -> dict[str, Any]:
    """1レースの発走前証跡。manifest を優先し、無ければ発走前ファイルから組み直す。"""
    race_id = race.get("race_id") or ""
    manifest = audit_manifest.latest_manifest(race_id, audit_manifest_directory)
    if manifest:
        evidence = dict(manifest)
        evidence["evidence_source"] = "pre_race_manifest"
    else:
        evidence = audit_manifest.evidence_from_files(
            {"id": race_id, "post_time": meta.get("post_time")},
            prediction_directory=prediction_snapshot_directory,
            context_directory=context_snapshot_directory,
            odds_directory=odds_history_directory,
            config=audit_config,
        )
        evidence["evidence_source"] = "derived_without_manifest"

    artifacts = evidence.get("artifacts") or {}
    basis = evidence.get("prediction_price_basis") or {}
    cards = evidence.get("card_price_evidence") or {}
    completeness = evidence.get("context_completeness") or {}
    return {
        "race_id": race_id,
        "venue": meta.get("venue"),
        "race_no": meta.get("race_no"),
        "race_name": meta.get("name"),
        "post_time": meta.get("post_time"),
        "evidence_source": evidence["evidence_source"],
        "manifest_observed_at": (manifest or {}).get("observed_at"),
        "present": {
            "prediction_snapshot": completeness.get("prediction_snapshot_present", False),
            "context_snapshot": completeness.get("context_snapshot_present", False),
            "odds_observation": completeness.get("odds_snapshot_present", False),
        },
        "hashes": {
            "prediction": (artifacts.get("prediction") or {}).get("sha256"),
            "context": (artifacts.get("context") or {}).get("sha256"),
            "odds_latest": (artifacts.get("odds") or {}).get("sha256"),
            "odds_prediction_basis": (basis.get("odds") or {}).get("sha256"),
        },
        "times": {
            "prediction_frozen_at": (artifacts.get("prediction") or {}).get("frozen_at"),
            "context_observed_at": (artifacts.get("context") or {}).get("observed_at"),
            "odds_latest_observed_at": (artifacts.get("odds") or {}).get("observed_at"),
            "odds_latest_source_time_win": (artifacts.get("odds") or {}).get("source_time"),
        },
        "odds_freshness_latest_win": evidence.get("odds_freshness"),
        "prediction_price_basis": {
            "status": basis.get("status"),
            "prediction_odds_match": basis.get("prediction_odds_match"),
            "observed_at": (basis.get("odds") or {}).get("observed_at"),
            "market_scope": (basis.get("odds") or {}).get("market_scope"),
        },
        "price_times_by_market": cards.get("markets"),
        "card_price_evidence": cards.get("cards"),
        "otori_price_evidence": cards.get("otori"),
        "body_weight_current_coverage": completeness.get("body_weight_current_coverage"),
        "speed": evidence.get("speed"),
        "interpretation": "audit_only",
    }


def _audit_summary(race_audits: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "races": len(race_audits),
        "with_manifest": 0,
        "derived_without_manifest": 0,
        "missing_prediction_snapshot": 0,
        "missing_context_snapshot": 0,
        "missing_odds_observation": 0,
        "latest_win_freshness": {},
        "basis_by_market_freshness": {},
        "body_weight_full_coverage": 0,
    }
    for row in race_audits:
        if row["evidence_source"] == "pre_race_manifest":
            summary["with_manifest"] += 1
        else:
            summary["derived_without_manifest"] += 1
        present = row["present"]
        summary["missing_prediction_snapshot"] += 0 if present["prediction_snapshot"] else 1
        summary["missing_context_snapshot"] += 0 if present["context_snapshot"] else 1
        summary["missing_odds_observation"] += 0 if present["odds_observation"] else 1
        status = (row.get("odds_freshness_latest_win") or {}).get("status") or "unknown"
        summary["latest_win_freshness"][status] = summary["latest_win_freshness"].get(status, 0) + 1
        for market, info in (row.get("price_times_by_market") or {}).items():
            bucket = summary["basis_by_market_freshness"].setdefault(market, {})
            key = (info or {}).get("freshness") or "unknown"
            bucket[key] = bucket.get(key, 0) + 1
        if row.get("body_weight_current_coverage") == 1.0:
            summary["body_weight_full_coverage"] += 1
    return summary


def build_report(results: dict[str, Any], config: dict[str, Any] | None = None,
                 context_snapshot_directory: Path | None = None,
                 audit_manifest_directory: Path | None = None,
                 prediction_snapshot_directory: Path | None = None,
                 odds_history_directory: Path | None = None,
                 audit_config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or context_layers.load_config()
    cfg = config.get("anomaly_review") or {}
    finish_max = int(cfg.get("finish_max", 3))
    model_rank_min = int(cfg.get("model_rank_min", 6))
    market_rank_min = int(cfg.get("market_rank_min", 6))

    audit_config = audit_config or audit_manifest.load_config()

    anomalies = []
    race_audits = []
    races_scanned = 0
    for race in results.get("results") or []:
        if race.get("evaluation_scope") == "manual_chat":
            continue
        races_scanned += 1
        race_id = race.get("race_id")
        meta = race.get("meta") or {}
        race_audits.append(_race_audit(
            race, meta, audit_config, audit_manifest_directory,
            prediction_snapshot_directory, context_snapshot_directory, odds_history_directory,
        ))
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
            "audit": _audit_summary(race_audits),
        },
        "race_audits": race_audits,
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
        f"anomalies={report['summary']['anomalies']} "
        f"race_audits={report['summary']['audit']['races']}"
    )


if __name__ == "__main__":
    main()
