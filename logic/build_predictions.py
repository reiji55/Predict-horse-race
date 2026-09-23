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

from logic import base_score, cards, chappy, context_layers, model_registry, myomi, prob_model, race_regime as regime_mod, snapshots
from logic import aptitude as aptitude_mod
from logic import human_score as human_mod
from logic import speed_index as speed_mod
from logic import top3_score as top3_mod

logger = logging.getLogger("logic.build_predictions")

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
OUTPUT_PATH = ROOT / "data" / "predictions.json"
CHALLENGER_ROOT = ROOT / "data" / "challengers"

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
    with (CONFIG_DIR / "chappy.json").open(encoding="utf-8") as f:
        chappy_config = json.load(f)
    with (CONFIG_DIR / "race_regime.json").open(encoding="utf-8") as f:
        race_regime_config = json.load(f)
    return {
        "cards": cards_config,
        "myomi": myomi_config,
        "speed": speed_config,
        "chappy": chappy_config,
        "race_regime": race_regime_config,
    }


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
               overall_jockey_rate: float, overall_trainer_rate: float,
               model_spec: dict[str, Any] | None = None) -> dict:
    """raw の races[] 1件から、指定モデルの predictions races[] 1件を組み立てる。"""
    cards_config = configs["cards"]
    myomi_config = configs["myomi"]
    speed_config = configs["speed"]
    chappy_config = configs["chappy"]
    race_regime_config = configs["race_regime"]
    model_spec = model_spec or model_registry.load_registry()["champion"]
    runtime = model_registry.runtime_metadata(model_spec)
    use_top3_partner = bool(model_spec.get("use_top3_partner", False))
    use_race_regime = bool(model_spec.get("use_race_regime", False))

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
        speed = speed_mod.compute_horse_speed(
            past_runs, speed_config, base_times,
            target_surface=today_course.get("surface"),
        )
        # ChappyはChampion/ChallengerどちらでもTop3シグナルを監査用に見る。
        # 固定3キャラの買い目へ使うかどうかだけ use_top3_partner で分ける。
        top3 = top3_mod.compute_top3_profile(
            past_runs, today_course, cards_config["top3"]
        )

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
            "top3_raw": top3["raw"] if top3 else None,
            "top3_n_usable": top3["n_usable"] if top3 else 0,
            "top3_same_dist_runs": top3["same_dist_runs"] if top3 else 0,
            "top3_same_dist_hits": top3["same_dist_top3"] if top3 else 0,
            "_past_runs": past_runs,
            "n_usable": speed["n_usable"] if speed else 0,
            # スピード指数仕様§3：n_usable ≤ 2 は値は使うが低信頼
            "uncertain": speed is None or speed["n_usable"] <= 2,
        })

    # --- スピード指数のレース内品質ガード -----------------------------
    # base_times が疎な段階で「一部の馬だけ悪い走が指数化される」非対称を防ぐ。
    # coverage不足なら①をレース全体で切り、coverageを満たす場合の欠損は中立(z=0)補完する。
    speed_quality = speed_mod.apply_race_speed_guard(horses, speed_config)
    if not speed_quality["used"]:
        logger.warning(
            "%s: スピード指数をレース全体で無効化しました "
            "(qualified=%s/%s coverage=%.3f < %.3f)",
            race.get("id"),
            speed_quality["qualified_horses"], speed_quality["total_horses"],
            speed_quality["coverage"], speed_quality["min_race_coverage"],
        )

    # --- Win Score と Top3 Score を分離 -------------------------------
    # ①②③のbase_scoreは「勝ち切る力」のまま。Top3はワイド/3連複の相手候補専用で、
    # win probability p や妙味EVには混ぜない。
    base_score.compute_base_scores(horses, cards_config)
    # 順位付けは Champion/Challenger 両方で行う（marks に top3_rank を残して後から比較するため）。
    # 実際に買い目の相手へ使うかどうかだけを use_top3_partner で分ける。
    cards.assign_place_partner_ranks(horses)
    marks = cards.assign_marks(horses, cards_config)

    # --- p / q → 妙味 -------------------------------------------------
    scores = [h["score"] for h in horses]
    win_odds = [h["odds"] for h in horses]
    p = prob_model.softmax_scores(scores, myomi_config["prob_model"]["temperature"])
    q = prob_model.market_support(win_odds)

    # 発走前の市場状態。Championでも診断値として記録するが、買い目へ使うのは
    # use_race_regime=true のChallengerだけ。結果を見て後付け分類しないためsnapshotへ残す。
    race_regime = regime_mod.classify(p, q, race_regime_config)

    # 旧妙味（単勝pと市場支持率の乖離）は診断値として残す。
    # 鳳の降臨判定には使わない。初実戦で「高EVを作った馬を鳳が買っていない」矛盾が起きたため。
    disagreement_myomi = myomi.compute_myomi(
        p, q, [h["n_usable"] for h in horses], win_odds, myomi_config
    )

    # --- value / myomi_rank → キャラ別カード --------------------------
    for horse, p_i, q_i, info in zip(horses, p, q, cards.compute_value_and_myomi_rank(p, q)):
        horse["p"] = p_i
        horse["q"] = q_i
        horse["value"] = info["value"]
        horse["myomi_rank"] = info["myomi_rank"]

    temperature = myomi_config["prob_model"]["temperature"]
    combo_odds = race.get("combo_odds") or {}

    # 表示する妙味は従来の市場乖離メーターを維持する。
    # 鳳はここではまだ決めない。Chappy統合判断の high-conviction gate が唯一の降臨条件。
    myomi_result = {
        "myomi": disagreement_myomi["myomi"],
        "myomi_parts": disagreement_myomi["myomi_parts"],
        "legendary": False,
        "myomi_source": myomi.SOURCE_DISAGREEMENT_WITH_EV_VETO,
    }

    generated_cards = []

    for char_id in BASE_CHARACTERS:
        if use_race_regime and regime_mod.should_abstain(
                char_id, race_regime, race_regime_config):
            card = cards.generate_abstain_card(
                char_id, cards_config,
                model_id=runtime["model_id"], model_role=runtime["model_role"],
                reason=f"race_regime:{race_regime.get('label')}",
                say=regime_mod.abstain_comment(char_id, race_regime_config),
                race_regime=race_regime,
            )
        else:
            card = cards.generate_card_for_character(
                char_id, horses, cards_config,
                temperature=temperature, combo_odds=combo_odds,
                use_place_model=use_top3_partner,
                model_id=runtime["model_id"], model_role=runtime["model_role"],
            )
        if card is None:
            continue
        cards.validate_card_invariants(card, marks, cards_config.get("amt_unit", 100))
        generated_cards.append(card)

    # --- Chappy 1000円統合判断 ---------------------------------------
    # 固定3キャラとは別レイヤー。Win/Top3/条件/近況/市場を動的に統合し、
    # ChatGPT会話で作った手動カードがあればそれを最優先する。
    chappy_card = None
    chappy_market_ev = None
    chappy_decision: dict[str, Any] = {"status": "insufficient_horses"}
    legendary = False
    if len(horses) >= 4:
        # 手動カードは「発走前に作られたと検証できたもの」だけ採用する。
        # 発走時刻を渡さないと検証できないので、ここで必ず渡す。
        manual_override = chappy.load_manual_override(
            race.get("id"), chappy_config,
            post_at=snapshots.post_datetime({"id": race.get("id"),
                                             "post_time": race.get("post_time")}),
        )
        chappy_card, chappy_decision = chappy.generate_card(
            horses, race, chappy_config,
            myomi_value=disagreement_myomi["myomi"],
            speed_quality=speed_quality,
            combo_odds=combo_odds,
            model_id=runtime["model_id"], model_role=runtime["model_role"],
            manual_override=manual_override,
            takeout=cards_config["combo_prob"]["takeout"],
            race_regime=race_regime if use_race_regime else None,
            solid_fourth_role=(
                race_regime_config["policies"]["chappy_auto"].get("solid_fourth_role")
                if use_race_regime else None
            ),
        )
        cards.validate_card_invariants(
            chappy_card, marks, cards_config.get("amt_unit", 100)
        )

        # 鳳はChappyの上位互換state。別カードを追加せず、Chappyカードそのものが鳳へ変わる。
        legendary = chappy_card["char"] == LEGENDARY_CHARACTER
        if legendary:
            generated_cards.insert(0, chappy_card)
        else:
            generated_cards.append(chappy_card)
        chappy_market_ev = chappy_card.get("market_ev")

    card_ev_myomi = myomi.compute_card_ev_myomi(
        chappy_market_ev, [h["n_usable"] for h in horses], myomi_config
    )

    # 新しい当日情報はまず観測だけ。base_score / p / 妙味 / 買い目にはまだ混ぜない。
    context = context_layers.build_context(race)

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
        "model_id": runtime["model_id"],
        "model_role": runtime["model_role"],
        "git_commit": runtime["git_commit"],
        "config_hash": runtime["config_hash"],
        "base_times_hash": runtime["base_times_hash"],
        "speed_quality": speed_quality,
        "context_layers": context,
        "race_regime": race_regime,
        "race_regime_policy_active": use_race_regime,
        "myomi": myomi_result["myomi"],
        "myomi_parts": myomi_result["myomi_parts"],
        "myomi_source": myomi_result.get("myomi_source"),
        "legendary": legendary,
        "chappy_decision": chappy_decision,
        "otori_card_ev": chappy_market_ev if legendary else None,
        "card_ev_myomi": card_ev_myomi,          # 実オッズで測れた場合の妙味（測れなければ0）
        "model_disagreement_myomi": disagreement_myomi,   # 旧メーター（診断・退避用）
        "marks": marks,
        "cards": generated_cards,
    }


