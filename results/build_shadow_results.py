from __future__ import annotations

import argparse
import datetime
import json
import logging
from pathlib import Path
from typing import Any

from logic import model_registry, snapshots
from results import build_results, model_compare

logger = logging.getLogger("results.build_shadow_results")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CHALLENGER_ROOT = DATA_DIR / "challengers"
DEFAULT_RACE_RESULTS = DATA_DIR / "race_results.json"
DEFAULT_CHAMPION_RESULTS = DATA_DIR / "results.json"
COMPARISON_PATH = DATA_DIR / "model_comparison.json"
CHAMPION_SNAPSHOT_DIR = snapshots.SNAPSHOT_DIR


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _after_registration(predictions: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """
    registered_at を持つ Challenger は、登録より後に発走したレースだけを採点する。

    登録前のレースを後から作り直して混ぜると forward 検証ではなくなるため（凍結表の cutoff より前のレースは
    表そのものに勝ちタイムが入っている）。発走時刻が判らないレースも数えない。
    """
    registered_at = spec.get("registered_at")
    if not registered_at:
        return predictions
    registered = datetime.datetime.fromisoformat(registered_at)
    kept, dropped = [], []
    for race in predictions.get("races", []):
        post_at = snapshots.post_datetime(race)
        (kept if post_at is not None and post_at > registered else dropped).append(race)
    if dropped:
        logger.warning("%s: 登録（%s）より前のレース %d 件は採点しません（%s）", spec["id"], registered_at,
                       len(dropped), ", ".join(str(r.get("id")) for r in dropped[:5]))
    return {**predictions, "races": kept}


def _snapshots_by_race(directory: Path) -> dict[str, dict[str, Any]]:
    return {s.get("race_id"): s for s in snapshots.load_all(directory) if s.get("pre_race")}


def build_all(race_results: dict[str, Any],
              champion_results: dict[str, Any],
              registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or model_registry.load_registry()
    challenger_results: dict[str, dict[str, Any]] = {}

    for spec in model_registry.enabled_challengers(registry):
        model_id = spec["id"]
        root = CHALLENGER_ROOT / model_id
        predictions = _after_registration(snapshots.as_predictions(root / "snapshots"), spec)
        result = build_results.build_results(predictions, race_results)
        result["model"] = {
            "model_id": model_id,
            "model_role": spec.get("role", "challenger"),
        }
        _write(root / "results.json", result)
        challenger_results[model_id] = result
        logger.info("%s: %d races settled", model_id, len(result.get("results", [])))

    comparison = model_compare.compare(champion_results, challenger_results)
    champion_snaps = _snapshots_by_race(CHAMPION_SNAPSHOT_DIR)
    for row in comparison["comparisons"]:
        # speed が実際に使われたかを、同じ共通レースで Champion と並べる（採点とは別の観察値）
        challenger_snaps = _snapshots_by_race(CHALLENGER_ROOT / row["challenger_model_id"] / "snapshots")
        row["speed_quality"] = {
            "champion": model_compare.speed_quality_summary(
                [champion_snaps.get(rid) for rid in row["common_race_ids"]]),
            "challenger": model_compare.speed_quality_summary(
                [challenger_snaps.get(rid) for rid in row["common_race_ids"]]),
        }
    for row in comparison["comparisons"]:
        cov = row["coverage"]
        if cov["missing_in_challenger"]:
            # Challengerのビルドが失敗したレースは比較から消える。黙って消すと
            # 「勝てたレースだけ残った」状態に気づけないので必ず警告する。
            logger.warning(
                "%s: Championにあって Challenger に無いレースが %d 件あります（%s）。"
                "比較は共通 %d レースのみです",
                row["challenger_model_id"], len(cov["missing_in_challenger"]),
                ", ".join(cov["missing_in_challenger"][:5]), cov["common_races"],
            )
    _write(COMPARISON_PATH, comparison)
    return comparison


def main() -> None:
    parser = argparse.ArgumentParser(description="Champion/Challenger shadow results builder")
    parser.add_argument("--results", default=str(DEFAULT_RACE_RESULTS))
    parser.add_argument("--champion-results", default=str(DEFAULT_CHAMPION_RESULTS))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    with open(args.results, encoding="utf-8") as f:
        race_results = json.load(f)
    with open(args.champion_results, encoding="utf-8") as f:
        champion_results = json.load(f)

    comparison = build_all(race_results, champion_results)
    logger.info("comparison written: %s (%d challengers)",
                COMPARISON_PATH, len(comparison["comparisons"]))


if __name__ == "__main__":
    main()
