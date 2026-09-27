"""順位ズレ馬の前向き記録（rank-gap-v1）。

「モデル順位は上位なのに市場人気は低い馬」を、**p の温度に依存しない順位だけ**で定義し、
発走前に登録して結果後に数える。p−q / EV は softmax の温度しだいで人気薄ほど大きく出るため
（監査レポート R1）、それとは別の指標として持つ。

定義（config/shadow_research.json の rank_gap）:
    base_rank   ≤ max_base_rank    … 発走前snapshotの marks 順（= base_score 順）
    market_rank ≥ min_market_rank  … 同じ snapshot の単勝オッズ昇順

これは**自動購入条件にもChappyの加点にも使わない**。出現数・3着内数・市場から見た期待値との差を
溜めるだけ。期待値は単勝支持率 q から Harville で出す3着内確率の和（人気薄を低めに出す既知の偏りあり）。
"""
from __future__ import annotations

import itertools
from typing import Any


def base_ranks(marks: list[dict[str, Any]]) -> dict[int, int]:
    """marks は base_score 降順で並んでいる（logic/cards.assign_marks）。"""
    return {int(m["num"]): i for i, m in enumerate(marks, start=1) if m.get("num") is not None}


def market_ranks(marks: list[dict[str, Any]]) -> dict[int, int]:
    """単勝オッズの昇順。同オッズは馬番順で決める（決定的にするため）。"""
    priced = [m for m in marks if m.get("num") is not None and (m.get("odds") or 0) > 0]
    priced.sort(key=lambda m: (float(m["odds"]), int(m["num"])))
    return {int(m["num"]): i for i, m in enumerate(priced, start=1)}


def market_support(marks: list[dict[str, Any]]) -> dict[int, float]:
    inv = {int(m["num"]): 1.0 / float(m["odds"]) for m in marks
           if m.get("num") is not None and (m.get("odds") or 0) > 0}
    total = sum(inv.values())
    return {n: v / total for n, v in inv.items()} if total > 0 else {}


def harville_top3(q: dict[int, float]) -> dict[int, float]:
    """単勝支持率から各馬の3着内確率（Harville）。"""
    out = {n: 0.0 for n in q}
    nums = list(q)
    for order in itertools.permutations(nums, 3):
        prob, remaining = 1.0, 1.0
        for n in order:
            if remaining <= 0:
                prob = 0.0
                break
            prob *= q[n] / remaining
            remaining -= q[n]
        if prob:
            for n in order:
                out[n] += prob
    return out


def register(marks: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """発走前に該当馬を登録する。結果は見ない。"""
    cfg = config["rank_gap"]
    b_ranks = base_ranks(marks)
    m_ranks = market_ranks(marks)
    q = market_support(marks)
    top3_prob = harville_top3(q) if len(q) >= 3 else {}
    names = {int(m["num"]): m.get("name") for m in marks if m.get("num") is not None}
    odds = {int(m["num"]): m.get("odds") for m in marks if m.get("num") is not None}

    horses = []
    for num, b in sorted(b_ranks.items(), key=lambda kv: kv[1]):
        mr = m_ranks.get(num)
        if mr is None:
            continue
        if b <= cfg["max_base_rank"] and mr >= cfg["min_market_rank"]:
            horses.append({
                "num": num,
                "name": names.get(num),
                "base_rank": b,
                "market_rank": mr,
                "win_odds": odds.get(num),
                "q": round(q.get(num, 0.0), 6),
                "market_top3_prob": round(top3_prob.get(num, 0.0), 6),
            })
    return {
        "definition": {
            "max_base_rank": cfg["max_base_rank"],
            "min_market_rank": cfg["min_market_rank"],
            "expected_method": cfg["expected_method"],
        },
        "field_size": len(b_ranks),
        "priced_horses": len(m_ranks),
        "horses": horses,
    }


def evaluate(registration: dict[str, Any], finish: list[int]) -> dict[str, Any]:
    top3 = {int(n) for n in (finish or [])[:3]}
    rows = [
        {**h, "top3": h["num"] in top3}
        for h in registration.get("horses") or []
    ]
    observed = sum(1 for r in rows if r["top3"])
    expected = sum(float(r.get("market_top3_prob") or 0.0) for r in rows)
    return {
        "n": len(rows),
        "observed_top3": observed,
        "expected_top3": round(expected, 6),
        "observed_minus_expected": round(observed - expected, 6),
        "horses": rows,
    }
