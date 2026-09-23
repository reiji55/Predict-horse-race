from __future__ import annotations

import sys
from pathlib import Path

# run_pipeline.yml / run_results.yml は `python tests/xxx.py` と**単体スクリプトとして**呼ぶ。
# リポジトリルートを import パスに入れておかないと本番パイプラインのテスト段階で
# ModuleNotFoundError になる（PR #2 でも同じ事故があった）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from results import model_compare


def _race(rid, payout, model_id):
    return {
        "race_id": rid,
        "model_id": model_id,
        "cards": [
            {"char": "kei", "hit": payout > 0, "spent": 500, "payout": payout},
            {"char": "gen", "hit": False, "spent": 500, "payout": 0},
        ],
    }


def test_compare_uses_only_common_races():
    champion = {"results": [
        _race("r1", 1000, "champ"),
        _race("r2", 0, "champ"),
        _race("old", 5000, "champ"),
    ]}
    challenger = {"results": [
        _race("r1", 0, "chall"),
        _race("r2", 3000, "chall"),
    ]}

    out = model_compare.compare(champion, {"chall": challenger})
    row = out["comparisons"][0]

    assert row["common_race_ids"] == ["r1", "r2"]
    assert row["common_races"] == 2
    assert row["champion"]["spent"] == 2000
    assert row["champion"]["payout"] == 1000
    assert row["challenger"]["payout"] == 3000
    assert row["delta"]["balance"] == 2000


def test_manual_chat_is_excluded_from_model_comparison():
    manual = _race("manual", 10000, "champ")
    manual["evaluation_scope"] = "manual_chat"
    champion = {"results": [_race("r1", 0, "champ"), manual]}
    challenger = {"results": [_race("r1", 0, "chall"), manual]}

    out = model_compare.compare(champion, {"chall": challenger})
    assert out["comparisons"][0]["common_race_ids"] == ["r1"]


def test_chappy_and_otori_are_excluded_from_fixed_model_comparison():
    champ = _race("r1", 1000, "champ")
    chall = _race("r1", 1000, "chall")
    champ["cards"].append({"char":"chappy","hit":True,"spent":1000,"payout":10000})
    chall["cards"].append({"char":"otori","hit":False,"spent":1000,"payout":0})

    out = model_compare.compare({"results":[champ]}, {"chall":{"results":[chall]}})
    row = out["comparisons"][0]

    # 固定3キャラ比較にはChappy/Otoriの1000円を混ぜない。
    assert row["champion"]["spent"] == 1000
    assert row["challenger"]["spent"] == 1000
    assert row["champion"]["payout"] == 1000
    assert row["challenger"]["payout"] == 1000

def test_missing_challenger_races_are_reported_not_hidden():
    """
    ★ Challenger側で落ちたレースを黙って比較から外さない。

    共通部分だけ集計する設計は正しいが、欠けた件数を出さないと
    「Challengerがこけたレースが消えて、勝てたレースだけ残った」状態に気づけない。
    """
    champion = {"results": [_race(rid, 0, "champ") for rid in ("R1", "R2", "R3")]}
    challenger = {"results": [_race("R1", 2000, "chall")]}

    out = model_compare.compare(champion, {"m": challenger})
    cov = out["comparisons"][0]["coverage"]

    assert cov["champion_races"] == 3
    assert cov["challenger_races"] == 1
    assert cov["common_races"] == 1
    assert cov["missing_in_challenger"] == ["R2", "R3"]
    assert cov["coverage_rate"] == round(1 / 3, 4)



def test_pass_is_an_opportunity_not_a_missed_card():
    champion_race = _race("r1", 0, "champ")
    challenger_race = _race("r1", 0, "chall")
    champion_race["race_regime"] = {"label": "solid"}
    challenger_race["race_regime"] = {"label": "solid"}
    for card in challenger_race["cards"]:
        if card["char"] == "gen":
            card.update({"action": "pass", "budget": 500, "spent": 0, "payout": 0})

    out = model_compare.compare(
        {"results": [champion_race]},
        {"race-regime-abstain-v1": {"results": [challenger_race]}},
    )
    row = out["comparisons"][0]
    gen = row["challenger"]["by_char"]["gen"]

    assert gen["opportunities"] == 1
    assert gen["passes"] == 1
    assert gen["cards"] == 0
    assert gen["hit_rate"] is None
    assert row["challenger"]["passes"] == 1
    assert row["head_to_head"][0]["challenger_passes"] == ["gen"]
    assert row["challenger_by_regime"]["solid"]["passes"] == 1

if __name__ == "__main__":
    test_compare_uses_only_common_races()
    print("test_compare_uses_only_common_races: OK")
    test_manual_chat_is_excluded_from_model_comparison()
    print("test_manual_chat_is_excluded_from_model_comparison: OK")
    test_chappy_and_otori_are_excluded_from_fixed_model_comparison()
    print("test_chappy_and_otori_are_excluded_from_fixed_model_comparison: OK")
    test_missing_challenger_races_are_reported_not_hidden()
    print("test_missing_challenger_races_are_reported_not_hidden: OK")
    print("\nすべてのテストが通りました（4件）。")
