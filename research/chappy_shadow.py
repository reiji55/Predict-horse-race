"""Chappy shadow portfolio（chappy-shadow-v1）。

**現行の自動Chappy（logic/chappy.py）は一切変更しない。** 発走前snapshotに凍結された
`chappy_decision`（signal_board と roles）だけを入力に、同じ9点patternへ別の4頭を割り当てた
shadow案を作り、同じレースの結果で現行と並べて採点する。

切り分けたいこと（監査レポート §4）:
    現行          … 6シグナル統合 → 役割スコアで重複なし4頭 → 9点
    integrated案  … 6シグナル統合 → **統合スコアの上位4頭をそのまま役割順に割り当て** → 同じ9点

点構成・金額・シグナル統合は同一なので、差は「役割スコアでの4頭選抜（圧縮）」だけになる。
shadowは正式予想・正式成績・UIには混ぜない。
"""
from __future__ import annotations

from typing import Any

from logic import chappy
from results import build_results


def _auto_card(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    return next((c for c in snapshot.get("cards") or []
                 if c.get("char") in ("chappy", "otori")), None)


def current_portfolio(snapshot: dict[str, Any], chappy_config: dict[str, Any]) -> dict[str, Any] | None:
    """現行の自動Chappyの買い目。手動カードが採用されたレースでは roles から再構成する。"""
    decision = snapshot.get("chappy_decision") or {}
    roles = decision.get("roles")
    if not roles:
        return None
    card = _auto_card(snapshot)
    if card and card.get("source") == "signal_engine":
        bets = [{k: b[k] for k in ("type", "horses", "amt")} for b in card["bets"]]
        source = "snapshot_card"
    else:
        rebuilt = chappy.build_portfolio({k: {"num": v["num"]} for k, v in roles.items()}, chappy_config)
        bets = [{k: b[k] for k in ("type", "horses", "amt")} for b in rebuilt]
        source = "rebuilt_from_roles"
    return {
        "roles": {k: v["num"] for k, v in roles.items()},
        "bets": bets,
        "source": source,
    }


def integrated_portfolio(snapshot: dict[str, Any], chappy_config: dict[str, Any],
                         variant: dict[str, Any]) -> dict[str, Any] | None:
    board = ((snapshot.get("chappy_decision") or {}).get("signal_board") or {}).get("horses") or []
    rows = [r for r in board if r.get("num") is not None and r.get(variant["rank_by"]) is not None]
    roles_in_order = variant["roles_in_order"]
    if len(rows) < len(roles_in_order):
        return None
    # 同点は馬番昇順で決める（決定的にするため）
    rows.sort(key=lambda r: (-float(r[variant["rank_by"]]), int(r["num"])))
    roles = {role: {"num": int(rows[i]["num"])} for i, role in enumerate(roles_in_order)}
    bets = chappy.build_portfolio(roles, chappy_config)
    return {
        "roles": {k: v["num"] for k, v in roles.items()},
        "ranked": [{"num": int(r["num"]), variant["rank_by"]: r[variant["rank_by"]]} for r in rows[:6]],
        "bets": [{k: b[k] for k in ("type", "horses", "amt")} for b in bets],
        "source": "signal_board",
    }


def build(snapshot: dict[str, Any], chappy_config: dict[str, Any],
          config: dict[str, Any]) -> dict[str, Any]:
    """発走前に作る shadow 案一式。"""
    variants: dict[str, Any] = {}
    current = current_portfolio(snapshot, chappy_config)
    if current:
        variants["current_auto"] = current
    for name, variant in (config["chappy_shadow"].get("variants") or {}).items():
        built = integrated_portfolio(snapshot, chappy_config, variant)
        if built:
            variants[name] = built
    for item in variants.values():
        total = sum(b["amt"] for b in item["bets"])
        anchor = item["roles"].get("win_anchor")
        item["horses"] = sorted({n for b in item["bets"] for n in b["horses"]})
        item["total"] = total
        item["anchor_stake_share"] = (
            round(sum(b["amt"] for b in item["bets"] if anchor in b["horses"]) / total, 6)
            if total else None
        )
    return {"chappy_version": chappy_config.get("version"), "variants": variants}


def evaluate(built: dict[str, Any], finish: list[int], dividends: dict[str, Any]) -> dict[str, Any]:
    top3 = {int(n) for n in (finish or [])[:3]}
    out: dict[str, Any] = {}
    base_horses = set((built.get("variants", {}).get("current_auto") or {}).get("horses") or [])
    for name, item in (built.get("variants") or {}).items():
        settled = [build_results.settle_bet(b, dividends) for b in item["bets"]]
        spent = sum(b["amt"] for b in settled)
        payout = sum(b["payout"] for b in settled)
        out[name] = {
            "roles": item["roles"],
            "horses": item["horses"],
            "anchor_stake_share": item.get("anchor_stake_share"),
            "spent": spent,
            "payout": payout,
            "hit": payout > 0,
            "hit_bets": sum(1 for b in settled if b["hit"]),
            "top3_capture": len(top3 & set(item["horses"])),
            "overlap_with_current": (len(base_horses & set(item["horses"])) if base_horses else None),
            "bets": settled,
        }
    return out
