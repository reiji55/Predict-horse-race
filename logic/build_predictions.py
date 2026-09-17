"""
predictions.json ビルドスクリプト（データスキーマ仕様_v1_2.md §1 が出力契約）

流れ（引き継ぎ書v3 §4）：
  raw/{week_id}.json
  → ①speed_index ②aptitude ③human_score を各馬に計算
  → base_score（①②③のz標準化合成。④は入れない）
  → 印付与（cards.assign_marks）
  → p = softmax(base_score/T)、q = market_support(win_odds)
  → myomi（レース単位）
  → value=p-q・myomi_rank → キャラ別カード生成（cards.generate_card_for_character）
  → predictions.json 書き出し（不変条件検証込み）

GitHub Actions から `python -m logic.build_predictions --week 2026-W27` のように呼ばれる想定。

TODO：logic/ 配下の各モジュール実装が揃い次第、この組み立てロジックを実装
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from logic import base_score, cards, myomi, prob_model
from logic import aptitude as aptitude_mod
from logic import human_score as human_mod
from logic import speed_index as speed_mod

logger = logging.getLogger("logic.build_predictions")

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.json"

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_configs() -> dict:
    with (CONFIG_DIR / "cards.json").open(encoding="utf-8") as f:
        cards_config = json.load(f)
    with (CONFIG_DIR / "myomi.json").open(encoding="utf-8") as f:
        myomi_config = json.load(f)
    with (CONFIG_DIR / "speed_index.json").open(encoding="utf-8") as f:
        speed_config = json.load(f)
    return {"cards": cards_config, "myomi": myomi_config, "speed": speed_config}


def build_predictions(raw: dict) -> dict:
    """raw/{week_id}.json の内容から predictions.json のトップレベル構造を組み立てる。"""
    raise NotImplementedError("logic/ 配下の各モジュール実装後にここで組み立てる")


def main() -> None:
    parser = argparse.ArgumentParser(description="週末3CARDS predictions.json ビルド")
    parser.add_argument("--week", required=True, help='例: "2026-W27"')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    raw_path = RAW_DIR / f"{args.week}.json"
    with raw_path.open(encoding="utf-8") as f:
        raw = json.load(f)

    predictions = build_predictions(raw)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(predictions, f, ensure_ascii=False, indent=2)
    logger.info("書き出し完了: %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
