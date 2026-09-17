"""
② 適性スコア（買い目生成仕様_v1.md §1.4）

「今日の条件への当てはまり」を過去走の着順から作る。①スピード指数が「素の時計」なのに対し、
②は「この条件で実際に走れるか」を見る。役割を被らせないのが肝。

設定：config/cards.json の "aptitude" セクション（w_surface, b_dist, b_venue, b_going, dist_tol）

--- 計算式（§1.4） ---

  (a) 各過去走を今日の条件との近さで重み付け

      prox = w_surface × [芝ダ一致] × ( 1 + b_dist × dist_close
                                          + b_venue × [同場]
                                          + b_going × [同馬場状態] )

      dist_close = max(0, 1 − |today_dist − run_dist| / dist_tol)

      **芝ダ不一致は prox=0**（芝の実績はダートの今日にほぼ効かない）。
      該当走が実質0本になった馬は②欠損 → base_score 側で重み再正規化（§1.2）。

  (b) 着順を頭数正規化してから prox 加重平均

      perf = (heads − finish + 0.5) / heads      # 1着に近いほど1、頭数の大小を吸収
      ②    = Σ prox × perf / Σ prox

      頭数正規化により「16頭立て3着」と「8頭立て3着」の価値差が揃う。

finish が null の走（中止・除外・取消）と、heads / dist が欠損している走は
perf を作れないため対象外とする（取得項目仕様§2.0「取れなかったらnull」の下流処理）。
"""
from __future__ import annotations

from typing import Any


def _dist_close(today_dist: int, run_dist: int, dist_tol: float) -> float:
    """距離の近さ（1.0=同距離, 0.0=dist_tol以上離れている）。"""
    if dist_tol <= 0:
        return 1.0 if today_dist == run_dist else 0.0
    return max(0.0, 1.0 - abs(today_dist - run_dist) / dist_tol)


def compute_prox(run: dict[str, Any], today_course: dict[str, Any], config: dict[str, Any]) -> float:
    """1走分の「今日の条件との近さ」重みを返す（買い目生成仕様§1.4(a)）。芝ダ不一致なら0。"""
    today_surface = today_course.get("surface")
    if today_surface is None or run.get("surface") != today_surface:
        return 0.0

    today_dist = today_course.get("dist")
    run_dist = run.get("dist")
    if today_dist is None or run_dist is None:
        return 0.0

    bonus = config["b_dist"] * _dist_close(today_dist, run_dist, config["dist_tol"])
    if today_course.get("venue") is not None and run.get("venue") == today_course.get("venue"):
        bonus += config["b_venue"]
    if today_course.get("going") is not None and run.get("going") == today_course.get("going"):
        bonus += config["b_going"]

    return config["w_surface"] * (1.0 + bonus)


def compute_perf(run: dict[str, Any]) -> float | None:
    """着順を頭数正規化した成績値（買い目生成仕様§1.4(b)）。1着に近いほど1に近い。"""
    finish = run.get("finish")
    heads = run.get("heads")
    if finish is None or heads is None or heads <= 0:
        return None
    return (heads - finish + 0.5) / heads


def compute_aptitude(past_runs: list[dict[str, Any]], today_course: dict[str, Any],
                     config: dict[str, Any]) -> float | None:
    """
    馬1頭の past_runs と今日のコース条件から適性スコアの生値（z標準化前）を計算する。

    today_course: {"surface": "芝", "dist": 1200, "venue": "小倉", "going": "良"}
                  going は当日朝の馬場状態（raw の races[].going）。未取得なら同馬場ボーナスは付かない
    戻り値: 加重平均値（0〜1程度）。該当走が0本なら None（§1.2で重み再正規化される）
    """
    weighted_sum = 0.0
    prox_total = 0.0

    for run in past_runs:
        perf = compute_perf(run)
        if perf is None:
            continue
        prox = compute_prox(run, today_course, config)
        if prox <= 0:
            continue
        weighted_sum += prox * perf
        prox_total += prox

    if prox_total <= 0:
        return None
    return weighted_sum / prox_total
