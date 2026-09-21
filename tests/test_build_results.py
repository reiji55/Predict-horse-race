"""
build_results（タスク7・成績集計）のテスト。

**`docs/samples/predictions.sample.json` + 公式配当表から `results.sample.json` を再現できるか**
を主テストにしている。サンプルは手で作った「目標の形」なので、これが一致すれば
払戻の計算ルール（データスキーマ仕様§5の4不変条件）の実装が正しいことになる。

実行： python3 tests/test_build_results.py  もしくは  python -m pytest tests/test_build_results.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from results import build_results

ROOT = Path(__file__).resolve().parent.parent
PREDICTIONS_SAMPLE = ROOT / "docs" / "samples" / "predictions.sample.json"
RESULTS_SAMPLE = ROOT / "docs" / "samples" / "results.sample.json"


def _load():
    predictions = json.loads(PREDICTIONS_SAMPLE.read_text(encoding="utf-8"))
    expected = json.loads(RESULTS_SAMPLE.read_text(encoding="utf-8"))
    # Fページ（未実装）の戻り値に相当するデータを、サンプルの結果から取り出す
    race_results = {
        r["race_id"]: {"finish": r["finish"], "dividends": r["dividends"]}
        for r in expected["results"]
    }
    return predictions, expected, race_results


def test_reproduces_results_sample():
    """サンプルの payout・hit・spent がすべて再現されること（§5の計算ルール）。"""
    predictions, expected, race_results = _load()
    built = build_results.build_results(predictions, race_results)

    assert len(built["results"]) == 1          # サンプルに結果があるのは1レースだけ
    actual_race = built["results"][0]
    expected_race = expected["results"][0]

    assert actual_race["race_id"] == expected_race["race_id"]
    assert actual_race["finish"] == expected_race["finish"]
    assert actual_race["dividends"] == expected_race["dividends"]

    actual_cards = {c["char"]: c for c in actual_race["cards"]}
    for expected_card in expected_race["cards"]:
        actual = actual_cards[expected_card["char"]]
        assert actual["hit"] == expected_card["hit"], expected_card["char"]
        assert actual["spent"] == expected_card["spent"], expected_card["char"]
        assert actual["payout"] == expected_card["payout"], expected_card["char"]
        assert actual["bets"] == expected_card["bets"], expected_card["char"]

    print("test_reproduces_results_sample: OK（4カードすべて一致）")


def test_payout_formula():
    """不変条件1：payout = pay × amt ÷ 100。外れは0。"""
    dividends = {"ワイド": [{"horses": [4, 9], "pay": 310}]}
    hit = build_results.settle_bet({"type": "ワイド", "horses": [9, 4], "amt": 200}, dividends)
    assert hit["hit"] is True and hit["payout"] == 620      # 組み合わせは順不同で照合

    miss = build_results.settle_bet({"type": "ワイド", "horses": [4, 1], "amt": 200}, dividends)
    assert miss["hit"] is False and miss["payout"] == 0
    print("test_payout_formula: OK")


def test_invariants_detect_broken_data():
    """配当表と食い違う結果は ValueError で弾かれる（スクレイプミスの早期検知）。"""
    predictions, _, race_results = _load()
    race = predictions["races"][0]
    dividends = race_results[race["id"]]["dividends"]
    prediction_card = race["cards"][0]

    card = build_results.settle_card(prediction_card, dividends)
    build_results.validate_result_invariants(card, prediction_card, dividends)  # 正常系

    for broken in (
        {**card, "payout": card["payout"] + 100},                       # 条件2
        {**card, "spent": card["spent"] - 100},                          # 条件3
        {**card, "hit": not card["hit"]},                                # 条件4
    ):
        try:
            build_results.validate_result_invariants(broken, prediction_card, dividends)
        except ValueError:
            continue
        raise AssertionError(f"検出されるべき不整合が通過しました: {broken['char']}")

    # 条件1：払戻額を書き換えたケース
    tampered = {**card, "bets": [{**card["bets"][0], "payout": 99999}] + card["bets"][1:]}
    try:
        build_results.validate_result_invariants(tampered, prediction_card, dividends)
        raise AssertionError("払戻額の改ざんが検出されませんでした")
    except ValueError:
        pass
    print("test_invariants_detect_broken_data: OK")


def test_spent_must_match_predictions_total():
    """不変条件3：spent は predictions 側の total と一致していなければならない。"""
    predictions, _, race_results = _load()
    race = predictions["races"][0]
    dividends = race_results[race["id"]]["dividends"]
    prediction_card = race["cards"][0]
    card = build_results.settle_card(prediction_card, dividends)

    try:
        build_results.validate_result_invariants(card, {**prediction_card, "total": 999}, dividends)
        raise AssertionError("total との不一致が検出されませんでした")
    except ValueError:
        pass
    print("test_spent_must_match_predictions_total: OK")


def test_settled_card_preserves_model_provenance():
    """Top3相手選びを後から比較できるよう、予想時のレンズとバージョンを結果へ残す。"""
    card = {
        "char": "gen",
        "total": 100,
        "objective": "ev",
        "place_partner_mode": "edge",
        "model_version": "top3-partner-v1",
        "probability_model": {"temperature": 10, "score_weights": {"speed": .25}},
        "bets": [{"type": "ワイド", "horses": [1, 8], "amt": 100}],
    }
    dividends = {"ワイド": [{"horses": [1, 8], "pay": 2180}]}
    settled = build_results.settle_card(card, dividends)

    assert settled["objective"] == "ev"
    assert settled["place_partner_mode"] == "edge"
    assert settled["model_version"] == "top3-partner-v1"
    assert settled["probability_model"] == card["probability_model"]


def test_unfinished_races_are_skipped():
    """結果がまだ出ていないレースは results に載せない（土曜の時点で日曜ぶんは未確定）。"""
    predictions, _, race_results = _load()
    built = build_results.build_results(predictions, {})
    assert built["results"] == []
    assert built["updated_at"].endswith("+09:00")
    print("test_unfinished_races_are_skipped: OK")


def test_summary_is_honest_about_losses():
    """集計は「全カード購入時の累計収支」に一本化。マイナスも隠さない。"""
    predictions, _, race_results = _load()
    built = build_results.build_results(predictions, race_results)
    summary = build_results.summarize(built)

    overall = summary["overall"]
    assert overall["races"] == 1
    assert overall["spent"] == 2500          # 鳳1000 + ケイ500 + 哲500 + 源500
    assert overall["payout"] == 12955        # サンプルの払戻合計
    assert overall["balance"] == overall["payout"] - overall["spent"]
    assert overall["roi"] == round(12955 / 2500, 4)

    assert summary["by_char"]["gen"]["hits"] == 0            # 源さんは外れ
    assert summary["by_char"]["gen"]["balance"] == -500      # マイナスをそのまま出す
    assert set(summary["by_type"]) == {"ワイド", "馬連", "3連複"}
    print("test_summary_is_honest_about_losses: OK（収支 %+d円 / 回収率 %.0f%%）"
          % (overall["balance"], overall["roi"] * 100))


def test_market_benchmark_buys_the_two_favourites():
    """
    市場ベンチマーク：1番人気−2番人気のワイド1点。
    サンプルは9番(3.1倍)と1番(5.6倍)が上位人気で、そのワイドは520円 → 500円買いで2600円。
    """
    predictions, _, race_results = _load()
    race = predictions["races"][0]
    dividends = race_results[race["id"]]["dividends"]

    benchmark = build_results.market_benchmark(race, dividends)
    assert benchmark["horses"] == [9, 1]          # 印の順ではなくオッズの順
    assert benchmark["odds"] == [3.1, 5.6]
    assert benchmark["hit"] is True
    assert benchmark["payout"] == 2600
    print("test_market_benchmark_buys_the_two_favourites: OK")


def test_market_benchmark_needs_two_odds():
    """オッズが揃っていなければ黙って0円と言わず、ベンチマーク自体を作らない。"""
    race = {"marks": [{"num": 4, "odds": None}, {"num": 9, "odds": 3.1}]}
    assert build_results.market_benchmark(race, {}) is None
    print("test_market_benchmark_needs_two_odds: OK")


def test_summary_reports_the_market_baseline_next_to_the_model():
    """**モデルが人気どおり買うより良いか**を、同じ集計の中で比べられること。"""
    predictions, _, race_results = _load()
    built = build_results.build_results(predictions, race_results)
    assert built["results"][0]["benchmark"]["payout"] == 2600

    market = build_results.summarize(built)["market"]
    assert market["races"] == 1 and market["spent"] == 500 and market["payout"] == 2600
    assert market["balance"] == 2100
    assert market["hit_rate"] == 1.0
    print("test_summary_reports_the_market_baseline_next_to_the_model: OK")


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
    print(f"\nすべてのテストが通りました（{len(ALL_TESTS)}件）。")
