from __future__ import annotations

import json
from pathlib import Path

from logic import base_score, cards, myomi, prob_model
from scraper.fetchers import b2_odds

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "cards.json").read_text(encoding="utf-8"))
MYOMI_CONFIG = json.loads((ROOT / "config" / "myomi.json").read_text(encoding="utf-8"))


def _horses():
    return [
        {"num": 1, "speed_raw": 100.0, "aptitude_raw": 0.20, "human_raw": 0.30, "odds": 2.5},
        {"num": 2, "speed_raw": 92.0, "aptitude_raw": 0.50, "human_raw": 0.25, "odds": 6.0},
        {"num": 3, "speed_raw": 80.0, "aptitude_raw": 0.95, "human_raw": 0.20, "odds": 18.0},
        {"num": 4, "speed_raw": 76.0, "aptitude_raw": 0.80, "human_raw": 0.10, "odds": 30.0},
    ]


def _prepare():
    horses = _horses()
    base_score.compute_base_scores(horses, CONFIG)
    cards.assign_marks(horses, CONFIG)
    p = prob_model.softmax_scores(
        [h["score"] for h in horses], MYOMI_CONFIG["prob_model"]["temperature"]
    )
    q = prob_model.market_support([h["odds"] for h in horses])
    for h, p_i, q_i in zip(horses, p, q):
        h["p"] = p_i
        h["q"] = q_i
    return horses


def test_every_template_is_purchaseable_in_100_yen_units():
    assert CONFIG["amt_unit"] == 100
    for char_id, templates in cards.CARD_TEMPLATES.items():
        for template in templates:
            amounts = [amt for _, _, amt in template["bets"]]
            assert all(amt >= 100 and amt % 100 == 0 for amt in amounts), (char_id, amounts)
            assert sum(amounts) == CONFIG["characters"][char_id]["total"]


def test_character_weights_recompute_probability_not_only_rank():
    horses = _prepare()
    temperature = MYOMI_CONFIG["prob_model"]["temperature"]

    cards.assign_character_ranks(horses, CONFIG, CONFIG["characters"]["kei"], temperature)
    kei_p = [h["sel_p"] for h in horses]
    kei_top = min(horses, key=lambda h: h["sel_base_rank"])["num"]

    cards.assign_character_ranks(horses, CONFIG, CONFIG["characters"]["gen"], temperature)
    gen_p = [h["sel_p"] for h in horses]
    gen_top = min(horses, key=lambda h: h["sel_base_rank"])["num"]

    assert kei_p != gen_p
    assert kei_top == 1
    assert gen_top == 3
    assert abs(sum(kei_p) - 1.0) < 1e-9
    assert abs(sum(gen_p) - 1.0) < 1e-9


def test_combo_odds_parser_normalizes_market_prices():
    body = {
        "odds": {
            "4": {"0209": ["269.9", 12]},
            "5": {"0209": ["45.2", "66.9", 8]},
            "7": {"020910": ["2252.8", 40]},
        }
    }
    parsed = b2_odds.extract_combo_odds(body)
    assert parsed["馬連"]["2-9"] == 269.9
    assert parsed["ワイド"]["2-9"] == [45.2, 66.9]
    assert parsed["3連複"]["2-9-10"] == 2252.8


def test_market_card_ev_uses_actual_combo_odds_and_requires_full_coverage():
    p = {1: 0.45, 2: 0.30, 3: 0.20, 4: 0.05}
    bets = [
        {"type": "ワイド", "horses": [1, 2], "amt": 200},
        {"type": "馬連", "horses": [1, 2], "amt": 100},
        {"type": "3連複", "horses": [1, 2, 3], "amt": 200},
    ]
    combo = {
        "ワイド": {"1-2": [3.0, 4.0]},
        "馬連": {"1-2": 12.0},
        "3連複": {"1-2-3": 30.0},
    }
    ev = cards.evaluate_market_card(bets, p, combo)
    assert ev["complete"] is True
    assert ev["coverage"] == 1.0
    assert ev["expected_roi"] is not None
    assert ev["bets"][0]["market_odds"] == 3.0  # ワイドは保守的に下限

    incomplete = cards.evaluate_market_card(
        bets, p, {"ワイド": {"1-2": [3.0, 4.0]}, "馬連": {"1-2": 12.0}}
    )
    assert incomplete["complete"] is False
    assert incomplete["expected_roi"] is None


def test_otori_myomi_is_driven_by_the_card_it_would_actually_buy():
    rich = myomi.compute_card_ev_myomi(
        {"complete": True, "expected_roi": 1.50},
        [3, 3, 3, 3],
        MYOMI_CONFIG,
    )
    assert rich["myomi"] == 100.0
    assert rich["legendary"] is True
    assert rich["myomi_source"] == "otori_market_card_ev"

    missing = myomi.compute_card_ev_myomi(
        {"complete": False, "expected_roi": None},
        [3, 3, 3, 3],
        MYOMI_CONFIG,
    )
    assert missing["myomi"] == 0.0
    assert missing["legendary"] is False
    assert "unavailable" in missing["myomi_source"]


def test_generated_character_card_records_its_own_probability_model():
    horses = _prepare()
    temperature = MYOMI_CONFIG["prob_model"]["temperature"]

    # 十分な組み合わせを与え、カードのmarket_evがcompleteになるようにする。
    combo = {"ワイド": {}, "馬連": {}, "3連複": {}}
    nums = [1, 2, 3, 4]
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            key = f"{nums[i]}-{nums[j]}"
            combo["ワイド"][key] = [10.0, 12.0]
            combo["馬連"][key] = 20.0
    for i in range(len(nums)):
        for j in range(i + 1, len(nums)):
            for k in range(j + 1, len(nums)):
                key = f"{nums[i]}-{nums[j]}-{nums[k]}"
                combo["3連複"][key] = 50.0

    card = cards.generate_card_for_character(
        "gen", horses, CONFIG, temperature=temperature, combo_odds=combo
    )
    assert card is not None
    assert card["market_ev"]["complete"] is True
    assert card["probability_model"]["score_weights"] == CONFIG["characters"]["gen"]["score_weights"]
