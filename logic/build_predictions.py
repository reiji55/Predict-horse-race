"""
predictions.json ビルドスクリプト（データスキーマ仕様_v1.2.md §1 が出力契約）

流れ（引き継ぎ書v3 §4）：
  raw/{week_id}.json
  → ①speed_index ②aptitude ③human_score を各馬に計算
  → base_score（①②③のz標準化合成。④は入れない）
  → 印付与（cards.assign_marks）
  → p = softmax(score/T)、q = market_support(win_odds)
  → myomi（レース単位）
  → value=p−q・myomi_rank → キャラ別カード生成（cards.generate_card_for_character）
  → predictions.json 書き出し（不変条件検証込み）

GitHub Actions から `python -m logic.build_predictions --week 2026-W27` のように呼ばれる想定。

--- 縮小推定の全体平均率について ---

買い目生成仕様§1.5 の α（全体平均複勝率）は、本来リーディング表の全騎手・全厩舎の合計から出す。
D・Eフェッチャーが未実装の現状は、**raw に載っている範囲の着度数を合算して推定**する
（`human_score.league_place_rate`）。D・Eが入れば同じ関数にリーディング表を渡すだけで精度が上がる。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
from pathlib import Path
from typing import Any

from logic import base_score, cards, myomi, prob_model, snapshots
from logic import aptitude as aptitude_mod
from logic import human_score as human_mod
from logic import speed_index as speed_mod

logger = logging.getLogger("logic.build_predictions")

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.json"

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

JST = datetime.timezone(datetime.timedelta(hours=9))

# 鳳は降臨レースのみ。カードの並びは predictions.sample.json に合わせ、降臨時は鳳を先頭に置く
BASE_CHARACTERS = ("kei", "tetsu", "gen")
LEGENDARY_CHARACTER = "otori"


def load_configs() -> dict:
    with (CONFIG_DIR / "cards.json").open(encoding="utf-8") as f:
        cards_config = json.load(f)
    with (CONFIG_DIR / "myomi.json").open(encoding="utf-8") as f:
        myomi_config = json.load(f)
    with (CONFIG_DIR / "speed_index.json").open(encoding="utf-8") as f:
        speed_config = json.load(f)
    return {"cards": cards_config, "myomi": myomi_config, "speed": speed_config}


def _overall_rates(raw: dict) -> tuple[float, float]:
    """raw 全体の着度数から、騎手・厩舎それぞれの全体平均複勝率（縮小推定のα）を推定する。"""
    jockey_stats = []
    trainer_stats = []
    for race in raw.get("races", []):
        for entry in race.get("entries", []):
            jockey_stats.append(entry.get("jockey_stats"))
            trainer_stats.append(entry.get("trainer_stats"))
    return (
        human_mod.league_place_rate(jockey_stats),
        human_mod.league_place_rate(trainer_stats),
    )


def build_race(race: dict, configs: dict, base_times: dict,
               overall_jockey_rate: float, overall_trainer_rate: float) -> dict:
    """raw の races[] 1件から、predictions.json の races[] 1件を組み立てる。"""
    cards_config = configs["cards"]
    myomi_config = configs["myomi"]
    speed_config = configs["speed"]

    course = race.get("course") or {}
    today_course = {
        "surface": course.get("surface"),
        "dist": course.get("dist"),
        "venue": race.get("venue"),
        "going": race.get("going"),
    }

    # --- ①②③ を各馬に計算 -------------------------------------------
    horses: list[dict[str, Any]] = []
    for entry in race.get("entries", []):
        past_runs = entry.get("past_runs") or []
        speed = speed_mod.compute_horse_speed(past_runs, speed_config, base_times)

        horses.append({
            "num": entry.get("num"),
            "waku": entry.get("waku"),
            "name": entry.get("name"),
            "odds": entry.get("win_odds"),
            "speed_raw": speed_mod.speed_raw(speed, speed_config),
            "aptitude_raw": aptitude_mod.compute_aptitude(
                past_runs, today_course, cards_config["aptitude"]
            ),
            "human_raw": human_mod.compute_human_score(
                entry.get("jockey_stats"), entry.get("trainer_stats"),
                overall_jockey_rate, overall_trainer_rate, cards_config["human"],
            ),
            "n_usable": speed["n_usable"] if speed else 0,
            # スピード指数仕様§3：n_usable ≤ 2 は値は使うが低信頼
            "uncertain": speed is None or speed["n_usable"] <= 2,
        })

    # --- 合成スコア → 印 ---------------------------------------------
    base_score.compute_base_scores(horses, cards_config)
    marks = cards.assign_marks(horses, cards_config)

    # --- p / q → 妙味 -------------------------------------------------
    scores = [h["score"] for h in horses]
    win_odds = [h["odds"] for h in horses]
    p = prob_model.softmax_scores(scores, myomi_config["prob_model"]["temperature"])
    q = prob_model.market_support(win_odds)

    myomi_result = myomi.compute_myomi(
        p, q, [h["n_usable"] for h in horses], win_odds, myomi_config
    )

    # --- value / myomi_rank → キャラ別カード --------------------------
    for horse, p_i, q_i, info in zip(horses, p, q, cards.compute_value_and_myomi_rank(p, q)):
        horse["p"] = p_i
        horse["q"] = q_i
        horse["value"] = info["value"]
        horse["myomi_rank"] = info["myomi_rank"]

    char_ids = list(BASE_CHARACTERS)
    if myomi_result["legendary"]:
        char_ids.insert(0, LEGENDARY_CHARACTER)  # 降臨レースは鳳を先頭に

    generated_cards = []
    for char_id in char_ids:
        card = cards.generate_card_for_character(char_id, horses, cards_config)
        if card is None:
            continue
        cards.validate_card_invariants(card, marks, cards_config.get("amt_unit", 50))
        generated_cards.append(card)

    return {
        "id": race.get("id"),
        "source_refs": race.get("source_refs", {"netkeiba": None, "jravan": None}),
        "day": race.get("day"),
        "venue": race.get("venue"),
        "race_no": race.get("race_no"),
        "name": race.get("name"),
        "grade": race.get("grade"),
        "post_time": race.get("post_time"),
        "course": course,
        "myomi": myomi_result["myomi"],
        "myomi_parts": myomi_result["myomi_parts"],
        "legendary": myomi_result["legendary"],
        "marks": marks,
        "cards": generated_cards,
    }


def build_predictions(raw: dict, configs: dict | None = None,
                      base_times: dict | None = None) -> dict:
    """raw/{week_id}.json の内容から predictions.json のトップレベル構造を組み立てる。"""
    configs = configs if configs is not None else load_configs()
    base_times = base_times if base_times is not None else speed_mod.load_base_times()
    speed_mod.warn_if_base_times_empty(base_times)

    overall_jockey_rate, overall_trainer_rate = _overall_rates(raw)
    logger.info("全体平均複勝率（縮小推定のα）: 騎手 %.3f / 厩舎 %.3f",
                overall_jockey_rate, overall_trainer_rate)

    races = []
    for race in raw.get("races", []):
        try:
            races.append(build_race(race, configs, base_times,
                                    overall_jockey_rate, overall_trainer_rate))
        except Exception:
            # 1レースの失敗で週全体を落とさない（取得項目仕様§1.1 マナー設計と同じ思想）
            logger.exception("レースの予想生成に失敗しました: %s", race.get("id"))

    return {
        "generated_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
        "week_id": raw.get("week_id"),
        "myomi_threshold": configs["myomi"]["myomi_threshold"],
        "races": races,
    }


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
    logger.info("書き出し完了: %s（%d レース）", OUTPUT_PATH, len(predictions["races"]))

    # 発走前の予想を凍結する（logic/snapshots.py の冒頭を参照）。
    # predictions.json は毎回上書きされるので、採点に使えるのはこちらだけ。
    report = snapshots.freeze(predictions)
    for item in report:
        logger.info("スナップショット %s: %s", item["action"], item["race_id"])


if __name__ == "__main__":
    main()
