from __future__ import annotations

import datetime
from typing import Any

JST = datetime.timezone(datetime.timedelta(hours=9))

# Champion/Challengerは固定モデル同士の比較。Chappy/Otoriは独立した統合レイヤーなので除外。
MODEL_COMPARE_CHARS = {"kei", "tetsu", "gen"}


def _eligible(result: dict[str, Any]) -> bool:
    return result.get("evaluation_scope") != "manual_chat"


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    spent = payout = cards = hits = passes = opportunities = 0
    by_char: dict[str, dict[str, int]] = {}
    for race in results:
        for card in race.get("cards", []):
            if card.get("char") not in MODEL_COMPARE_CHARS:
                continue
            opportunities += 1
            char = by_char.setdefault(card.get("char") or "unknown", {
                "opportunities": 0, "cards": 0, "passes": 0,
                "hits": 0, "spent": 0, "payout": 0
            })
            char["opportunities"] += 1
            if card.get("action") == "pass":
                passes += 1
                char["passes"] += 1
                continue

            cards += 1
            hits += 1 if card.get("hit") else 0
            spent += int(card.get("spent") or 0)
            payout += int(card.get("payout") or 0)
            char["cards"] += 1
            char["hits"] += 1 if card.get("hit") else 0
            char["spent"] += int(card.get("spent") or 0)
            char["payout"] += int(card.get("payout") or 0)

    def finish(s: dict[str, int]) -> dict[str, Any]:
        return {
            **s,
            "balance": s["payout"] - s["spent"],
            "roi": round(s["payout"] / s["spent"], 4) if s["spent"] else None,
            "hit_rate": round(s["hits"] / s["cards"], 4) if s["cards"] else None,
        }

    return {
        "races": len(results),
        "opportunities": opportunities,
        "cards": cards,
        "passes": passes,
        "hits": hits,
        "spent": spent,
        "payout": payout,
        "balance": payout - spent,
        "roi": round(payout / spent, 4) if spent else None,
        "card_hit_rate": round(hits / cards, 4) if cards else None,
        "by_char": {k: finish(v) for k, v in by_char.items()},
    }



def _aggregate_by_regime(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """発走前に凍結したレジーム別に成績を分ける。後付けで結果から分類しない。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for race in results:
        # race_regime導入前のsnapshotは「unknown(判定不能)」と区別して数える。
        regime = race.get("race_regime")
        label = (regime or {}).get("label") or ("unrecorded" if regime is None else "unknown")
        buckets.setdefault(label, []).append(race)
    return {label: _aggregate(rows) for label, rows in sorted(buckets.items())}

def compare(champion: dict[str, Any],
            challengers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """
    ChampionとChallengerを**同じレースだけ**で比較する。

    Challenger導入前のChampion実績を混ぜると不公平なので、
    各challengerごとに race_id の共通部分(intersection)だけを集計する。
    """
    champion_map = {
        r["race_id"]: r for r in champion.get("results", [])
        if _eligible(r) and r.get("race_id")
    }

    comparisons = []
    for model_id, payload in challengers.items():
        challenger_map = {
            r["race_id"]: r for r in payload.get("results", [])
            if _eligible(r) and r.get("race_id")
        }
        common_ids = sorted(set(champion_map) & set(challenger_map))
        champ_rows = [champion_map[rid] for rid in common_ids]
        chall_rows = [challenger_map[rid] for rid in common_ids]

        head_to_head = []
        for rid in common_ids:
            c1, c2 = champion_map[rid], challenger_map[rid]
            c1_spent = sum(int(x.get("spent") or 0) for x in c1.get("cards", [])
                           if x.get("char") in MODEL_COMPARE_CHARS)
            c1_pay = sum(int(x.get("payout") or 0) for x in c1.get("cards", [])
                         if x.get("char") in MODEL_COMPARE_CHARS)
            c2_spent = sum(int(x.get("spent") or 0) for x in c2.get("cards", [])
                           if x.get("char") in MODEL_COMPARE_CHARS)
            c2_pay = sum(int(x.get("payout") or 0) for x in c2.get("cards", [])
                         if x.get("char") in MODEL_COMPARE_CHARS)
            c2_passes = [
                x.get("char") for x in c2.get("cards", [])
                if x.get("char") in MODEL_COMPARE_CHARS and x.get("action") == "pass"
            ]
            head_to_head.append({
                "race_id": rid,
                "champion_balance": c1_pay - c1_spent,
                "challenger_balance": c2_pay - c2_spent,
                "delta_challenger_minus_champion": (c2_pay - c2_spent) - (c1_pay - c1_spent),
                "challenger_passes": c2_passes,
                "race_regime": c2.get("race_regime"),
            })

        champ_stats = _aggregate(champ_rows)
        chall_stats = _aggregate(chall_rows)
        # ★ 欠けたレースを必ず表に出す。
        # 共通部分だけ集計すると、Challenger側がこけたレースが黙って比較から消え、
        # 「都合の良いレースだけで勝っている」状態に気づけない。
        missing_in_challenger = sorted(set(champion_map) - set(challenger_map))
        missing_in_champion = sorted(set(challenger_map) - set(champion_map))
        coverage = {
            "champion_races": len(champion_map),
            "challenger_races": len(challenger_map),
            "common_races": len(common_ids),
            "missing_in_challenger": missing_in_challenger,
            "missing_in_champion": missing_in_champion,
            "coverage_rate": (
                round(len(common_ids) / len(champion_map), 4) if champion_map else None
            ),
        }

        comparisons.append({
            "challenger_model_id": model_id,
            "coverage": coverage,
            "common_race_ids": common_ids,
            "common_races": len(common_ids),
            "champion": champ_stats,
            "challenger": chall_stats,
            "champion_by_regime": _aggregate_by_regime(champ_rows),
            "challenger_by_regime": _aggregate_by_regime(chall_rows),
            "delta": {
                "balance": chall_stats["balance"] - champ_stats["balance"],
                "roi": (
                    round(chall_stats["roi"] - champ_stats["roi"], 4)
                    if chall_stats["roi"] is not None and champ_stats["roi"] is not None
                    else None
                ),
                "card_hit_rate": (
                    round(chall_stats["card_hit_rate"] - champ_stats["card_hit_rate"], 4)
                    if chall_stats["card_hit_rate"] is not None and champ_stats["card_hit_rate"] is not None
                    else None
                ),
            },
            "head_to_head": head_to_head,
        })

    return {
        "generated_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
        "champion_model_id": next(
            (r.get("model_id") for r in champion_map.values() if r.get("model_id")),
            None,
        ),
        "comparisons": comparisons,
    }
