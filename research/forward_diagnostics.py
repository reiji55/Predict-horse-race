"""Forward diagnostics（forward-diagnostics-v1）。結果確定後の失敗段階監査。observe-only。

仕様: docs/audit/FORWARD_DIAGNOSTICS_SPEC_20260927.md

外れたあとに目視で原因を探すのではなく、発走前の証拠と確定着順から次を機械的に記録する。

    1. 固定3人が本当に別の仮説を見ているか（疑似コンセンサス・軸への依存額）
    2. 馬は拾えていたのに買い目化で落としたか（conversion loss）
    3. Top4 圧縮で落ちたのか、Top6 にもいなかったのか（recall@4 / recall@6）
    4. speed / 入力品質が設計どおりだったか
    5. Chappy の integrated → role → ticket のどの段階で落ちたか

入力（すべて読むだけ）:
    data/snapshots/{race_id}.json          … 発走前に凍結された Champion 予想
    data/shadow/prerace/{race_id}/*.json   … 発走前の shadow 記録（snapshot SHA-256 付き）
    data/race_results.json                 … 確定着順（finish[:3] だけを使う）

出力（研究専用。data/results.json・predictions.json・UIには混ぜない）:
    data/shadow/diagnostics/{race_id}.json … レース単位
    data/shadow/diagnostics_summary.json   … 累積（forward / retrospective を分ける）

発走前証拠の拘束（fail-closed）:
    採点時の snapshot と SHA-256 が一致し、発走前に取られた prerace record があるレースだけを診断する。
    無ければ status=unavailable とし、snapshot や結果から選定順を再構築しない。
    prerace record の fidelity が false のときは、キャラ別選定順（*_sel）だけを unavailable にする
    （research/prerace_capture.py の既存ルール）。

このモジュールは Champion の予想経路（logic/・scraper/）から読まれない。結果は予想の入力にならない。
"""
from __future__ import annotations

import argparse
import datetime
import itertools
import json
import logging
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from logic import cards, snapshots
from research import common, diversity

logger = logging.getLogger("research.forward_diagnostics")

SCHEMA = "forward-diagnostic-race-v1"
SUMMARY_SCHEMA = "forward-diagnostics-summary-v1"
CONFIG_PATH = common.ROOT / "config" / "forward_diagnostics.json"
DIAGNOSTICS_DIR = common.SHADOW_DIR / "diagnostics"
SUMMARY_PATH = common.SHADOW_DIR / "diagnostics_summary.json"

# 3連複は「直接の2頭馬券」に数えない（2頭とも選んでいたのに、その2頭だけで成立する券が無い、を測るため）
DIRECT_PAIR_TYPES = (cards.WIDE, cards.UMAREN)
TRIO_TYPE = cards.SANRENPUKU
AUTO_CHAPPY_CHARS = ("chappy", "otori")

SPEED_KEYS = ("raw_available_horses", "qualified_horses", "total_horses", "coverage", "used", "reason")

# 失敗段階ラベル（後方監査用。単一原因とは限らないので配列で持つ）。並びは出力順。
LABELS = (
    "feature_miss",             # どの順位でも seen_k 位以内に入っていない（カード単位では、そのカードの上流の順位で）
    "base_rank_cut",            # base 順位が cut_k より下・seen_k 以内（Top6 にはいたが Top4 圧縮で外れた位置）
    "top3_rank_cut",            # Top3 順位が cut_k より下・seen_k 以内
    "character_selection_cut",  # 固定3人カード: 上流（base/top3/自分の sel）で seen_k 以内なのに買い目に不在
    "chappy_integration_cut",   # Chappy: base/top3 で seen_k 以内なのに integrated では seen_k 圏外、role にも不在
    "chappy_role_cut",          # Chappy: integrated で seen_k 以内なのに role に不在
    "ticket_conversion_loss",   # 3着内の2頭（または3頭）を選んでいたのに、その組の直接馬券（または3連複）が無い
)
NO_MISS = "no_structural_miss_detected"


# ------------------------------------------------------------------ 共通

def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as f:
        return json.load(f)


def _rel(path: Path | None) -> str | None:
    if path is None:
        return None
    return str(path.relative_to(common.ROOT)) if path.is_relative_to(common.ROOT) else str(path)


def _available(order: list[int], source: str) -> dict[str, Any]:
    return {"status": "available", "source": source, "order": order}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def _ordered_labels(labels: set[str]) -> list[str]:
    ordered = [label for label in LABELS if label in labels]
    return ordered or [NO_MISS]


def _is_pass(card: dict[str, Any]) -> bool:
    return card.get("action") == "pass" or not card.get("bets")


def _card(snapshot: dict[str, Any], char: str) -> dict[str, Any] | None:
    return next((c for c in snapshot.get("cards") or [] if c.get("char") == char), None)


