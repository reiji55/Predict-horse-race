"""PR-C0：Race Performance v1 の事前登録（config/race_performance_v1.json）が、文書の記録と食い違わないことを確かめる。

計算の実装ではない（RPS の実装は PR-C1）。ここで見るのは次の4つだけ。
- 設定の canonical SHA-256 が、事前登録の文書（§1 登録記録）に書いた値と同じ。予告なく設定が変わったら落ちる
- 重みの合計と、既存3因子からの導き方（0.85 倍＋RPS 0.15）
- class の並びが既存の正規化のキーと同じで、強さが単調に増える
- 文書の検算例（§4.6）が、登録したパラメータで計算した値と合う
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from logic import model_registry
from scraper.common import constants

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "race_performance_v1.json"
DOC_PATH = ROOT / "docs" / "research" / "RACE_PERFORMANCE_PREREG_V1.md"


def _config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _canonical_sha256(payload):
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_registered_sha256_matches_the_config():
    doc = DOC_PATH.read_text(encoding="utf-8")
    recorded = re.search(r"設定の canonical SHA-256 \| `([0-9a-f]{64})`", doc)
    assert recorded, "文書の登録記録に SHA-256 が無い"
    assert recorded.group(1) == _canonical_sha256(_config()), \
        "config/race_performance_v1.json が登録した内容と違う（変えるなら新しい version として登録し直す）"


def test_config_is_not_part_of_the_production_config_hash():
    assert "race_performance_v1.json" not in model_registry.HASH_CONFIGS


def test_score_weights_sum_to_one_and_follow_the_registered_derivation():
    integration = _config()["integration"]
    weights = integration["score_weights"]
    derived = integration["derived_from"]
    assert set(weights) == {"speed", "aptitude", "human", "race_performance"}
    assert math.isclose(sum(weights.values()), 1.0, abs_tol=1e-12)
    for factor, base in derived["score_weights_at_registration"].items():
        assert math.isclose(weights[factor], base * derived["existing_factors_scale"], abs_tol=1e-12), factor
    assert math.isclose(weights["race_performance"], derived["race_performance_weight"], abs_tol=1e-12)
    assert integration["character_score_weights"] == "unchanged_3_factor"


def test_class_strength_uses_the_existing_class_keys_in_order():
    strength = _config()["class_strength"]
    assert list(strength) == constants.CLASS_KEYS
    values = list(strength.values())
    assert values == sorted(values) and len(set(values)) == len(values)
    assert values[-1] == 1.0


def test_weights_and_limits_are_a_single_registered_set():
    cfg = _config()
    recency = cfg["weights"]["recency_by_input_position"]
    assert len(recency) == cfg["inputs"]["max_runs"] == 5
    assert recency == sorted(recency, reverse=True)
    assert cfg["usable_run"]["max_abs_dist_diff_m"] * 2 == cfg["weights"]["dist_decay_m"]   # w_dist は 0.5〜1.0
    assert cfg["aggregate"]["min_usable_runs"] == 2
    assert cfg["margin"]["tau_sec"] == 1.0
    assert "finish" in cfg["inputs"]["not_used"] and "win_odds" in cfg["inputs"]["not_used"]
    assert "long_history" in cfg["inputs"]["not_used"]


def test_worked_examples_in_the_document_match_the_registered_parameters():
    """§4.6 の検算例を、登録したパラメータだけで計算し直す（PR-C1 の実装が満たすべき値）。"""
    cfg = _config()
    strength, tau = cfg["class_strength"], cfg["margin"]["tau_sec"]
    recency, decay = cfg["weights"]["recency_by_input_position"], cfg["weights"]["dist_decay_m"]

    def v_run(cls, margin):
        return strength[cls] * math.exp(-max(0.0, margin) / tau)

    assert round(v_run("g1", 0.0), 6) == 1.0
    assert round(v_run("3win", 0.0), 6) == 0.55
    assert round(v_run("g2", 0.1), 6) == 0.814354
    assert round(v_run("g2", 2.0), 6) == 0.121802
    assert v_run("g1", 0.0) > v_run("3win", 0.0) and v_run("g2", 0.1) > v_run("g2", 2.0)

    # 位置0・2・4が有効（位置1・3は除外）。重みは入力位置のまま
    usable = [(0, "g1", 0.0, 0), (2, "3win", 0.5, 200), (4, "op", 1.2, 400)]
    num = sum(v_run(c, m) * recency[p] * (1 - d / decay) for p, c, m, d in usable)
    den = sum(recency[p] * (1 - d / decay) for p, c, m, d in usable)
    assert round(num, 6) == 1.263406 and round(den, 6) == 1.9
    assert round(num / den, 6) == 0.66495

    doc = DOC_PATH.read_text(encoding="utf-8")
    for shown in ("1.000000", "0.550000", "0.814354", "0.121802", "1.263406 / 1.9", "0.664950"):
        assert shown in doc, shown
