"""
Top3 / Place Score — 「勝ち切る力」と分離した、3着以内に残る適性のランキング用指標。

重要:
- これは確率ではない。0〜1付近のヒューリスティックな生スコア。
- win probability p や market EV の計算には直接入れない。
- ワイド / 3連複の「相手候補を誰にするか」にだけ使う。

2026-09-21 神戸新聞杯の反省:
勝ち候補としては低評価でも、同距離で繰り返し2〜3着以内に残っている人気薄は
ワイド/3連複では価値がある。Win Score と Top3 Score を分離して、その性質を表現する。

各過去走:
  top3 = 1 if finish <= 3 else 0
  perf = (heads - finish + 0.5) / heads
  run_value = w_top3 * top3 + w_perf * perf

重み:
  recency_weight
  × (1 + b_dist*dist_close + b_venue*[同場] + b_going*[同馬場])

surface不一致は使わない。距離は dist_tol 内で連続的に近さを評価する。
"""
from __future__ import annotations

from typing import Any


def _dist_close(today_dist: int, run_dist: int, dist_tol: float) -> float:
    if dist_tol <= 0:
        return 1.0 if today_dist == run_dist else 0.0
    return max(0.0, 1.0 - abs(today_dist - run_dist) / dist_tol)


def compute_run_value(run: dict[str, Any], today_course: dict[str, Any],
                      config: dict[str, Any]) -> tuple[float, float] | None:
    """1走分の (価値, 条件近接重み) を返す。使えない走は None。"""
    finish = run.get("finish")
    heads = run.get("heads")
    run_dist = run.get("dist")
    today_dist = today_course.get("dist")
    if finish is None or heads is None or heads <= 0 or run_dist is None or today_dist is None:
        return None

    if run.get("surface") != today_course.get("surface"):
        return None

    top3 = 1.0 if finish <= 3 else 0.0
    perf = (heads - finish + 0.5) / heads
    value = config["w_top3"] * top3 + config["w_perf"] * perf

    proximity = 1.0 + config["b_dist"] * _dist_close(
        int(today_dist), int(run_dist), float(config["dist_tol"])
    )
    if run.get("venue") == today_course.get("venue"):
        proximity += config["b_venue"]
    if (today_course.get("going") is not None
            and run.get("going") == today_course.get("going")):
        proximity += config["b_going"]

    return value, proximity


def compute_top3_profile(past_runs: list[dict[str, Any]],
                         today_course: dict[str, Any],
                         config: dict[str, Any]) -> dict[str, Any] | None:
    """
    直近走からTop3生スコアと監査用の証拠量を返す。

    戻り値:
      raw              ... 0〜1付近。確率ではない
      n_usable         ... 同surfaceかつ着順/頭数/距離が使える走数
      same_dist_runs   ... 今日と同距離の走数
      same_dist_top3   ... 同距離で3着以内だった回数
    """
    recency = config["recency_weights"]
    weighted_sum = 0.0
    weight_total = 0.0
    n_usable = 0
    same_dist_runs = 0
    same_dist_top3 = 0
    today_dist = today_course.get("dist")

    for position, run in enumerate(past_runs):
        evaluated = compute_run_value(run, today_course, config)
        if evaluated is None:
            continue
        value, proximity = evaluated
        recency_w = recency[position] if position < len(recency) else recency[-1]
        weight = recency_w * proximity
        weighted_sum += weight * value
        weight_total += weight
        n_usable += 1

        if today_dist is not None and run.get("dist") == today_dist:
            same_dist_runs += 1
            if run.get("finish") is not None and run["finish"] <= 3:
                same_dist_top3 += 1

    if weight_total <= 0:
        return None

    return {
        "raw": weighted_sum / weight_total,
        "n_usable": n_usable,
        "same_dist_runs": same_dist_runs,
        "same_dist_top3": same_dist_top3,
    }
