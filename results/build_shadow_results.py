from __future__ import annotations

import argparse
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


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def build_all(race_results: dict[str, Any],
              champion_results: dict[str, Any],
              registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or model_registry.load_registry()
    challenger_results: dict[str, dict[str, Any]] = {}

    for spec in model_registry.enabled_challengers(registry):
        model_id = spec["id"]
        root = CHALLENGER_ROOT / model_id
        predictions = snapshots.as_predictions(root / "snapshots")
        result = build_results.build_results(predictions, race_results)
        result["model"] = {
            "model_id": model_id,
            "model_role": spec.get("role", "challenger"),
        }
        _write(root / "results.json", result)
        challenger_results[model_id] = result
        logger.info("%s: %d races settled", model_id, len(result.get("results", [])))

    comparison = model_compare.compare(champion_results, challenger_results)
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
