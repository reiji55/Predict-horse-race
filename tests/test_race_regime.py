from __future__ import annotations

import json
from pathlib import Path

from logic import cards, race_regime

ROOT = Path(__file__).resolve().parent.parent
REGIME_CONFIG = json.loads((ROOT / "config" / "race_regime.json").read_text(encoding="utf-8"))
CARDS_CONFIG = json.loads((ROOT / "config" / "cards.json").read_text(encoding="utf-8"))


def test_solid_requires_market_concentration_and_model_agreement():
    q = [0.38, 0.22, 0.12, 0.06, 0.05, 0.05, 0.04, 0.03, 0.03, 0.02]
    p = [0.36, 0.23, 0.13, 0.07, 0.05, 0.05, 0.04, 0.03, 0.025, 0.015]

    out = race_regime.classify(p, q, REGIME_CONFIG)

    assert out["label"] == "solid"
    assert all(out["solid_checks"].values())
    assert out["metrics"]["top3_overlap"] == 3


def test_open_means_market_is_dispersed_not_that_an_upset_is_guaranteed():
    # 完全均等に近い市場。結果が荒れると断定するのではなく「事前に軸が定まりにくい」状態。
    q = [0.1] * 10
    p = [0.1] * 10

    out = race_regime.classify(p, q, REGIME_CONFIG)

    assert out["label"] == "open"
    assert out["open_checks"]["market_top3_share"] is True
    assert out["open_checks"]["market_entropy"] is True


def test_solid_market_is_not_solid_when_model_strongly_disagrees():
    q = [0.40, 0.22, 0.12, 0.06, 0.05, 0.04, 0.04, 0.03, 0.02, 0.02]
    p = [0.05, 0.05, 0.05, 0.30, 0.20, 0.12, 0.08, 0.06, 0.05, 0.04]

    out = race_regime.classify(p, q, REGIME_CONFIG)

    assert out["label"] != "solid"
    assert out["solid_checks"]["model_market_tv"] is False


def test_insufficient_data_is_unknown_and_never_forces_a_pass():
    out = race_regime.classify([0.5, 0.5], [0.6, 0.4], REGIME_CONFIG)

    assert out["label"] == "unknown"
    assert race_regime.should_abstain("gen", out, REGIME_CONFIG) is False


def test_low_pair_coverage_is_unknown_not_a_subset_solid():
    """人気薄のオッズ欠損を部分集合で再正規化すると上位集中が水増しされる。判定しない。"""
    q_full = [0.30, 0.18, 0.12, 0.08, 0.07, 0.06, 0.05, 0.04, 0.04, 0.03, 0.02, 0.01]
    p_full = [0.29, 0.19, 0.12, 0.08, 0.07, 0.06, 0.05, 0.04, 0.04, 0.03, 0.02, 0.01]
    # 下位6頭のオッズが欠損（q=None）。残り6頭だけなら top3 share は約0.75で SOLID になり得る。
    q_missing = q_full[:6] + [None] * 6

    out = race_regime.classify(p_full, q_missing, REGIME_CONFIG)

    assert out["label"] == "unknown"
    assert out["reason"].startswith("pair_coverage<")
    assert out["pair_coverage"] == 0.5
    assert race_regime.should_abstain("gen", out, REGIME_CONFIG) is False


def test_one_scratched_horse_still_classifies():
    """取消1頭程度（16頭中15頭）でラベルを失わない。"""
    q = [0.30, 0.18, 0.12, 0.08, 0.07, 0.05, 0.04, 0.03, 0.03, 0.02, 0.02, 0.02, 0.02, 0.01, 0.01, None]
    p = [0.29, 0.19, 0.12, 0.08, 0.07, 0.05, 0.04, 0.03, 0.03, 0.02, 0.02, 0.02, 0.02, 0.01, 0.01, 0.0]

    out = race_regime.classify(p, q, REGIME_CONFIG)

    assert out["label"] != "unknown"
    assert out["usable_horses"] == 15
    assert out["field_size"] == 16


def test_only_gen_passes_on_solid_in_v1():
    regime = {"label": "solid"}

    assert race_regime.should_abstain("gen", regime, REGIME_CONFIG) is True
    assert race_regime.should_abstain("kei", regime, REGIME_CONFIG) is False
    assert race_regime.should_abstain("tetsu", regime, REGIME_CONFIG) is False


def test_pass_card_is_zero_yen_but_keeps_normal_budget_for_audit():
    regime = {"label": "solid", "version": "race-regime-v1"}
    card = cards.generate_abstain_card(
        "gen", CARDS_CONFIG, model_id="m", model_role="challenger",
        reason="race_regime:solid", say="見送るぜ。", race_regime=regime,
    )

    assert card["action"] == "pass"
    assert card["total"] == 0
    assert card["budget"] == 500
    assert card["bets"] == []
    cards.validate_card_invariants(card, marks=[], amt_unit=100)
