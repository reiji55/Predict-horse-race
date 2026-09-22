from __future__ import annotations

import sys
from pathlib import Path

# run_pipeline.yml / run_results.yml は `python tests/xxx.py` と**単体スクリプトとして**呼ぶ。
# リポジトリルートを import パスに入れておかないと本番パイプラインのテスト段階で
# ModuleNotFoundError になる（PR #2 でも同じ事故があった）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
from pathlib import Path

from logic import cards
from logic import top3_score

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "cards.json").read_text(encoding="utf-8"))


def test_top3_score_rewards_repeat_same_distance_place_form():
    today = {"surface": "芝", "dist": 2400, "venue": "阪神", "going": "良"}

    stayer = [
        {"surface": "芝", "dist": 2400, "venue": "阪神", "going": "良", "finish": 2, "heads": 12},
        {"surface": "芝", "dist": 2400, "venue": "東京", "going": "良", "finish": 2, "heads": 16},
        {"surface": "芝", "dist": 2400, "venue": "東京", "going": "稍重", "finish": 3, "heads": 18},
    ]
    flashy = [
        {"surface": "芝", "dist": 1800, "venue": "東京", "going": "良", "finish": 1, "heads": 16},
        {"surface": "芝", "dist": 2000, "venue": "中山", "going": "良", "finish": 8, "heads": 18},
        {"surface": "芝", "dist": 1800, "venue": "京都", "going": "良", "finish": 9, "heads": 12},
    ]

    a = top3_score.compute_top3_profile(stayer, today, CONFIG["top3"])
    b = top3_score.compute_top3_profile(flashy, today, CONFIG["top3"])

    assert a is not None and b is not None
    assert a["raw"] > b["raw"]
    assert a["same_dist_runs"] == 3
    assert a["same_dist_top3"] == 3


def test_top3_score_ignores_other_surface():
    today = {"surface": "芝", "dist": 2400, "venue": "阪神", "going": "良"}
    runs = [
        {"surface": "ダ", "dist": 2400, "venue": "阪神", "going": "良", "finish": 1, "heads": 12},
        {"surface": "芝", "dist": 2400, "venue": "阪神", "going": "良", "finish": 5, "heads": 12},
    ]
    profile = top3_score.compute_top3_profile(runs, today, CONFIG["top3"])
    assert profile is not None
    assert profile["n_usable"] == 1
    assert profile["same_dist_runs"] == 1
    assert profile["same_dist_top3"] == 0


def _partner_horses():
    return [
        {"num": 1, "top3_raw": 0.90, "odds": 1.5},
        {"num": 2, "top3_raw": 0.85, "odds": 3.0},
        {"num": 3, "top3_raw": 0.80, "odds": 30.0},
        {"num": 4, "top3_raw": 0.40, "odds": 100.0},
    ]


def test_insurance_and_edge_wide_roles_choose_different_partners():
    horses = _partner_horses()
    cards.assign_place_partner_ranks(horses)

    insurance = cards.order_place_partners(horses, "insurance", CONFIG, axis_num=1)
    edge = cards.order_place_partners(horses, "edge", CONFIG, axis_num=1)

    assert insurance[0]["num"] == 2   # Top3も高く、市場も信頼している
    assert edge[0]["num"] == 3        # Top3が僅差なら30倍側を妙味候補にする
    assert edge[-1]["num"] == 4       # 100倍でもTop3適性が低すぎれば上げない


def test_place_policy_changes_wide_and_trio_but_not_umaren():
    ordered = [
        {"num": 1}, {"num": 2}, {"num": 3}, {"num": 4}
    ]
    place_pool = [
        {"num": 3}, {"num": 4}, {"num": 2}
    ]

    wide = cards._bet_horses_with_place_policy(cards.WIDE, (0, 1), ordered, place_pool)
    trio = cards._bet_horses_with_place_policy(cards.SANRENPUKU, (0, 1, 2), ordered, place_pool)
    umaren = cards._bet_horses_with_place_policy(cards.UMAREN, (0, 1), ordered, place_pool)

    assert wide == [1, 3]
    assert trio == [1, 3, 4]
    assert umaren == [1, 2]  # 勝ち/2着側は従来のWin選定を維持


def test_no_top3_data_falls_back_to_old_template_order():
    ordered = [{"num": 1}, {"num": 2}, {"num": 3}]
    assert cards._bet_horses_with_place_policy(
        cards.WIDE, (0, 1), ordered, []
    ) == [1, 2]

if __name__ == "__main__":
    test_top3_score_rewards_repeat_same_distance_place_form()
    print("test_top3_score_rewards_repeat_same_distance_place_form: OK")
    test_top3_score_ignores_other_surface()
    print("test_top3_score_ignores_other_surface: OK")
    test_insurance_and_edge_wide_roles_choose_different_partners()
    print("test_insurance_and_edge_wide_roles_choose_different_partners: OK")
    test_place_policy_changes_wide_and_trio_but_not_umaren()
    print("test_place_policy_changes_wide_and_trio_but_not_umaren: OK")
    test_no_top3_data_falls_back_to_old_template_order()
    print("test_no_top3_data_falls_back_to_old_template_order: OK")
    print("\nすべてのテストが通りました（5件）。")
