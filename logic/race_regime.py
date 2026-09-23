"""発走前の情報だけでレースの「市場状態」を分類する診断レイヤー。

これは「荒れる/荒れない」を当てる予言モデルではない。
単勝市場の集中度と、Winモデルが市場とどの程度同意しているかを測り、

  solid   … 人気上位に支持が集まり、モデルも概ね同意
  open    … 人気が割れている、かつ/またはモデルとの見方も大きく割れている
  neutral … その中間
  unknown … オッズ等が不足して判定不能

とラベル付けする。

v1 は Champion の買い目を変えない。config/models.json の use_race_regime=true
な Challenger だけが、このラベルを「見送り」やChappyの役割選択に使う。
発走前 snapshot にラベルを残すことで、結果を見た後の後付け判定を防ぐ。
"""
from __future__ import annotations

import math
from typing import Any

LABEL_SOLID = "solid"
LABEL_OPEN = "open"
LABEL_NEUTRAL = "neutral"
LABEL_UNKNOWN = "unknown"


def _normalise(values: list[float]) -> list[float]:
    total = sum(values)
    return [v / total for v in values] if total > 0 else []


def _entropy(values: list[float]) -> float:
    """0=一頭集中、1=完全均等の正規化エントロピー。"""
    if len(values) <= 1:
        return 0.0
    raw = -sum(v * math.log(v) for v in values if v > 0)
    return raw / math.log(len(values))


def classify(p: list[float | None], q: list[float | None],
             config: dict[str, Any]) -> dict[str, Any]:
    """モデル勝率 p と市場支持率 q から、発走前の市場状態を返す。"""
    paired = [
        (i, float(p_i), float(q_i))
        for i, (p_i, q_i) in enumerate(zip(p, q))
        if p_i is not None and q_i is not None
    ]
    min_horses = int(config.get("min_horses", 6))
    # 欠損馬を除いた部分集合で再正規化すると、人気薄のオッズ欠損だけで
    # 上位3頭シェアが水増しされ、偽のSOLIDが出る。ペア率が低いときは判定しない。
    min_pair_coverage = float(config.get("min_pair_coverage", 0.0))
    field_size = len(p)
    pair_coverage = len(paired) / field_size if field_size else 0.0
    reason = None
    if len(paired) < min_horses:
        reason = f"usable_horses<{min_horses}"
    elif pair_coverage < min_pair_coverage:
        reason = f"pair_coverage<{min_pair_coverage}"
    if reason is not None:
        return {
            "version": config.get("version"),
            "label": LABEL_UNKNOWN,
            "usable_horses": len(paired),
            "field_size": field_size,
            "pair_coverage": round(pair_coverage, 6),
            "metrics": None,
            "solid_checks": {},
            "open_checks": {},
            "reason": reason,
        }

    indices = [row[0] for row in paired]
    ps = _normalise([row[1] for row in paired])
    qs = _normalise([row[2] for row in paired])

    q_order = sorted(range(len(qs)), key=lambda i: qs[i], reverse=True)
    p_order = sorted(range(len(ps)), key=lambda i: ps[i], reverse=True)
    q_top3 = q_order[:min(3, len(q_order))]
    p_top3 = p_order[:min(3, len(p_order))]

    top1_share = qs[q_order[0]]
    top3_share = sum(qs[i] for i in q_top3)
    entropy = _entropy(qs)
    tv = 0.5 * sum(abs(p_i - q_i) for p_i, q_i in zip(ps, qs))
    overlap = len(set(p_top3) & set(q_top3))

    metrics = {
        "market_top1_share": round(top1_share, 6),
        "market_top3_share": round(top3_share, 6),
        "market_entropy": round(entropy, 6),
        "model_market_tv": round(tv, 6),
        "top3_overlap": overlap,
        "market_top3_indices": [indices[i] for i in q_top3],
        "model_top3_indices": [indices[i] for i in p_top3],
    }

    solid_cfg = config["solid"]
    solid_checks = {
        "market_top3_share": top3_share >= solid_cfg["min_market_top3_share"],
        "market_entropy": entropy <= solid_cfg["max_market_entropy"],
        "model_market_tv": tv <= solid_cfg["max_model_market_tv"],
        "top3_overlap": overlap >= solid_cfg["min_top3_overlap"],
    }

    open_cfg = config["open"]
    open_checks = {
        "market_top3_share": top3_share <= open_cfg["max_market_top3_share"],
        "market_entropy": entropy >= open_cfg["min_market_entropy"],
        "model_market_tv": tv >= open_cfg["min_model_market_tv"],
    }

    if all(solid_checks.values()):
        label = LABEL_SOLID
        reason = "market_concentrated_and_model_agrees"
    elif sum(bool(v) for v in open_checks.values()) >= int(open_cfg.get("min_checks", 2)):
        label = LABEL_OPEN
        reason = "market_or_model_is_open"
    else:
        label = LABEL_NEUTRAL
        reason = "mixed_signals"

    return {
        "version": config.get("version"),
        "label": label,
        "usable_horses": len(paired),
        "field_size": field_size,
        "pair_coverage": round(pair_coverage, 6),
        "metrics": metrics,
        "solid_checks": solid_checks,
        "open_checks": open_checks,
        "reason": reason,
    }


def should_abstain(char_id: str, regime: dict[str, Any],
                   config: dict[str, Any]) -> bool:
    """キャラのpass_onに現在ラベルが含まれるか。unknownでは見送らない。"""
    label = regime.get("label")
    if label == LABEL_UNKNOWN:
        return False
    policy = (config.get("policies") or {}).get(char_id) or {}
    return label in set(policy.get("pass_on") or [])


def abstain_comment(char_id: str, config: dict[str, Any]) -> str:
    return str(((config.get("policies") or {}).get(char_id) or {}).get("comment") or "")
