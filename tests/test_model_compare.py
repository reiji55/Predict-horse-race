from __future__ import annotations

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
