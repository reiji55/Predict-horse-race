"""固定3人の多様性モニター（diversity-monitor-v1）。観測のみ。

毎レース、ケイ・哲・源のペアごとに次の3つを記録する。

    sel_spearman … 各キャラの選定順（sel 降順）の順位相関。発走前に再計算した並びが要る
    axis_match   … 軸（選定順の先頭＝全テンプレの先頭馬）が同じか
    jaccard      … 買い目に登場する馬の集合の Jaccard 係数

redundancy_flag（軸一致 かつ Jaccard ≥ min_jaccard）は「似すぎ」の目印にすぎない。
**似ているからといって何かを変更する処理はここには無い。**
"""
from __future__ import annotations

import copy
import itertools
from typing import Any

from logic import cards


def character_orders(horses: list[dict[str, Any]], cards_config: dict[str, Any],
                     temperature: float, characters: list[str]) -> dict[str, list[dict[str, Any]]]:
    """本番と同じ関数でキャラ別の選定順を再計算する（horses は壊さない）。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for char_id in characters:
        char_config = cards_config["characters"][char_id]
        work = copy.deepcopy(horses)
        cards.assign_character_ranks(work, cards_config, char_config, temperature)
        ordered = cards.select_horses(
            work, char_config["lambda"], char_config.get("axis_base_rank_floor"),
            base_rank_key="sel_base_rank", value_rank_key="sel_value_rank",
        )
        out[char_id] = [{"num": h["num"], "sel": round(h["sel"], 6)} for h in ordered]
    return out


def card_horse_sets(snapshot_cards: list[dict[str, Any]], characters: list[str]
                    ) -> dict[str, dict[str, Any]]:
    """snapshotのカードから、キャラごとの軸と買い目馬集合。"""
    out: dict[str, dict[str, Any]] = {}
    for card in snapshot_cards or []:
        char_id = card.get("char")
        if char_id not in characters or card.get("action") == "pass" or not card.get("bets"):
            continue
        horses = sorted({int(n) for bet in card["bets"] for n in bet["horses"]})
        out[char_id] = {"axis": int(card["bets"][0]["horses"][0]), "horses": horses}
    return out


def _spearman(order_a: list[int], order_b: list[int]) -> float | None:
    common = [n for n in order_a if n in set(order_b)]
    if len(common) < 3:
        return None
    rank_a = {n: i for i, n in enumerate([n for n in order_a if n in common])}
    rank_b = {n: i for i, n in enumerate([n for n in order_b if n in common])}
    n = len(common)
    d2 = sum((rank_a[x] - rank_b[x]) ** 2 for x in common)
    return round(1 - 6 * d2 / (n * (n * n - 1)), 6)


def jaccard(a: list[int], b: list[int]) -> float | None:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return None
    return round(len(sa & sb) / len(sa | sb), 6)


def evaluate(orders: dict[str, list[dict[str, Any]]] | None,
             sets: dict[str, dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    cfg = config["diversity"]
    flag_cfg = cfg["redundancy_flag"]
    chars = [c for c in cfg["characters"] if c in sets]
    pairs = []
    for a, b in itertools.combinations(chars, 2):
        spearman = None
        if orders and a in orders and b in orders:
            spearman = _spearman([r["num"] for r in orders[a]], [r["num"] for r in orders[b]])
        axis_a = orders[a][0]["num"] if orders and orders.get(a) else sets[a]["axis"]
        axis_b = orders[b][0]["num"] if orders and orders.get(b) else sets[b]["axis"]
        jac = jaccard(sets[a]["horses"], sets[b]["horses"])
        axis_match = axis_a == axis_b
        redundant = (
            (axis_match or not flag_cfg["require_axis_match"])
            and jac is not None and jac >= flag_cfg["min_jaccard"]
        )
        pairs.append({
            "pair": f"{a}-{b}",
            "sel_spearman": spearman,
            "axis": [axis_a, axis_b],
            "axis_match": axis_match,
            "jaccard": jac,
            "redundancy_flag": redundant,
        })
    return {
        "sel_orders_available": bool(orders),
        "axes": {c: (orders[c][0]["num"] if orders and orders.get(c) else sets[c]["axis"]) for c in chars},
        "horse_sets": {c: sets[c]["horses"] for c in chars},
        "pairs": pairs,
    }
