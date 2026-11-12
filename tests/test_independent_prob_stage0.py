"""research/independent_prob_stage0.py の検証（Issue #57 Stage 0 監査）。"""
from __future__ import annotations

from research import independent_prob_stage0 as s0


def test_payout_match_labels() -> None:
    d = {"markets": {"sanrenpuku": {"odds": {"2-5-9": 25.9}},
                     "umaren": {"odds": {"2-9": 5.8}},
                     "wide": {"odds": {"2-9": [2.4, 2.6], "5-9": [7.0, 7.4]}}}}
    res = {"dividends": {"3連複": [{"horses": [9, 2, 5], "pay": 2590}],
                         "馬連": [{"horses": [2, 9], "pay": 590}],
                         "ワイド": [{"horses": [2, 9], "pay": 250}, {"horses": [5, 9], "pay": 670}]}}
    out = s0._payout_match(d, res)
    assert out["3連複"] == {"rows": [{"combo": "2-5-9", "pay": 2590, "odds": 25.9, "check": "match"}], "match": True}
    assert out["馬連"]["rows"][0]["check"] == "mismatch" and out["馬連"]["match"] is False
    assert [r["check"] for r in out["ワイド"]["rows"]] == ["within_range", "outside_range"]
    unlisted = s0._payout_match({"markets": {"sanrenpuku": {"odds": {}}}},
                                {"dividends": {"3連複": [{"horses": [6, 12, 15], "pay": 1635170}]}})
    assert unlisted["3連複"]["rows"][0]["check"] == "combo_not_listed" and unlisted["3連複"]["match"] is None


def test_diagnostics_reproduces_bins_and_respects_flags() -> None:
    races = [{"race_id": "a", "flags": [], "log_loss": {"champion": 2.0, "market": 1.5}},
             {"race_id": "b", "flags": ["leading_stats_partial"], "log_loss": {"champion": 1.0, "market": 2.0}},
             {"race_id": "c", "flags": [], "log_loss": {"champion": 1.0, "market": None}}]
    horses = [{"race_id": "a", "p_champion": 0.5, "q_market": 0.2, "y": 1},
              {"race_id": "a", "p_champion": 0.5, "q_market": 0.8, "y": 0},
              {"race_id": "b", "p_champion": 0.3, "q_market": 0.3, "y": 1}]
    all_ = s0.diagnostics(horses, races, set())
    assert all_["races"] == 2 and all_["horses"] == 3          # 市場の無いレースは数えない
    assert all_["mean_log_loss"] == {"champion": 1.5, "market": 1.75, "champion_minus_market": -0.25}
    assert all_["races_model_better"] == 1
    bins = {b["p_over_q"]: (b["horses"], b["actual_wins"]) for b in all_["p_over_q_bins"]}
    assert bins["0.0–0.5"] == (0, 0) and bins["0.5–0.8"] == (1, 0) and bins["0.8–1.25"] == (1, 1) and bins["2.0–∞"] == (1, 1)
    clean = s0.diagnostics(horses, races, {"leading_stats_partial"})
    assert clean["races"] == 1 and clean["horses"] == 2
