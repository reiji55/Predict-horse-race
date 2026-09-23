"""週末3CARDS のデータ品質レポートと最低限の品質ゲート。

目的:
- scraper/build_raw.py は「1レース失敗しても週全体を止めない」思想で動く。
- そのままだと、対象レースが途中で消えたり、過去走/オッズが空でも workflow が success になりうる。
- 本モジュールは raw と predictions を突き合わせ、壊れた出力を本番データとして push しないための
  最低限の品質ゲートを提供する。

critical:
- race list / race 取得に明示的な失敗がある
- raw にあるレースの prediction が無い
- 出走馬ゼロ
- prediction の marks / cards が空
- 過去走または単勝オッズが全頭でゼロ

warning:
- 過去走 / 単勝オッズの一部欠損
- 騎手 / 厩舎成績の欠損
- 式別オッズが3券種すべて揃っていない
- speed guard によりスピード指数がレース全体でOFF

warning は現状のフォールバック設計で安全に処理できるため、既定では workflow を止めない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("logic.data_quality")

JST = datetime.timezone(datetime.timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PREDICTIONS = ROOT / "data" / "predictions.json"
DEFAULT_OUTPUT = ROOT / "data" / "quality_report.json"

REQUIRED_COMBO_TYPES = ("ワイド", "馬連", "3連複")


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total > 0 else 0.0


def _coverage(entries: list[dict[str, Any]], key: str) -> float:
    return _ratio(sum(1 for entry in entries if entry.get(key)), len(entries))


def _odds_coverage(entries: list[dict[str, Any]]) -> float:
    return _ratio(
        sum(1 for entry in entries if entry.get("win_odds") is not None),
        len(entries),
    )


def _combo_type_coverage(race: dict[str, Any]) -> float:
    combo = race.get("combo_odds") or {}
    present = sum(
        1 for bet_type in REQUIRED_COMBO_TYPES
        if isinstance(combo.get(bet_type), dict) and bool(combo.get(bet_type))
    )
    return _ratio(present, len(REQUIRED_COMBO_TYPES))


def _severity(issues: list[dict[str, str]]) -> str:
    levels = {item["severity"] for item in issues}
    if "critical" in levels:
        return "critical"
    if "warning" in levels:
        return "warning"
    return "ok"


def _add(issues: list[dict[str, str]], severity: str, code: str, message: str) -> None:
    issues.append({"severity": severity, "code": code, "message": message})


def build_report(raw: dict[str, Any], predictions: dict[str, Any]) -> dict[str, Any]:
    raw_races = raw.get("races") or []
    pred_races = predictions.get("races") or []
    pred_by_id = {race.get("id"): race for race in pred_races if race.get("id")}

    global_issues: list[dict[str, str]] = []
    collection = raw.get("collection_report") or {}
    failed_collection = collection.get("failed") or []
    selected_count = len(collection.get("selected") or [])
    built_count = len(collection.get("built") or [])

    if failed_collection:
        _add(
            global_issues, "critical", "scraper_collection_failed",
            f"取得段階で {len(failed_collection)} 件の失敗が記録されています",
        )
    if selected_count and built_count < selected_count:
        _add(
            global_issues, "critical", "scraper_race_missing",
            f"対象 {selected_count} レース中 {built_count} レースしか構築できていません",
        )
    if collection and selected_count == 0 and not failed_collection:
        _add(
            global_issues, "warning", "no_targets_selected",
            "この実行では予想対象レースが1件も選ばれていません",
        )

    race_reports: list[dict[str, Any]] = []
    totals = {
        "entries": 0,
        "past_runs": 0,
        "win_odds": 0,
        "jockey_stats": 0,
        "trainer_stats": 0,
    }

    for race in raw_races:
        race_id = race.get("id")
        entries = race.get("entries") or []
        pred = pred_by_id.get(race_id)
        issues: list[dict[str, str]] = []

        entry_count = len(entries)
        past_count = sum(1 for e in entries if e.get("past_runs"))
        odds_count = sum(1 for e in entries if e.get("win_odds") is not None)
        jockey_count = sum(1 for e in entries if e.get("jockey_stats"))
        trainer_count = sum(1 for e in entries if e.get("trainer_stats"))

        totals["entries"] += entry_count
        totals["past_runs"] += past_count
        totals["win_odds"] += odds_count
        totals["jockey_stats"] += jockey_count
        totals["trainer_stats"] += trainer_count

        past_cov = _ratio(past_count, entry_count)
        odds_cov = _ratio(odds_count, entry_count)
        jockey_cov = _ratio(jockey_count, entry_count)
        trainer_cov = _ratio(trainer_count, entry_count)
        combo_cov = _combo_type_coverage(race)

        if entry_count == 0:
            _add(issues, "critical", "no_entries", "出走馬が1頭もありません")
        if entry_count and past_count == 0:
            _add(issues, "critical", "no_past_runs", "過去走が全頭で空です")
        elif past_cov < 0.8:
            _add(
                issues, "warning", "low_past_run_coverage",
                f"過去走カバレッジが {past_cov:.0%} です",
            )
        if entry_count and odds_count == 0:
            _add(issues, "critical", "no_win_odds", "単勝オッズが全頭で空です")
        elif odds_cov < 0.8:
            _add(
                issues, "warning", "low_win_odds_coverage",
                f"単勝オッズカバレッジが {odds_cov:.0%} です",
            )

        if jockey_cov < 0.8:
            _add(
                issues, "warning", "low_jockey_stats_coverage",
                f"騎手成績カバレッジが {jockey_cov:.0%} です",
            )
        if trainer_cov < 0.8:
            _add(
                issues, "warning", "low_trainer_stats_coverage",
                f"厩舎成績カバレッジが {trainer_cov:.0%} です",
            )
        if combo_cov < 1.0:
            _add(
                issues, "warning", "incomplete_combo_odds",
                f"式別オッズの券種カバレッジが {combo_cov:.0%} です",
            )

        prediction_info: dict[str, Any] | None = None
        if pred is None:
            _add(issues, "critical", "missing_prediction", "raw にあるレースの予想が生成されていません")
        else:
            marks = pred.get("marks") or []
            cards = pred.get("cards") or []
            speed_quality = pred.get("speed_quality") or {}
            if not marks:
                _add(issues, "critical", "empty_marks", "予想の marks が空です")
            if not cards:
                _add(issues, "critical", "empty_cards", "予想カードが1枚もありません")
            if speed_quality and not speed_quality.get("used", False):
                _add(
                    issues, "warning", "speed_guard_disabled",
                    "speed guard によりスピード指数がレース全体でOFFです",
                )
            prediction_info = {
                "marks": len(marks),
                "cards": len(cards),
                "speed_used": speed_quality.get("used"),
                "speed_coverage": speed_quality.get("coverage"),
                "model_id": pred.get("model_id"),
            }

        race_reports.append({
            "race_id": race_id,
            "venue": race.get("venue"),
            "race_no": race.get("race_no"),
            "name": race.get("name"),
            "status": _severity(issues),
            "raw": {
                "entries": entry_count,
                "past_runs_coverage": past_cov,
                "win_odds_coverage": odds_cov,
                "jockey_stats_coverage": jockey_cov,
                "trainer_stats_coverage": trainer_cov,
                "combo_type_coverage": combo_cov,
            },
            "prediction": prediction_info,
            "issues": issues,
        })

    raw_ids = {race.get("id") for race in raw_races if race.get("id")}
    extra_predictions = sorted(
        race_id for race_id in pred_by_id
        if race_id not in raw_ids
        and pred_by_id[race_id].get("evaluation_scope") != "manual_chat"
    )
    if extra_predictions:
        _add(
            global_issues, "warning", "prediction_not_in_raw",
            "raw に存在しない通常予想があります: " + ", ".join(extra_predictions[:5]),
        )

    critical_races = sum(1 for row in race_reports if row["status"] == "critical")
    warning_races = sum(1 for row in race_reports if row["status"] == "warning")
    overall_status = _severity(global_issues + [
        issue for row in race_reports for issue in row["issues"]
    ])

    total_entries = totals["entries"]
    return {
        "generated_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
        "week_id": raw.get("week_id"),
        "status": overall_status,
        "summary": {
            "raw_races": len(raw_races),
            "prediction_races": len(pred_races),
            "critical_races": critical_races,
            "warning_races": warning_races,
            "selected_this_run": selected_count,
            "built_this_run": built_count,
            "collection_failures": len(failed_collection),
            "entry_coverage": {
                "past_runs": _ratio(totals["past_runs"], total_entries),
                "win_odds": _ratio(totals["win_odds"], total_entries),
                "jockey_stats": _ratio(totals["jockey_stats"], total_entries),
                "trainer_stats": _ratio(totals["trainer_stats"], total_entries),
            },
        },
        "collection_report": collection,
        "global_issues": global_issues,
        "races": race_reports,
    }


def should_fail(report: dict[str, Any], fail_on: str) -> bool:
    status = report.get("status", "ok")
    if fail_on == "never":
        return False
    if fail_on == "warning":
        return status in {"warning", "critical"}
    return status == "critical"


def main() -> None:
    parser = argparse.ArgumentParser(description="週末3CARDS data quality report")
    parser.add_argument("--raw", required=True, help="raw/{week_id}.json")
    parser.add_argument("--predictions", default=str(DEFAULT_PREDICTIONS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--fail-on",
        choices=("never", "critical", "warning"),
        default="critical",
        help="どの重大度から終了コード1にするか（既定: critical）",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    with open(args.raw, encoding="utf-8") as f:
        raw = json.load(f)
    with open(args.predictions, encoding="utf-8") as f:
        predictions = json.load(f)

    report = build_report(raw, predictions)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")

    summary = report["summary"]
    log = logger.error if report["status"] == "critical" else (
        logger.warning if report["status"] == "warning" else logger.info
    )
    log(
        "data quality=%s raw=%d predictions=%d critical=%d warning=%d "
        "past_runs=%.0f%% odds=%.0f%%",
        report["status"],
        summary["raw_races"],
        summary["prediction_races"],
        summary["critical_races"],
        summary["warning_races"],
        summary["entry_coverage"]["past_runs"] * 100,
        summary["entry_coverage"]["win_odds"] * 100,
    )
    for issue in report["global_issues"]:
        logger.warning("%s: %s", issue["code"], issue["message"])
    for row in report["races"]:
        for issue in row["issues"]:
            level = logger.error if issue["severity"] == "critical" else logger.warning
            level("%s %s: %s", row["race_id"], issue["code"], issue["message"])

    if should_fail(report, args.fail_on):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