def _auto_chappy_card(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    return next((c for c in snapshot.get("cards") or [] if c.get("char") in AUTO_CHAPPY_CHARS), None)


def _bets(card: dict[str, Any]) -> list[tuple[str, tuple[int, ...], int]]:
    """(券種, 昇順の馬番タプル, 金額)。同じ組の重複はここでは残し、集合化は呼び出し側で行う。"""
    return [(b.get("type"), tuple(sorted(int(n) for n in b["horses"])), int(b.get("amt") or 0))
            for b in card.get("bets") or [] if b.get("horses")]


def _pairs(nums: list[int]) -> list[tuple[int, int]]:
    return [tuple(p) for p in itertools.combinations(sorted(nums), 2)]  # type: ignore[misc]


def _within(order: dict[str, Any] | None, num: int, k: int) -> bool:
    return bool(order) and order.get("status") == "available" and num in order["order"][:k]


def _rank(order: dict[str, Any] | None, num: int) -> int | None:
    if not order or order.get("status") != "available" or num not in order["order"]:
        return None
    return order["order"].index(num) + 1


# ------------------------------------------------------------------ 発走前証拠

def matched_prerace_record(race_id: str, post: datetime.datetime, snapshot_sha: str,
                           prerace_dir: Path) -> tuple[dict[str, Any] | None, Path | None, str]:
    """採点対象 snapshot と SHA-256 が一致する、発走前の最新 record。無ければ (None, None, 理由)。"""
    race_dir = prerace_dir / race_id
    matched: list[tuple[datetime.datetime, Path, dict[str, Any]]] = []
    any_prerace = False
    for path in sorted(race_dir.glob("*.json")) if race_dir.is_dir() else []:
        row = common.read_json(path)
        if not isinstance(row, dict) or row.get("pre_race") is not True or row.get("race_id") != race_id:
            continue
        captured = common.parse_dt(row.get("captured_at"))
        if captured is None or captured >= post:
            continue
        any_prerace = True
        if ((row.get("provenance") or {}).get("snapshot") or {}).get("sha256") == snapshot_sha:
            matched.append((captured, path, row))
    if matched:
        _, path, row = max(matched, key=lambda t: t[0])
        return row, path, "prerace_record_matched"
    return None, None, ("prerace_record_unmatched" if any_prerace else "no_prerace_record")


# ------------------------------------------------------------------ 発走前の診断

def candidate_orders(snapshot: dict[str, Any], record: dict[str, Any],
                     cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """発走前に存在した順位だけを並べる（再計算しない）。"""
    marks = [m for m in snapshot.get("marks") or [] if m.get("num") is not None]
    out: dict[str, dict[str, Any]] = {}

    out["base"] = (_available([int(m["num"]) for m in marks], "snapshot.marks")
                   if marks else _unavailable("no_marks"))
    ranked = sorted((m for m in marks if m.get("top3_rank") is not None),
                    key=lambda m: (m["top3_rank"], int(m["num"])))
    out["top3"] = (_available([int(m["num"]) for m in ranked], "snapshot.marks.top3_rank")
                   if ranked else _unavailable("no_top3_rank"))

    fidelity_ok = (record.get("fidelity") or {}).get("recomputed_matches_snapshot") is True
    sel_orders = (record.get("diversity") or {}).get("sel_orders") or {}
    for char in cfg["fixed_three"]:
        key = f"{char}_sel"
        card = _card(snapshot, char)
        if not fidelity_ok:
            out[key] = _unavailable("prerace_fidelity_false")
        elif not sel_orders.get(char):
            out[key] = _unavailable("sel_order_not_recorded")
        else:
            order = [int(r["num"]) for r in sel_orders[char]]
            axis = int(card["bets"][0]["horses"][0]) if card and not _is_pass(card) else None
            # 本番カードの軸（テンプレの0番目）と選定順の先頭が食い違うなら、その順位は信用しない
            out[key] = (_unavailable("sel_order_axis_mismatch") if axis is not None and order[0] != axis
                        else _available(order, "prerace_record.diversity.sel_orders"))

    board = ((snapshot.get("chappy_decision") or {}).get("signal_board") or {}).get("horses") or []
    rows = [r for r in board if r.get("num") is not None and r.get("integrated") is not None]
    rows.sort(key=lambda r: (-float(r["integrated"]), int(r["num"])))  # research/chappy_shadow.py と同じ並べ方
    out["chappy_integrated"] = (_available([int(r["num"]) for r in rows],
                                           "snapshot.chappy_decision.signal_board.integrated")
                                if rows else _unavailable("no_signal_board"))
    return out


def fixed_three(snapshot: dict[str, Any], orders: dict[str, dict[str, Any]],
                cfg: dict[str, Any]) -> dict[str, Any]:
    """固定3人の疑似コンセンサス。

    axis: 各カードのテンプレ0番目の馬（= 選定順の先頭。全テンプレの先頭 bet の先頭馬）。
    dominant_axis: 最も多くのキャラが軸にした馬。同数なら依存額が大きい方、さらに同じなら馬番の小さい方。
    dominant_axis_exposure: dominant_axis を含む bet の購入額の合計 / 固定3人の総購入額。
        3連複100円も軸を含めば100円依存として数える（「その馬に賭けた額」ではなく「その馬が必要な額」）。
    PASS カードは購入額・分母に入れない。
    """
    chars = cfg["fixed_three"]
    active: dict[str, dict[str, Any]] = {}
    passed, missing = [], []
    for char in chars:
        card = _card(snapshot, char)
        if card is None:
            missing.append(char)
        elif _is_pass(card):
            passed.append(char)
        else:
            active[char] = card

    axes = {c: int(card["bets"][0]["horses"][0]) for c, card in active.items()}
    bets = [b for card in active.values() for b in _bets(card)]
    total = sum(b[2] for b in bets)

    def exposure(num: int) -> int:
        return sum(amt for _, horses, amt in bets if num in horses)

    counts = Counter(axes.values())
    dominant = min(counts, key=lambda n: (-counts[n], -exposure(n), n)) if counts else None
    dominant_yen = exposure(dominant) if dominant is not None else 0

    horse_sets = {c: sorted({n for _, horses, _ in _bets(card) for n in horses}) for c, card in active.items()}
    flag_cfg = cfg["redundancy_flag"]
    spearman: dict[str, float | None] = {}
    jaccard: dict[str, float | None] = {}
    redundant: list[str] = []
    for a, b in itertools.combinations([c for c in chars if c in active], 2):
        key = f"{a}-{b}"
        oa, ob = orders.get(f"{a}_sel") or {}, orders.get(f"{b}_sel") or {}
        spearman[key] = (diversity._spearman(oa["order"], ob["order"])
                         if oa.get("status") == "available" and ob.get("status") == "available" else None)
        jaccard[key] = diversity.jaccard(horse_sets[a], horse_sets[b])
        if ((axes[a] == axes[b] or not flag_cfg["require_axis_match"])
                and jaccard[key] is not None and jaccard[key] >= flag_cfg["min_jaccard"]):
            redundant.append(key)

    return {
        "active_chars": [c for c in chars if c in active],
        "passed_chars": passed,
        "missing_chars": missing,
        "axis_source": "ticket_template_index0",
        "axes": axes,
        "axis_counts": {str(n): counts[n] for n in sorted(counts)},
        "max_axis_agreement": max(counts.values()) if counts else 0,
        "axis_all_same": len(active) == len(chars) and len(counts) == 1,
        "dominant_axis": dominant,
        "dominant_axis_exposure_yen": dominant_yen,
        "total_stake_yen": total,
        "dominant_axis_exposure_ratio": round(dominant_yen / total, 4) if total else None,
        "pairwise_rank_spearman": spearman,
        "bet_horse_jaccard": jaccard,
        "redundant_pairs": redundant,
        "redundancy_flag": bool(redundant),
    }


def input_quality(snapshot: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """snapshot の speed_quality を要約参照し、設計どおりに動いたかを versioned に判定する。"""
    sq = snapshot.get("speed_quality")
    marks = snapshot.get("marks") or []
    checks = {
        "speed_used": isinstance(sq, dict) and sq.get("used") is True,
        "win_odds_complete": bool(marks) and all(
            isinstance(m.get("odds"), (int, float)) and m["odds"] > 0 for m in marks),
        "scores_complete": bool(marks) and all(m.get("score") is not None for m in marks),
    }
    design = cfg["design_check"]
    return {
        "speed_quality": {k: sq.get(k) for k in SPEED_KEYS} if isinstance(sq, dict) else None,
        "speed_quality_status": "available" if isinstance(sq, dict) else "missing",
        "speed_degraded": not checks["speed_used"],
        "model_running_as_designed": all(checks[k] for k in design["require"]),
        "design_check": {"version": design["version"], "required": list(design["require"]), "checks": checks},
    }


# ------------------------------------------------------------------ 結果確定後の診断（監査専用）

def recall(orders: dict[str, dict[str, Any]], top3: list[int], cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    actual = set(top3)
    for source in cfg["rank_sources"]:
        order = orders.get(source) or _unavailable("unknown_source")
        if order["status"] != "available":
            out[source] = {"status": "unavailable", "reason": order.get("reason")}
            continue
        row: dict[str, Any] = {"status": "available", "denominator": len(top3)}
        for k in cfg["recall_k"]:
            count = len(set(order["order"][:k]) & actual)
            row[f"at{k}"] = round(count / len(top3), 4)
            row[f"count{k}"] = count
        out[source] = row
    return out


def winnable_pair_types(pair: tuple[int, int], top3: list[int]) -> list[str]:
    """確定着順でこの2頭の組が成立する直接馬券の券種。

    ワイド … 3着内の2頭ならどの組でも成立
    馬連   … 1着-2着の組だけ成立
    （3着内に2頭とも入っていない組は、どの券種でも成立しない）
    """
    if not set(pair) <= set(top3):
        return []
    types = [cards.WIDE]
    if set(pair) == set(top3[:2]):
        types.append(cards.UMAREN)
    return types


def _direct_types_by_pair(bets: list[tuple[str, tuple[int, ...], int]]) -> dict[tuple[int, int], set[str]]:
    out: dict[tuple[int, int], set[str]] = {}
    for bet_type, horses, _ in bets:
        if bet_type in DIRECT_PAIR_TYPES and len(horses) == 2:
            out.setdefault(horses, set()).add(bet_type)   # type: ignore[arg-type]
    return out


def conversion(card: dict[str, Any], top3: list[int]) -> dict[str, Any]:
    """選定（買い目に登場した馬）と、直接の2頭馬券・3連複への変換。

    direct_pair_coverage_ratio は券種をまたいだ構造上の組カバー率（成立するかは問わない）。
    conversion loss は券種ごとの成立条件で判定する: 3着内の2頭を選んでいたのに、
    その組が成立する券種（ワイド、1着-2着なら馬連も）の馬券を1枚も持っていなければ loss。
    例: 1着A・3着C を選び、馬連A-C だけ持っている → 馬連は成立しないので loss。
    """
    bets = _bets(card)
    selected = sorted({n for _, horses, _ in bets for n in horses})
    selected_top3 = [n for n in top3 if n in selected]           # 着順どおり
    available_pairs = _pairs(selected)
    types_by_pair = _direct_types_by_pair(bets)                  # 同じ組・同じ券種の重複は1つ
    ticketed = sorted(types_by_pair)
    top3_pairs = _pairs(selected_top3)
    pair_rows = []
    for pair in top3_pairs:
        winnable = winnable_pair_types(pair, top3)
        held = sorted(types_by_pair.get(pair, set()))
        pair_rows.append({"pair": list(pair), "winnable_types": winnable, "ticketed_types": held,
                          "converted": any(t in winnable for t in held)})
    converted = [r["pair"] for r in pair_rows if r["converted"]]
    not_converted = [r["pair"] for r in pair_rows if not r["converted"]]
    umaren_pair = tuple(sorted(top3[:2]))
    umaren_selected = set(umaren_pair) <= set(selected)
    trios = {horses for bet_type, horses, _ in bets if bet_type == TRIO_TYPE and len(horses) == 3}
    all_selected = len(selected_top3) == 3
    trio_ticketed = tuple(sorted(top3)) in trios
    return {
        "status": "evaluated",
        "stake_yen": sum(amt for _, _, amt in bets),
        "selected_horses": selected,
        "actual_top3_selected": selected_top3,
        "actual_top3_selected_count": len(selected_top3),
        "direct_pairs_available": [list(p) for p in available_pairs],
        "direct_pairs_available_count": len(available_pairs),
        "direct_pairs_ticketed": [list(p) for p in ticketed],
        "direct_pairs_ticketed_count": len(ticketed),
        "direct_pair_coverage_ratio": round(len(ticketed) / len(available_pairs), 4) if available_pairs else None,
        "actual_top3_pairs_selected": [list(p) for p in top3_pairs],
        "actual_top3_pair_conversion": pair_rows,
        # 成立する券種の馬券を持っていた組 / 持っていなかった組
        "actual_top3_pairs_directly_ticketed": converted,
        "actual_top3_pairs_not_directly_ticketed": not_converted,
        "winning_pair_present_but_not_ticketed": bool(not_converted),
        "umaren_winning_pair": list(umaren_pair),
        "umaren_winning_pair_selected": umaren_selected,
        "umaren_winning_pair_ticketed": cards.UMAREN in types_by_pair.get(umaren_pair, set()),
        "all_top3_selected": all_selected,
        "winning_trio_ticketed": trio_ticketed,
        "winning_trio_selected_but_not_ticketed": all_selected and not trio_ticketed,
    }


def _fixed_card_labels(char: str, conv: dict[str, Any], top3: list[int],
                       orders: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """固定3人カード。上流 = base / top3 / 自分の sel（使えるものだけ）。

    3着内の馬がカードに不在のとき:
        上流のどれかで seen_k 位以内 → character_selection_cut
        どれにも入っていない         → feature_miss
    """
    upstream = [s for s in ("base", "top3", f"{char}_sel") if (orders.get(s) or {}).get("status") == "available"]
    labels: set[str] = set()
    for num in top3:
        if num in conv["selected_horses"]:
            continue
        seen = any(_within(orders.get(s), num, cfg["seen_k"]) for s in upstream)
        labels.add("character_selection_cut" if seen else "feature_miss")
    if conv["winning_pair_present_but_not_ticketed"] or conv["winning_trio_selected_but_not_ticketed"]:
        labels.add("ticket_conversion_loss")
    return _ordered_labels(labels), upstream


def _chappy_card_labels(conv: dict[str, Any], roles: set[int], top3: list[int],
                        orders: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> list[str]:
    """自動Chappyカード。3着内の馬が買い目に不在のとき、どの段階で消えたかを1つ選ぶ:

        role にいた                                  → ticket_conversion_loss
        integrated で seen_k 位以内                  → chappy_role_cut
        base / top3 で seen_k 位以内                 → chappy_integration_cut
        どれにも入っていない                         → feature_miss
    """
    seen_k = cfg["seen_k"]
    labels: set[str] = set()
    for num in top3:
        if num in conv["selected_horses"]:
            continue
        if num in roles:
            labels.add("ticket_conversion_loss")
        elif _within(orders.get("chappy_integrated"), num, seen_k):
            labels.add("chappy_role_cut")
        elif _within(orders.get("base"), num, seen_k) or _within(orders.get("top3"), num, seen_k):
            labels.add("chappy_integration_cut")
        else:
            labels.add("feature_miss")
    if conv["winning_pair_present_but_not_ticketed"] or conv["winning_trio_selected_but_not_ticketed"]:
        labels.add("ticket_conversion_loss")
    return _ordered_labels(labels)


def _horse_labels(num: int, orders: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> list[str]:
    """レース全体の順位で見た、3着内の馬1頭のラベル（カードとは独立）。"""
    cut_k, seen_k = cfg["cut_k"], cfg["seen_k"]
    labels: set[str] = set()
    if not any(_within(orders.get(s), num, seen_k) for s in cfg["rank_sources"]):
        labels.add("feature_miss")
    for source, label in (("base", "base_rank_cut"), ("top3", "top3_rank_cut")):
        rank = _rank(orders.get(source), num)
        if rank is not None and cut_k < rank <= seen_k:
            labels.add(label)
    return [label for label in LABELS if label in labels]


def _roles(snapshot: dict[str, Any]) -> dict[str, int]:
    roles = (snapshot.get("chappy_decision") or {}).get("roles") or {}
    return {role: int(v["num"]) for role, v in roles.items() if isinstance(v, dict) and v.get("num") is not None}


def chappy_stage(snapshot: dict[str, Any], orders: dict[str, dict[str, Any]],
                 top3: list[int], cfg: dict[str, Any]) -> dict[str, Any]:
    """integrated → role → ticket の段階遷移。role を悪者と決めつけず、遷移だけを記録する。"""
    card = _auto_chappy_card(snapshot)
    if card is None or _is_pass(card):
        return _unavailable("no_auto_chappy_card")
    if card.get("source") != "signal_engine":
        return _unavailable("non_signal_engine_card")
    roles = _roles(snapshot)
    if not roles:
        return _unavailable("no_roles")
    integrated = orders.get("chappy_integrated") or {}
    if integrated.get("status") != "available":
        return _unavailable("integrated_order_unavailable")

    cut_k, seen_k = cfg["cut_k"], cfg["seen_k"]
    order = integrated["order"]
    actual = set(top3)
    role_set = set(roles.values())
    bets = _bets(card)
    ticket_horses = {n for _, horses, _ in bets for n in horses}
    types_by_pair = _direct_types_by_pair(bets)
    direct = set(types_by_pair)
    trios = {horses for bet_type, horses, _ in bets if bet_type == TRIO_TYPE and len(horses) == 3}

    def converted(pair: tuple[int, int]) -> bool:
        return any(t in winnable_pair_types(pair, top3) for t in types_by_pair.get(pair, set()))

    role_pairs = _pairs(sorted(role_set))
    role_trios = [tuple(t) for t in itertools.combinations(sorted(role_set), 3)]
    usage = []
    for role, num in roles.items():
        pairs = [p for p in role_pairs if num in p]
        trio_list = [t for t in role_trios if num in t]
        usage.append({
            "role": role, "num": num, "actual_top3": num in actual,
            "direct_pairs_with_roles": len(pairs),
            "direct_pairs_ticketed": sum(1 for p in pairs if p in direct),
            "trios_with_roles": len(trio_list),
            "trios_ticketed": sum(1 for t in trio_list if t in trios),
        })
    top3_role_pairs = [p for p in role_pairs if set(p) <= actual]

    def in_upstream(num: int) -> bool:
        return _within(orders.get("base"), num, seen_k) or _within(orders.get("top3"), num, seen_k)

    return {
        "status": "evaluated",
        "roles": roles,
        "integrated_recall": {
            f"at{k}": round(len(set(order[:k]) & actual) / len(top3), 4) for k in cfg["recall_k"]
        } | {f"count{k}": len(set(order[:k]) & actual) for k in cfg["recall_k"]},
        "actual_top3_counts": {
            f"integrated_top{seen_k}": len(set(order[:seen_k]) & actual),
            f"integrated_top{cut_k}": len(set(order[:cut_k]) & actual),
            "roles": len(role_set & actual),
            "tickets": len(ticket_horses & actual),
        },
        f"integrated_top{cut_k}_not_in_roles": [
            {"num": n, "integrated_rank": i + 1, "actual_top3": n in actual}
            for i, n in enumerate(order[:cut_k]) if n not in role_set],
        f"roles_outside_integrated_top{cut_k}": [
            {"role": role, "num": n, "integrated_rank": (order.index(n) + 1 if n in order else None),
             "actual_top3": n in actual}
            for role, n in roles.items() if n not in order[:cut_k]],
        "role_ticket_usage": usage,
        "role_horses_not_fully_used": [u["num"] for u in usage
                                       if u["direct_pairs_ticketed"] < u["direct_pairs_with_roles"]
                                       or u["trios_ticketed"] < u["trios_with_roles"]],
        "actual_top3_role_pairs": [list(p) for p in top3_role_pairs],
        "actual_top3_role_pairs_not_directly_ticketed": [list(p) for p in top3_role_pairs if not converted(p)],
        "actual_top3_drops": {
            # 3着内の馬が、どの遷移で落ちた／拾われたか（頭数）
            "integration_cut": sum(1 for n in top3 if n not in role_set
                                   and not _within(integrated, n, seen_k) and in_upstream(n)),
            "role_cut": sum(1 for n in top3 if n not in role_set and _within(integrated, n, seen_k)),
            f"role_added_outside_integrated_top{seen_k}": sum(1 for n in top3 if n in role_set
                                                     and not _within(integrated, n, seen_k)),
            "role_to_ticket_pairs_missing": sum(1 for p in top3_role_pairs if not converted(p)),
        },
    }


# ------------------------------------------------------------------ レース単位

def diagnose_race(snapshot: dict[str, Any], snapshot_path: Path, result: dict[str, Any],
                  prerace_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    race_id = snapshot["race_id"]
    post = common.post_at(race_id, snapshot.get("post_time"))
    snapshot_sha = common.sha256_file(snapshot_path)
    top3 = [int(n) for n in (result.get("finish") or [])[:3]]
    record, record_path, evidence_status = matched_prerace_record(race_id, post, snapshot_sha, prerace_dir)

    head = {
        "schema": SCHEMA,
        "diagnostics_version": cfg.get("version"),
        "diagnostics_config_hash": common.config_hash(cfg),
        "race_id": race_id,
        # diagnostics 自身の事前登録日時で分ける（ラベル定義は 9/27 の結果を見た後に作ったため、
        # shadow_research.json の registered_at を流用すると 9/27 が forward に入ってしまう）
        "phase": common.phase_of(post, cfg),
        "registered_at": cfg.get("registered_at"),
        "official_results_untouched": True,
        "used_for_prediction": False,
        "evidence": {
            "status": evidence_status,
            "snapshot_path": _rel(snapshot_path),
            "snapshot_sha256": snapshot_sha,
            "snapshot_frozen_at": snapshot.get("frozen_at"),
            "prerace_record_path": _rel(record_path),
            "prerace_captured_at": (record or {}).get("captured_at"),
            "prerace_minutes_to_post": (record or {}).get("minutes_to_post"),
            "post_at": post.isoformat() if post else None,
        },
        "actual_top3": top3,
    }
    frozen = common.parse_dt(snapshot.get("frozen_at"))
    reason = None
    if frozen is None or frozen >= post:
        reason = "snapshot_not_frozen_before_post"
    elif record is None:
        reason = evidence_status
    if reason:
        return {**head, "status": "unavailable", "reason": reason,
                "pre_race": {"status": "unavailable", "reason": reason},
                "post_race": {"status": "unavailable", "reason": reason}}

    orders = candidate_orders(snapshot, record, cfg)
    auto_card = _auto_chappy_card(snapshot)
    roles = _roles(snapshot)

    # ---- 結果確定後（監査専用） ----
    card_rows: dict[str, Any] = {}
    card_labels: dict[str, list[str]] = {}
    for char in cfg["fixed_three"]:
        card = _card(snapshot, char)
        if card is None:
            continue
        if _is_pass(card):
            card_rows[char] = {"status": "pass"}
            continue
        conv = conversion(card, top3)
        labels, upstream = _fixed_card_labels(char, conv, top3, orders, cfg)
        conv["upstream_sources"] = upstream
        conv["failure_stage"] = labels
        card_rows[char] = conv
        card_labels[char] = labels
    if auto_card is not None:
        char = auto_card.get("char")
        if _is_pass(auto_card):
            card_rows[char] = {"status": "pass"}
        elif auto_card.get("source") != "signal_engine" or not roles \
                or (orders.get("chappy_integrated") or {}).get("status") != "available":
            card_rows[char] = {"status": "not_evaluated", "reason": "non_signal_engine_card_or_no_integrated_order"}
        else:
            conv = conversion(auto_card, top3)
            conv["failure_stage"] = _chappy_card_labels(conv, set(roles.values()), top3, orders, cfg)
            card_rows[char] = conv
            card_labels[char] = conv["failure_stage"]

    horses = []
    race_labels: set[str] = set()
    for pos, num in enumerate(top3, start=1):
        labels = _horse_labels(num, orders, cfg)
        race_labels.update(labels)
        horses.append({
            "num": num, "finish_position": pos,
            "ranks": {s: _rank(orders.get(s), num) for s in cfg["rank_sources"]
                      if (orders.get(s) or {}).get("status") == "available"},
            "ticketed_by": [c for c, row in card_rows.items()
                            if row.get("status") == "evaluated" and num in row["selected_horses"]],
            "in_chappy_roles": num in set(roles.values()),
            "labels": labels,
        })
    for labels in card_labels.values():
        race_labels.update(label for label in labels if label != NO_MISS)

    return {
        **head,
        "status": "available",
        "pre_race": {
            "status": "available",
            "fidelity_recomputed_matches_snapshot":
                (record.get("fidelity") or {}).get("recomputed_matches_snapshot") is True,
            "fixed_three": fixed_three(snapshot, orders, cfg),
            "input_quality": input_quality(snapshot, cfg),
            "candidate_orders": {
                **orders,
                "chappy_roles": roles,
                "ticket_horses": {c.get("char"): sorted({n for _, h, _ in _bets(c) for n in h})
                                  for c in snapshot.get("cards") or [] if not _is_pass(c)},
            },
        },
        "post_race": {
            "status": "available",
            "note": "確定着順を使う監査専用の値。予想・選定・買い目の入力にはしない。",
            "recall": recall(orders, top3, cfg),
            "cards": card_rows,
            "chappy_stage": chappy_stage(snapshot, orders, top3, cfg),
            "failure_stage": {
                "thresholds": {"cut_k": cfg["cut_k"], "seen_k": cfg["seen_k"]},
                "race": _ordered_labels(race_labels),
                "by_card": card_labels,
                "by_horse": horses,
            },
        },
    }


# ------------------------------------------------------------------ 累積

def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def summarize(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    available = [r for r in rows if r.get("status") == "available"]
    pre = [r["pre_race"] for r in available]
    post = [r["post_race"] for r in available]
    fixed = [p["fixed_three"] for p in pre]
    full = [f for f in fixed if not f["passed_chars"] and not f["missing_chars"]]
    speed_used = sum(1 for p in pre if p["input_quality"]["design_check"]["checks"]["speed_used"])

    pairs = sorted({k for f in fixed for k in f["pairwise_rank_spearman"]})
    spearman = {k: {"races": len(v), "mean": _mean(v)} for k in pairs
                for v in [[f["pairwise_rank_spearman"][k] for f in fixed
                           if f["pairwise_rank_spearman"].get(k) is not None]]}

    recall_sum: dict[str, Any] = {}
    for source in cfg["rank_sources"]:
        rows_s = [p["recall"][source] for p in post if p["recall"].get(source, {}).get("status") == "available"]
        recall_sum[source] = {"races": len(rows_s)} | {
            f"mean_at{k}": _mean([r[f"count{k}"] / r["denominator"] for r in rows_s]) for k in cfg["recall_k"]}

    chars = list(cfg["fixed_three"]) + [c for c in AUTO_CHAPPY_CHARS
                                         if any(c in p["cards"] for p in post)]
    by_char: dict[str, Any] = {}
    for char in chars:
        rows_c = [p["cards"][char] for p in post if char in p["cards"]]
        evaluated = [r for r in rows_c if r.get("status") == "evaluated"]
        by_char[char] = {
            "cards": len(evaluated),
            "passes": sum(1 for r in rows_c if r.get("status") == "pass"),
            "not_evaluated": sum(1 for r in rows_c if r.get("status") == "not_evaluated"),
            "mean_direct_pair_coverage": _mean([r["direct_pair_coverage_ratio"] for r in evaluated
                                                if r["direct_pair_coverage_ratio"] is not None]),
            "conversion_loss_count": sum(1 for r in evaluated if "ticket_conversion_loss" in r["failure_stage"]),
            "winning_pair_present_but_not_ticketed": sum(1 for r in evaluated
                                                         if r["winning_pair_present_but_not_ticketed"]),
            "winning_trio_selected_but_not_ticketed": sum(1 for r in evaluated
                                                          if r["winning_trio_selected_but_not_ticketed"]),
        }

    stages = [p["chappy_stage"] for p in post if p["chappy_stage"].get("status") == "evaluated"]
    drop_keys = sorted({k for s in stages for k in s["actual_top3_drops"]})
    count_keys = sorted({k for s in stages for k in s["actual_top3_counts"]})
    chappy_sum = {
        "races": len(stages),
        "actual_top3_counts_total": {k: sum(s["actual_top3_counts"][k] for s in stages) for k in count_keys},
        "actual_top3_drops_total": {k: sum(s["actual_top3_drops"][k] for s in stages) for k in drop_keys},
    }

    label_counts = Counter(label for p in post for label in p["failure_stage"]["race"])
    return {
        "races": len(rows),
        "available_races": len(available),
        "unavailable": dict(sorted(Counter(r.get("reason") for r in rows
                                           if r.get("status") == "unavailable").items())),
        "fixed_three_full_races": len(full),
        "fixed_three_axis_all_same_count": sum(1 for f in full if f["axis_all_same"]),
        "fixed_three_axis_all_same_rate": (round(sum(1 for f in full if f["axis_all_same"]) / len(full), 4)
                                           if full else None),
        "mean_dominant_axis_exposure_ratio": _mean([f["dominant_axis_exposure_ratio"] for f in fixed
                                                    if f["dominant_axis_exposure_ratio"] is not None]),
        "pairwise_mean_spearman": spearman,
        "speed_used_races": speed_used,
        "speed_used_rate": round(speed_used / len(pre), 4) if pre else None,
        "recall": recall_sum,
        "by_char": by_char,
        "chappy_stage": chappy_sum,
        "race_failure_stage_counts": {label: label_counts[label] for label in (*LABELS, NO_MISS)
                                      if label_counts[label]},
    }


# ------------------------------------------------------------------ 実行

def run(race_results: dict[str, Any], snapshot_dir: Path | None = None, prerace_dir: Path | None = None,
        output_dir: Path | None = None, summary_path: Path | None = None,
        cfg: dict[str, Any] | None = None, now: datetime.datetime | None = None) -> dict[str, Any]:
    snapshot_dir = snapshot_dir or snapshots.SNAPSHOT_DIR
    prerace_dir = prerace_dir or common.PRERACE_DIR
    output_dir = output_dir or DIAGNOSTICS_DIR
    summary_path = summary_path or SUMMARY_PATH
    cfg = cfg or load_config()
    if common.parse_dt(cfg.get("registered_at")) is None:
        raise ValueError("config/forward_diagnostics.json に registered_at が必要です")

    targets = []
    for path in sorted(snapshot_dir.glob("*.json")):
        snap = common.read_json(path)
        if not isinstance(snap, dict) or not common.is_model_race(snap):
            continue
        result = race_results.get(snap.get("race_id")) or {}
        if len(result.get("finish") or []) < 3:
            continue
        post = common.post_at(snap["race_id"], snap.get("post_time"))
        if post is None:
            logger.warning("%s: 発走時刻が分からないため診断しません", snap.get("race_id"))
            continue
        targets.append((post, snap["race_id"], path, snap, result))
    targets.sort(key=lambda t: (t[0], t[1]))

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for post, race_id, path, snap, result in targets:
        try:
            row = diagnose_race(snap, path, result, prerace_dir, cfg)
        except Exception as exc:  # noqa: BLE001 — 1レースの失敗で全体を止めないが、黙らない
            logger.error("%s: diagnostics failed: %s: %s", race_id, type(exc).__name__, exc)
            errors.append({"race_id": race_id, "error": f"{type(exc).__name__}: {exc}"})
            # 古い診断を残さない（前回の値を今回の結果と誤読しないため）
            common.write_json(output_dir / f"{race_id}.json",
                              {"schema": SCHEMA, "race_id": race_id, "status": "error",
                               "error": f"{type(exc).__name__}: {exc}",
                               "official_results_untouched": True, "used_for_prediction": False})
            continue
        if row["status"] == "unavailable":
            logger.warning("%s: diagnostics unavailable (%s)", race_id, row["reason"])
        common.write_json(output_dir / f"{race_id}.json", row)
        rows.append(row)

    summary = {
        "schema": SUMMARY_SCHEMA,
        "generated_at": (now or datetime.datetime.now(common.JST)).isoformat(timespec="seconds"),
        "diagnostics_version": cfg.get("version"),
        "diagnostics_config_hash": common.config_hash(cfg),
        "spec": cfg.get("spec"),
        "registered_at": cfg.get("registered_at"),
        "official_results_untouched": True,
        "used_for_prediction": False,
        "note": "失敗段階の観測記録。少数標本なので良し悪しの判定には使わない。"
                "forward（事前登録後）と retrospective を分ける。予想・選定・買い目・重みには使わない。",
        "forward": summarize([r for r in rows if r["phase"] == "forward"], cfg),
        "retrospective": summarize([r for r in rows if r["phase"] != "forward"], cfg),
        "errors": errors,
    }
    common.write_json(summary_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="結果確定後の forward diagnostics（observe-only）")
    parser.add_argument("--results", default=str(common.ROOT / "data" / "race_results.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    race_results = common.read_json(Path(args.results))
    if not isinstance(race_results, dict):
        logger.error("%s を読めないため diagnostics をスキップします", args.results)
        sys.exit(1)
    summary = run(race_results)
    print(json.dumps({k: {"races": summary[k]["races"], "available": summary[k]["available_races"]}
                      for k in ("forward", "retrospective")} | {"errors": len(summary["errors"])},
                     ensure_ascii=False))
    if summary["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
