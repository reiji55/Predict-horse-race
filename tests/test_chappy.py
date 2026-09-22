from __future__ import annotations

import copy
import json
from pathlib import Path

from logic import chappy

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "chappy.json").read_text(encoding="utf-8"))


def _run(finish, venue="中山", dist=1800, going="良", surface="ダ", heads=16):
    return {
        "finish": finish, "venue": venue, "dist": dist, "going": going,
        "surface": surface, "heads": heads,
    }


def _horse(num, score, top3, odds, value, runs, p):
    return {
        "num": num, "name": f"H{num}", "score": score, "top3_raw": top3,
        "top3_n_usable": len(runs), "odds": odds, "value": value,
        "_past_runs": runs, "p": p,
    }


def _race():
    return {
        "id": "20260922-nakayama-11", "venue": "中山", "going": "不良",
        "course": {"surface": "ダ", "dist": 1800},
    }


def test_condition_profile_rewards_repeat_same_course_distance():
    strong = [
        _run(1, going="不良"), _run(2, going="稍重"),
        _run(1, going="良"), _run(2, going="不良"),
    ]
    weak = [
        _run(1, venue="東京", dist=1600), _run(9, venue="東京", dist=1600),
        _run(7, venue="福島", dist=1700),
    ]

    s = chappy.condition_profile(strong, {"surface":"ダ","dist":1800,"venue":"中山","going":"不良"})
    w = chappy.condition_profile(weak, {"surface":"ダ","dist":1800,"venue":"中山","going":"不良"})

    assert s["same_course_dist_runs"] == 4
    assert s["same_course_dist_top3"] == 4
    assert s["score"] > w["score"]


def test_signal_board_dynamically_boosts_exceptional_course_repeatability():
    horses = [
        _horse(1, 80, .75, 4.0, .10, [_run(1), _run(2), _run(1)], .30),
        _horse(2, 77, .72, 12.0, .08, [_run(1), _run(2), _run(3), _run(2)], .25),
        _horse(3, 75, .70, 8.0, .05, [_run(4), _run(5), _run(3)], .20),
        _horse(4, 72, .40, 40.0, .02, [_run(8), _run(9), _run(7)], .15),
    ]
    board = chappy.build_signal_board(
        horses, _race(), CONFIG, {"coverage": 1.0}
    )
    boosted = [r for r in board["horses"] if r["condition_boosted"]]
    assert boosted
    row = boosted[0]
    assert row["dynamic_weights"]["condition"] > CONFIG["base_weights"]["condition"]
    assert row["dynamic_weights"]["win"] < CONFIG["base_weights"]["win"]


def test_auto_chappy_portfolio_is_1000_and_not_legendary_without_combo_odds():
    horses = [
        _horse(1, 82, .85, 3.0, .12, [_run(1), _run(2), _run(1)], .32),
        _horse(2, 79, .80, 7.0, .10, [_run(2), _run(1), _run(3)], .26),
        _horse(3, 76, .78, 13.0, .09, [_run(1), _run(2), _run(2)], .22),
        _horse(4, 71, .72, 30.0, .06, [_run(2), _run(3), _run(4)], .12),
        _horse(5, 68, .35, 80.0, .01, [_run(9), _run(8), _run(10)], .08),
    ]
    card, decision = chappy.generate_card(
        horses, _race(), CONFIG, myomi_value=85,
        speed_quality={"coverage":1.0}, combo_odds={},
        model_id="m", model_role="champion",
    )
    assert card["char"] == "chappy"
    assert card["total"] == 1000
    assert sum(b["amt"] for b in card["bets"]) == 1000
    assert decision["otori_gate"]["passed"] is False
    assert decision["otori_gate"]["checks"]["combo_odds"] is False


def test_manual_override_wins_over_auto_portfolio():
    horses = [
        _horse(1, 82, .85, 3.0, .12, [_run(1), _run(2), _run(1)], .32),
        _horse(2, 79, .80, 7.0, .10, [_run(2), _run(1), _run(3)], .26),
        _horse(3, 76, .78, 13.0, .09, [_run(1), _run(2), _run(2)], .22),
        _horse(4, 71, .72, 30.0, .06, [_run(2), _run(3), _run(4)], .12),
    ]
    override = {
        "race_id":"20260922-nakayama-11", "author":"ChatGPT",
        "created_at":"2026-09-22T12:44:00+09:00",
        "rationale":["manual"],
        "bets":[
            {"type":"ワイド","horses":[1,2],"amt":200},
            {"type":"ワイド","horses":[1,3],"amt":100},
            {"type":"ワイド","horses":[2,3],"amt":100},
            {"type":"ワイド","horses":[1,4],"amt":100},
            {"type":"馬連","horses":[1,2],"amt":100},
            {"type":"3連複","horses":[1,2,3],"amt":100},
            {"type":"3連複","horses":[1,3,4],"amt":100},
            {"type":"3連複","horses":[2,3,4],"amt":100},
            {"type":"3連複","horses":[1,2,4],"amt":100},
        ]
    }
    card, decision = chappy.generate_card(
        horses, _race(), CONFIG, myomi_value=50,
        speed_quality={"coverage":1.0}, combo_odds={},
        model_id="m", model_role="champion", manual_override=override,
    )
    assert card["source"] == "manual_chat"
    assert card["bets"] == override["bets"]
    assert decision["manual"]["author"] == "ChatGPT"


def test_otori_is_chappy_state_not_second_card():
    cfg = copy.deepcopy(CONFIG)
    cfg["otori_gate"].update({
        "min_myomi":0, "min_conviction":0, "min_data_quality":0,
        "min_hit_pct_proxy":0, "require_complete_combo_odds":True,
        "min_market_roi_veto":0,
    })
    horses = [
        _horse(1, 82, .85, 3.0, .12, [_run(1), _run(2), _run(1)], .32),
        _horse(2, 79, .80, 7.0, .10, [_run(2), _run(1), _run(3)], .26),
        _horse(3, 76, .78, 13.0, .09, [_run(1), _run(2), _run(2)], .22),
        _horse(4, 71, .72, 30.0, .06, [_run(2), _run(3), _run(4)], .12),
        _horse(5, 68, .65, 45.0, .04, [_run(3), _run(4), _run(2)], .08),
    ]

    # 1回目に自動portfolioの組を知る。gateはオッズ欠損で閉じる。
    first, _ = chappy.generate_card(
        horses, _race(), cfg, myomi_value=100,
        speed_quality={"coverage":1.0}, combo_odds={},
        model_id="m", model_role="champion",
    )
    combo = {"ワイド":{}, "馬連":{}, "3連複":{}}
    for bet in first["bets"]:
        key = "-".join(str(x) for x in sorted(bet["horses"]))
        combo[bet["type"]][key] = [100.0,100.0] if bet["type"]=="ワイド" else 100.0

    card, decision = chappy.generate_card(
        horses, _race(), cfg, myomi_value=100,
        speed_quality={"coverage":1.0}, combo_odds=combo,
        model_id="m", model_role="champion",
    )
    assert decision["otori_gate"]["passed"] is True
    assert card["char"] == "otori"
    assert card["total"] == 1000
    assert sum(b["amt"] for b in card["bets"]) == 1000
