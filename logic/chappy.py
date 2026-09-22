"""
Chappy 1000円統合判断レイヤー。

固定weightの「4人目キャラ」ではなく、Win / Top3 / 条件再現性 / 近況 /
市場価格 / value を同時に見て役割を決め、1000円をポートフォリオ化する。

重要:
- Top3 Scoreもwin pも未校正。ここで「真の確率」を主張しない。
- market EV は現段階では veto/診断用途。大きさを信用しすぎない。
- 強烈な同コース同距離の反復実績がある馬だけ、条件シグナルの重みを動的に上げる。
- ChatGPTが会話上で作った手動カードを data/chappy_manual/{race_id}.json に置けば、
  自動案より優先してそのカードを採用できる。
- 鳳は別モデルではなく Chappy の high-conviction state。gate通過時に置き換わる。
"""
from __future__ import annotations

import datetime
import json
import logging
import math
from pathlib import Path
from typing import Any

from logic import cards

logger = logging.getLogger("logic.chappy")

JST = datetime.timezone(datetime.timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "chappy.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as f:
        return json.load(f)


def _norm(values: list[float | None]) -> list[float]:
    usable = [float(v) for v in values if v is not None]
    if not usable:
        return [0.0 for _ in values]
    lo, hi = min(usable), max(usable)
    if hi <= lo:
        return [1.0 if v is not None else 0.0 for v in values]
    return [0.0 if v is None else (float(v) - lo) / (hi - lo) for v in values]


def _perf(run: dict[str, Any]) -> float | None:
    finish, heads = run.get("finish"), run.get("heads")
    if finish is None or heads is None or heads <= 0:
        return None
    return max(0.0, min(1.0, (heads - finish + 0.5) / heads))


def _rate(rows: list[dict[str, Any]]) -> tuple[float, int, int]:
    usable = [r for r in rows if r.get("finish") is not None]
    hits = sum(1 for r in usable if r["finish"] <= 3)
    return (hits / len(usable) if usable else 0.0, len(usable), hits)


def condition_profile(past_runs: list[dict[str, Any]], today: dict[str, Any]) -> dict[str, Any]:
    surface = today.get("surface")
    dist = today.get("dist")
    venue = today.get("venue")
    going = today.get("going")

    same_surface = [r for r in past_runs if r.get("surface") == surface]
    same_dist = [r for r in same_surface if dist is not None and r.get("dist") == dist]
    same_course_dist = [r for r in same_dist if r.get("venue") == venue]
    same_going = [r for r in same_surface if going and r.get("going") == going]

    d_rate, d_runs, d_hits = _rate(same_dist)
    cd_rate, cd_runs, cd_hits = _rate(same_course_dist)
    g_rate, g_runs, g_hits = _rate(same_going)

    parts: list[tuple[float, float]] = []
    if d_runs:
        parts.append((0.50, d_rate))
    if cd_runs:
        parts.append((0.35, cd_rate))
    if g_runs:
        parts.append((0.15, g_rate))
    denom = sum(w for w, _ in parts)
    score = sum(w * v for w, v in parts) / denom if denom else 0.0

    return {
        "score": round(score, 6),
        "same_dist_runs": d_runs,
        "same_dist_top3": d_hits,
        "same_dist_rate": round(d_rate, 6),
        "same_course_dist_runs": cd_runs,
        "same_course_dist_top3": cd_hits,
        "same_course_dist_rate": round(cd_rate, 6),
        "same_going_runs": g_runs,
        "same_going_top3": g_hits,
        "same_going_rate": round(g_rate, 6),
    }


def recent_profile(past_runs: list[dict[str, Any]], today: dict[str, Any]) -> dict[str, Any]:
    surface = today.get("surface")
    runs = [r for r in past_runs if r.get("surface") == surface][:3]
    if not runs:
        return {"score": 0.0, "runs": 0, "top3": 0}

    vals = []
    hits = 0
    recency = [1.0, 0.8, 0.6]
    for i, run in enumerate(runs):
        perf = _perf(run)
        if perf is None:
            continue
        hit = 1.0 if run.get("finish") is not None and run["finish"] <= 3 else 0.0
        hits += int(hit)
        vals.append((recency[i], 0.65 * hit + 0.35 * perf))
    denom = sum(w for w, _ in vals)
    score = sum(w * v for w, v in vals) / denom if denom else 0.0
    return {"score": round(score, 6), "runs": len(vals), "top3": hits}


def _weighted(signal: dict[str, float], weights: dict[str, float]) -> float:
    return sum(weights.get(k, 0.0) * signal.get(k, 0.0) for k in weights)


def build_signal_board(horses: list[dict[str, Any]], race: dict[str, Any],
                       config: dict[str, Any], speed_quality: dict[str, Any] | None) -> dict[str, Any]:
    course = race.get("course") or {}
    today = {
        "surface": course.get("surface"),
        "dist": course.get("dist"),
        "venue": race.get("venue"),
        "going": race.get("going"),
    }

    win_norm = _norm([h.get("score") for h in horses])
    top3_norm = _norm([h.get("top3_raw") for h in horses])
    value_norm = _norm([h.get("value") for h in horses])
    log_odds = [math.log(max(float(h.get("odds") or 1.0), 1.0)) if h.get("odds") else None for h in horses]
    market_norm = _norm(log_odds)

    rows = []
    boost_cfg = config["condition_boost"]
    base_weights = config["base_weights"]

    for i, horse in enumerate(horses):
        cond = condition_profile(horse.get("_past_runs") or [], today)
        recent = recent_profile(horse.get("_past_runs") or [], today)
        signals = {
            "win": win_norm[i],
            "top3": top3_norm[i],
            "condition": cond["score"],
            "recent": recent["score"],
            "market": market_norm[i],
            "value": value_norm[i],
        }
        weights = dict(base_weights)
        boosted = (
            cond["same_course_dist_runs"] >= boost_cfg["min_same_course_distance_runs"]
            and cond["same_course_dist_rate"] >= boost_cfg["min_top3_rate"]
        )
        if boosted:
            weights["condition"] += boost_cfg["add_condition"]
            weights["win"] = max(0.0, weights["win"] - boost_cfg["subtract_win"])
            weights["recent"] = max(0.0, weights["recent"] - boost_cfg["subtract_recent"])

        integrated = _weighted(signals, weights)
        top3_evidence = min(1.0, (horse.get("top3_n_usable") or 0) / 3.0)
        speed_cov = float((speed_quality or {}).get("coverage") or 0.0)
        data_quality = 0.65 * top3_evidence + 0.35 * speed_cov

        role_scores = {
            role: _weighted(signals, role_weights)
            for role, role_weights in config["role_weights"].items()
        }

        rows.append({
            "num": horse.get("num"),
            "name": horse.get("name"),
            "odds": horse.get("odds"),
            "signals": {k: round(v, 6) for k, v in signals.items()},
            "dynamic_weights": {k: round(v, 6) for k, v in weights.items()},
            "condition_boosted": boosted,
            "condition": cond,
            "recent": recent,
            "integrated": round(integrated, 6),
            "data_quality": round(data_quality, 6),
            "role_scores": {k: round(v, 6) for k, v in role_scores.items()},
        })

    return {"version": config["version"], "horses": rows}


def _pick(rows: list[dict[str, Any]], role: str, used: set[int]) -> dict[str, Any]:
    ordered = sorted(
        (r for r in rows if r.get("num") is not None and r["num"] not in used),
        key=lambda r: (r["role_scores"][role], r["integrated"]),
        reverse=True,
    )
    if not ordered:
        raise ValueError(f"Chappy role候補が不足しています: {role}")
    return ordered[0]


def choose_roles(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = board["horses"]
    used: set[int] = set()
    roles = {}
    for role in ("win_anchor", "support", "top3_edge", "long_edge"):
        row = _pick(rows, role, used)
        roles[role] = row
        used.add(row["num"])
    return roles


def _bet_from_pattern(item: list[Any], roles: dict[str, dict[str, Any]],
                      config: dict[str, Any]) -> dict[str, Any]:
    bet_type = item[0]
    role_names = item[1:-1]
    bucket = item[-1]
    horses = [roles[name]["num"] for name in role_names]
    amt = config["portfolio"]["core_wide"] if bucket == "core" and bet_type == cards.WIDE else config["portfolio"]["unit"]
    return {"type": bet_type, "horses": horses, "amt": amt, "bucket": bucket}


def build_portfolio(roles: dict[str, dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    bets = [_bet_from_pattern(item, roles, config) for item in config["portfolio"]["pattern"]]
    spent = sum(b["amt"] for b in bets)
    if spent != config["total"]:
        raise ValueError(f"Chappy portfolio total mismatch: {spent} != {config['total']}")
    return bets


def load_manual_override(race_id: str, config: dict[str, Any],
                         root: Path | None = None,
                         post_at: "datetime.datetime | None" = None) -> dict[str, Any] | None:
    """
    手動カードを読む。**発走前に作られたと機械的に確認できたものだけ**を採用する。

    docs/CHAPPY_MANUAL_OVERRIDE.md は「必ず発走前に作る」「発走後に過去レースへ
    追加しない」と運用ルールで書いているが、コード側に検査が無かった。
    運用ルールだけに頼ると、結果を見たあとに置かれたファイルを
    「発走前の予想」として採点してしまう余地が残る（2026-09-19 に
    発走後の再生成を採点した事故と同じ構造）。

    そこで機械的に閉じる：
      - `created_at` が無い / 読めない → 採用しない
      - `created_at` が発走時刻以降 → 採用しない
    どちらも例外にせず警告して「手動カードは無い」ものとして扱う（自動Chappyが出る）。
    発走時刻が判らない場合は判定できないので、**保守側に倒して採用しない**。
    """
    base = root or ROOT
    path = base / config["manual_override_dir"] / f"{race_id}.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        payload = json.load(f)
    if payload.get("race_id") != race_id:
        raise ValueError(f"Chappy manual override race_id mismatch: {path}")

    created_raw = payload.get("created_at")
    created_at = None
    if created_raw:
        try:
            created_at = datetime.datetime.fromisoformat(str(created_raw))
        except ValueError:
            created_at = None
    if created_at is None:
        logger.warning(
            "%s: 手動Chappyカードの created_at が無い/読めないため採用しません（%r）",
            race_id, created_raw,
        )
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=JST)

    if post_at is None:
        logger.warning("%s: 発走時刻が判らないため手動Chappyカードを採用しません", race_id)
        return None
    if created_at >= post_at:
        logger.error(
            "%s: 手動Chappyカードが発走時刻以降に作られています（created_at=%s / 発走=%s）。"
            "**結果を見たあとの買い目になりうるので採用しません。**",
            race_id, created_at.isoformat(timespec="minutes"),
            post_at.isoformat(timespec="minutes"),
        )
        return None

    payload["_verified_pre_race"] = True
    return payload


def _decision_summary(roles: dict[str, dict[str, Any]]) -> str:
    a, e, s = roles["win_anchor"], roles["top3_edge"], roles["support"]
    return (
        f"勝ち軸は{a['num']}番{a['name']}。"
        f"Top3妙味は{e['num']}番{e['name']}を最重視。"
        f"能力側の相手に{s['num']}番{s['name']}を残す。"
    )


def _apply_manual(override: dict[str, Any], roles: dict[str, dict[str, Any]],
                  config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bets = [{k: v for k, v in b.items() if not k.startswith("_")}
            for b in override.get("bets", [])]
    if sum(int(b.get("amt") or 0) for b in bets) != config["total"]:
        raise ValueError("manual Chappy card must total 1000 yen")
    log = {
        "source": "manual_chat",
        "author": override.get("author", "ChatGPT"),
        "created_at": override.get("created_at"),
        "rationale": override.get("rationale", []),
        "verified_pre_race": bool(override.get("_verified_pre_race")),
        "roles": override.get("roles") or {k: v["num"] for k, v in roles.items()},
    }
    return bets, log


def generate_card(horses: list[dict[str, Any]], race: dict[str, Any],
                  config: dict[str, Any], myomi_value: float,
                  speed_quality: dict[str, Any] | None,
                  combo_odds: dict[str, Any] | None,
                  model_id: str | None, model_role: str | None,
                  manual_override: dict[str, Any] | None = None,
                  takeout: float = 0.2) -> tuple[dict[str, Any], dict[str, Any]]:
    board = build_signal_board(horses, race, config, speed_quality)
    roles = choose_roles(board)

    if manual_override is not None:
        bets, manual_log = _apply_manual(manual_override, roles, config)
        source = "manual_chat"
    else:
        bets = build_portfolio(roles, config)
        manual_log = None
        source = "signal_engine"

    p_by_num = {
        h["num"]: h["p"] for h in horses
        if h.get("p") is not None and h.get("num") is not None
    }
    evaluation = cards.evaluate_card(bets, p_by_num, takeout)
    market_ev = cards.evaluate_market_card(bets, p_by_num, combo_odds)

    selected = list(roles.values())
    avg_role_strength = sum(
        roles[name]["role_scores"][name] for name in roles
    ) / len(roles)
    avg_quality = sum(r["data_quality"] for r in selected) / len(selected)
    conviction = max(0.0, min(1.0, 0.70 * avg_role_strength + 0.30 * avg_quality))

    gate_cfg = config["otori_gate"]
    checks = {
        "myomi": myomi_value >= gate_cfg["min_myomi"],
        "conviction": conviction >= gate_cfg["min_conviction"],
        "data_quality": avg_quality >= gate_cfg["min_data_quality"],
        "hit_pct_proxy": evaluation["hit_pct"] >= gate_cfg["min_hit_pct_proxy"],
        "combo_odds": (
            bool(market_ev.get("complete"))
            if gate_cfg["require_complete_combo_odds"]
            else True
        ),
        "market_roi_veto": (
            bool(market_ev.get("complete"))
            and (market_ev.get("expected_roi") or 0.0) >= gate_cfg["min_market_roi_veto"]
        ),
    }
    legendary = all(checks.values())

    if legendary and manual_override is None:
        shift = int(gate_cfg.get("concentration_shift") or 0)
        if shift > 0:
            core = next((b for b in bets if b.get("bucket") == "core" and b["type"] == cards.WIDE), None)
            insurance = next((b for b in bets if b.get("bucket") == "insurance"), None)
            if core and insurance and insurance["amt"] >= shift:
                core["amt"] += shift
                insurance["amt"] -= shift
                bets = [b for b in bets if b["amt"] > 0]
                evaluation = cards.evaluate_card(bets, p_by_num, takeout)
                market_ev = cards.evaluate_market_card(bets, p_by_num, combo_odds)

    decision_log = {
        "engine": config["version"],
        "source": source,
        "roles": {k: {"num": v["num"], "name": v["name"], "odds": v["odds"]} for k, v in roles.items()},
        "conviction": round(conviction, 6),
        "data_quality": round(avg_quality, 6),
        "myomi": myomi_value,
        "otori_gate": {"passed": legendary, "checks": checks},
        "summary": _decision_summary(roles),
        "manual": manual_log,
        "signal_board": board,
    }

    card = {
        "char": "otori" if legendary else "chappy",
        "objective": "dynamic_signal_integration",
        "place_partner_mode": "dynamic",
        "model_version": config["version"],
        "model_role": model_role,
        "portfolio_style": "high_conviction" if legendary else "balanced_edge_1000",
        "source": source,
        "conviction": round(conviction, 6),
        "hit_pct": evaluation["hit_pct"],
        "payout_range": evaluation["payout_range"],
        "market_ev": market_ev,
        "probability_model": {"source": "common_win_p_un-calibrated"},
        "total": config["total"],
        "bets": [{k: v for k, v in bet.items() if k != "bucket"} for bet in bets],
        "decision_log": decision_log,
        "say": (
            "鳳モード。複数シグナルが同時に揃ったため、チャッピーの1000円案を高確信配分へ切り替える。"
            if legendary else decision_log["summary"]
        ),
    }
    return card, decision_log