def build_predictions(raw: dict, configs: dict | None = None,
                      base_times: dict | None = None,
                      model_spec: dict[str, Any] | None = None,
                      generated_at: str | None = None) -> dict:
    """raw/{week_id}.json から、指定したChampion/Challengerの予想を組み立てる。"""
    configs = configs if configs is not None else load_configs()
    base_times = base_times if base_times is not None else speed_mod.load_base_times()
    speed_mod.warn_if_base_times_empty(base_times)
    model_spec = model_spec or model_registry.load_registry()["champion"]
    runtime = model_registry.runtime_metadata(model_spec)

    overall_jockey_rate, overall_trainer_rate = _overall_rates(raw)
    logger.info("全体平均複勝率（縮小推定のα）: 騎手 %.3f / 厩舎 %.3f",
                overall_jockey_rate, overall_trainer_rate)

    races = []
    for race in raw.get("races", []):
        try:
            races.append(build_race(
                race, configs, base_times,
                overall_jockey_rate, overall_trainer_rate,
                model_spec=model_spec,
            ))
        except Exception:
            # 1レースの失敗で週全体を落とさない（取得項目仕様§1.1 マナー設計と同じ思想）
            logger.exception("レースの予想生成に失敗しました: %s", race.get("id"))

    return {
        "generated_at": generated_at or datetime.datetime.now(JST).isoformat(timespec="seconds"),
        "week_id": raw.get("week_id"),
        "model": runtime,
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

    registry = model_registry.load_registry()
    configs = load_configs()
    base_times = speed_mod.load_base_times()
    run_now = datetime.datetime.now(JST)
    generated_at = run_now.isoformat(timespec="seconds")

    # Champion: UIと通常成績に使う唯一の本番予想。
    champion = build_predictions(
        raw, configs=configs, base_times=base_times,
        model_spec=registry["champion"], generated_at=generated_at,
    )
    report = snapshots.freeze(champion, now=run_now)
    for item in report:
        logger.info("Champion snapshot %s: %s", item["action"], item["race_id"])
    restored = snapshots.restore_finished_races(champion, now=run_now)
    if restored:
        logger.info("Champion: 発走済み %d レースを凍結予想へ復元", restored)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(champion, f, ensure_ascii=False, indent=2)
        f.write("\n")
    logger.info("Champion書き出し: %s (%s)", OUTPUT_PATH, champion["model"]["model_id"])

    # Challenger: UIには出さない。同じraw・同じrun_nowで発走前に凍結し、後で同じ結果で採点する。
    for challenger_spec in model_registry.enabled_challengers(registry):
        challenger = build_predictions(
            raw, configs=configs, base_times=base_times,
            model_spec=challenger_spec, generated_at=generated_at,
        )
        model_id = challenger_spec["id"]
        root = CHALLENGER_ROOT / model_id
        snapshot_dir = root / "snapshots"
        output_path = root / "predictions.json"

        report = snapshots.freeze(challenger, now=run_now, directory=snapshot_dir)
        for item in report:
            logger.info("Challenger %s snapshot %s: %s",
                        model_id, item["action"], item["race_id"])
        snapshots.restore_finished_races(challenger, now=run_now, directory=snapshot_dir)

        root.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(challenger, f, ensure_ascii=False, indent=2)
            f.write("\n")
        logger.info("Challenger書き出し: %s", output_path)


if __name__ == "__main__":
    main()
