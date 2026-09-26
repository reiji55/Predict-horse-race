"""確率較正モニター（calibration-monitor-v1）。

各レース・全出走馬について、単勝の的中を「多クラス（勝ち馬1頭）」とみなし、

    log_loss = −ln p(勝ち馬)
    brier    = Σ_i (p_i − y_i)²      （y_i は勝ち馬だけ1）

を、次の確率で比べる（小さいほど良い）。

    p_current        … 本番と同じ softmax(score / T_current)。Championの p そのもの
    q_market         … 単勝オッズから作る市場支持率
    p_T{t}           … 事前に固定した温度候補（config）。**結果を見て選ばない**
    p_entropy_match  … 発走前のオッズだけで決める温度（p のエントロピーを q に合わせる）。結果は使わない
    p_walk_forward   … **そのレースより前に確定したレースだけ**で対数損失が最小になる温度。
                        そのレース自身の結果は決して使わない（min_history_races に満たなければ記録しない）

Championの temperature は一切変えない。ここでの比較はすべて shadow。

評価対象は「score と単勝オッズが両方ある馬」。取消などで欠ける馬がいれば、その馬を除いて
p と q を同じ集合で再正規化する（片方だけ欠けた馬で比較が歪まないように）。
対象がフィールドの min_pair_coverage 未満、または勝ち馬が対象外なら、そのレースは評価しない。
"""
from __future__ import annotations

import math
from typing import Any


def _softmax(scores: dict[int, float], temperature: float) -> dict[int, float]:
    top = max(scores.values())
    exps = {n: math.exp((s - top) / temperature) for n, s in scores.items()}
    total = sum(exps.values())
    return {n: v / total for n, v in exps.items()}


def _normalise(values: dict[int, float]) -> dict[int, float]:
    total = sum(values.values())
    return {n: v / total for n, v in values.items()} if total > 0 else {}


def _entropy(prob: dict[int, float]) -> float:
    return -sum(p * math.log(p) for p in prob.values() if p > 0)


def win_metrics(prob: dict[int, float], winner: int, eps: float = 1e-12) -> dict[str, float]:
    p_win = max(prob.get(winner, 0.0), eps)
    brier = sum((p - (1.0 if n == winner else 0.0)) ** 2 for n, p in prob.items())
    return {"log_loss": round(-math.log(p_win), 6), "brier": round(brier, 6),
            "p_winner": round(prob.get(winner, 0.0), 6)}


def evaluation_set(horses: list[dict[str, Any]], min_pair_coverage: float
                   ) -> tuple[dict[int, float], dict[int, float], dict[str, Any]]:
    """(scores, odds, info)。score と正のオッズが両方ある馬だけ。"""
    field = [h for h in horses if h.get("num") is not None]
    scores = {int(h["num"]): float(h["score"]) for h in field
              if h.get("score") is not None and (h.get("odds") or 0) > 0}
    odds = {int(h["num"]): float(h["odds"]) for h in field
            if int(h["num"]) in scores}
    coverage = len(scores) / len(field) if field else 0.0
    info = {"field_size": len(field), "evaluated_horses": len(scores),
            "coverage": round(coverage, 6),
            "usable": bool(field) and len(scores) >= 2 and coverage >= min_pair_coverage}
    return scores, odds, info


def entropy_match_temperature(scores: dict[int, float], q: dict[int, float],
                              t_min: float, t_max: float, iterations: int = 60) -> float:
    """H(softmax(score/T)) = H(q) となる T を二分法で求める。発走前情報だけで決まる。"""
    target = _entropy(q)
    lo, hi = float(t_min), float(t_max)
    if _entropy(_softmax(scores, lo)) >= target:
        return lo
    if _entropy(_softmax(scores, hi)) <= target:
        return hi
    for _ in range(iterations):
        mid = (lo + hi) / 2
        if _entropy(_softmax(scores, mid)) < target:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 6)


def walk_forward_temperature(history: list[dict[str, Any]], grid: list[float],
                             min_history: int, eps: float = 1e-12) -> float | None:
    """過去レースだけで平均対数損失が最小の温度。history が足りなければ None。"""
    if len(history) < min_history:
        return None
    best_t, best_loss = None, math.inf
    for t in grid:
        loss = sum(
            -math.log(max(_softmax(h["scores"], float(t)).get(h["winner"], 0.0), eps))
            for h in history
        ) / len(history)
        if loss < best_loss - 1e-12:
            best_t, best_loss = float(t), loss
    return best_t


def evaluate_race(inputs: dict[str, Any], winner: int | None,
                  history: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """1レース分。history にはこのレースより前に発走・確定したレースだけを渡すこと。"""
    cfg = config["calibration"]
    eps = float(cfg.get("eps", 1e-12))
    scores, odds, info = evaluation_set(inputs.get("horses") or [], float(cfg["min_pair_coverage"]))
    out: dict[str, Any] = {
        "score_source": inputs.get("score_source"),
        "temperature_current": inputs.get("temperature_current"),
        "temperature_source": inputs.get("temperature_source", "prerace_record"),
        "evaluation_set": info,
        "winner": winner,
        "models": {},
    }
    if not info["usable"]:
        out["status"] = "insufficient_coverage"
        return out
    if winner is None or winner not in scores:
        out["status"] = "winner_not_evaluable"
        return out

    q = _normalise({n: 1.0 / o for n, o in odds.items()})
    t_current = float(inputs["temperature_current"])
    models: dict[str, tuple[dict[int, float], float | None]] = {
        "p_current": (_softmax(scores, t_current), t_current),
        "q_market": (q, None),
    }
    for t in cfg["temperature_candidates"]:
        models[f"p_T{t:g}"] = (_softmax(scores, float(t)), float(t))
    em = cfg.get("entropy_match") or {}
    if em.get("enabled"):
        t_em = entropy_match_temperature(scores, q, em["t_min"], em["t_max"])
        models["p_entropy_match"] = (_softmax(scores, t_em), t_em)
    wf = cfg["walk_forward"]
    t_wf = walk_forward_temperature(history, wf["grid"], int(wf["min_history_races"]), eps)
    out["walk_forward"] = {"history_races": len(history), "temperature": t_wf}
    if t_wf is not None:
        models["p_walk_forward"] = (_softmax(scores, t_wf), t_wf)

    for name, (prob, t) in models.items():
        out["models"][name] = {**win_metrics(prob, winner, eps), "temperature": t}
    out["status"] = "evaluated"
    # 次のレースの walk-forward 用（結果確定後の情報なので、このレース自身の評価には使っていない）
    out["history_item"] = {"scores": scores, "winner": winner}
    return out
