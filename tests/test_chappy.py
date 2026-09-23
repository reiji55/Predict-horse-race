from __future__ import annotations

import sys
from pathlib import Path

# run_pipeline.yml / run_results.yml は `python tests/xxx.py` と**単体スクリプトとして**呼ぶ。
# リポジトリルートを import パスに入れておかないと本番パイプラインのテスト段階で
# ModuleNotFoundError になる（PR #2 でも同じ事故があった）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def _horses():
    """4役ぶんの候補が揃った最小の出走馬リスト。"""
    strong = [_run(1), _run(2), _run(1), _run(3)]
    weak = [_run(9, venue="東京", dist=1600), _run(11, venue="東京", dist=1600)]
    return [
        _horse(1, 82.0, 0.85, 2.6, 0.06, strong, 0.34),
        _horse(2, 76.0, 0.70, 6.2, 0.02, strong[:3], 0.22),
        _horse(3, 71.0, 0.62, 18.0, 0.04, weak, 0.15),
        _horse(4, 66.0, 0.48, 55.0, 0.01, weak, 0.09),
        _horse(5, 60.0, 0.30, 90.0, -0.02, weak, 0.05),
    ]


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


# --- 統合レビュー（2026-09-22）で足した検査 ---------------------------------

def test_manual_override_after_post_time_is_refused(tmp_path, caplog):
    """
    ★ 結果を見たあとに置かれた手動カードを採点させない。

    docs/CHAPPY_MANUAL_OVERRIDE.md は「必ず発走前に作る」と運用ルールで書いていたが、
    コード側に検査が無かった。運用ルールだけに頼ると、2026-09-19 に発走後の再生成を
    採点した事故と同じ構造の穴が残る。
    """
    import datetime

    race_id = "20260927-nakayama-11"
    directory = tmp_path / "data" / "chappy_manual"
    directory.mkdir(parents=True)
    post_at = datetime.datetime(2026, 9, 27, 15, 45, tzinfo=chappy.JST)
    config = {"manual_override_dir": "data/chappy_manual", "total": 1000}

    def write(created_at):
        payload = {"race_id": race_id, "created_at": created_at,
                   "bets": [{"type": "ワイド", "horses": [1, 2], "amt": 1000}]}
        (directory / f"{race_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    write("2026-09-27T13:45:00+09:00")                       # 発走前 → 採用
    accepted = chappy.load_manual_override(race_id, config, root=tmp_path, post_at=post_at)
    assert accepted is not None and accepted["_verified_pre_race"] is True

    for created in ("2026-09-27T15:45:00+09:00", "2026-09-27T17:30:00+09:00"):
        write(created)                                        # 発走時刻以降 → 不採用
        assert chappy.load_manual_override(race_id, config, root=tmp_path, post_at=post_at) is None
    assert "結果を見たあとの買い目" in caplog.text

    for created in (None, "きのう"):                           # 時刻が無い/壊れている → 不採用
        write(created)
        assert chappy.load_manual_override(race_id, config, root=tmp_path, post_at=post_at) is None

    write("2026-09-27T13:45:00+09:00")                        # 発走時刻が不明 → 不採用
    assert chappy.load_manual_override(race_id, config, root=tmp_path, post_at=None) is None


def test_chappy_and_otori_are_never_both_present():
    """★ 1レースに統合レイヤーの枠はひとつだけ。char がどちらかに定まる設計。"""
    card, _ = chappy.generate_card(
        _horses(), _race(), CONFIG, myomi_value=95.0,
        speed_quality={"coverage": 1.0}, combo_odds=None,
        model_id="m", model_role="champion",
    )
    assert card["char"] in ("chappy", "otori")


def test_otori_stays_closed_without_complete_market_odds():
    """式別オッズが1点でも欠けたら、他が全部強くても鳳にしない（fail-closed）。"""
    card, log = chappy.generate_card(
        _horses(), _race(), CONFIG, myomi_value=100.0,
        speed_quality={"coverage": 1.0}, combo_odds=None,
        model_id="m", model_role="champion",
    )
    assert log["otori_gate"]["checks"]["combo_odds"] is False
    assert card["char"] == "chappy"



def test_solid_auto_chappy_uses_depth_role_instead_of_forced_long_edge():
    """SOLIDの自動検証モデルでは4頭目を『高オッズ役』として強制しない。"""
    board = {
        "horses": [
            {"num": 1, "integrated": .9, "role_scores": {
                "win_anchor": 10, "support": 1, "top3_edge": 1, "long_edge": 1, "solid_depth": 1}},
            {"num": 2, "integrated": .8, "role_scores": {
                "win_anchor": 1, "support": 10, "top3_edge": 1, "long_edge": 1, "solid_depth": 1}},
            {"num": 3, "integrated": .7, "role_scores": {
                "win_anchor": 1, "support": 1, "top3_edge": 10, "long_edge": 1, "solid_depth": 1}},
            {"num": 4, "integrated": .2, "role_scores": {
                "win_anchor": 1, "support": 1, "top3_edge": 1, "long_edge": 10, "solid_depth": 1}},
            {"num": 5, "integrated": .6, "role_scores": {
                "win_anchor": 1, "support": 1, "top3_edge": 1, "long_edge": 1, "solid_depth": 10}},
        ]
    }
    normal = chappy.choose_roles(board)
    solid = chappy.choose_roles(board, long_edge_selector="solid_depth")

    assert normal["long_edge"]["num"] == 4
    assert solid["long_edge"]["num"] == 5


def test_manual_chappy_is_not_constrained_by_solid_regime():
    """発走前に作った本線ChatGPTカードは、autoのSOLID方針で書き換えない。"""
    horses = _horses()
    override = {
        "race_id": "20260922-nakayama-11",
        "author": "ChatGPT",
        "created_at": "2026-09-22T12:44:00+09:00",
        "bets": [
            {"type": "ワイド", "horses": [1, 2], "amt": 1000},
        ],
    }
    card, decision = chappy.generate_card(
        horses, _race(), CONFIG, myomi_value=20,
        speed_quality={"coverage": 1.0}, combo_odds={},
        model_id="m", model_role="challenger",
        manual_override=override,
        race_regime={"label": "solid"},
        solid_fourth_role="solid_depth",
    )

    assert card["source"] == "manual_chat"
    assert card["bets"] == override["bets"]
    assert decision["role_policy"]["manual_unconstrained"] is True
    assert decision["role_policy"]["long_edge_selector"] == "long_edge"

if __name__ == "__main__":
    test_condition_profile_rewards_repeat_same_course_distance()
    print("test_condition_profile_rewards_repeat_same_course_distance: OK")
    test_signal_board_dynamically_boosts_exceptional_course_repeatability()
    print("test_signal_board_dynamically_boosts_exceptional_course_repeatability: OK")
    test_auto_chappy_portfolio_is_1000_and_not_legendary_without_combo_odds()
    print("test_auto_chappy_portfolio_is_1000_and_not_legendary_without_combo_odds: OK")
    test_manual_override_wins_over_auto_portfolio()
    print("test_manual_override_wins_over_auto_portfolio: OK")
    test_otori_is_chappy_state_not_second_card()
    print("test_otori_is_chappy_state_not_second_card: OK")
    test_chappy_and_otori_are_never_both_present()
    print("test_chappy_and_otori_are_never_both_present: OK")
    test_otori_stays_closed_without_complete_market_odds()
    print("test_otori_stays_closed_without_complete_market_odds: OK")
    print("\nすべてのテストが通りました（7件）。")
