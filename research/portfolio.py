"""3連複ポートフォリオ層（portfolio-v1、observe-only）。Issue #39 の PR-D（#38 の内容を統合）。

同じ確率（engine）の上で、3連複 100 円 × 5 点の組み方を、制約あり／なしで比べる。
    制約    1頭が入る組の数の上限（露出上限）と、軸 X を含まない組の最低数（#38 の候補ルール）。
    X       engine の3着内周辺確率が最大の馬。
    測る値  発走前：的中確率（別々の組は同時に当たらないので和）、X が3着内のとき／外れたときの条件付き的中確率、
            露出の最大、X 抜きの組の数、未収録組の補い方 18 通りでの的中確率の幅、
            収録オッズで測った期待回収（市場の確率なので構造上ほぼ払戻率。優位性の指標ではない）。
            発走後（結果があれば）：的中と払戻（反実仮想。正式成績・ROI には入れない）。
    比較    scenario-frame/v1 の記録の券も、同じ確率で同じ値を測る（記録には触れない）。

engine は今は市場だけの分布（research/market_baseline.py の market_trio、主の補い方）。market-mix-v1 は
採用が決まっていないので使わない。展開（ペース・脚質）のデータは無いので、全レース pace_unknown。
この層は事前登録ではない。前向きの採否は PR-E で別に登録する。

入力は発走前に固定した版（research/market_audit.py の固定）。出力は data/research/portfolio/ にだけ書く。
予想・正式成績・UI・Champion・scenario-frame/v1 の記録は変えない。
"""
from __future__ import annotations

import argparse
import datetime
import itertools
import json
import logging
import sys
from pathlib import Path
from typing import Any

from research import common
from research import market_audit as ma
from research import market_baseline as mb

logger = logging.getLogger("research.portfolio")

CONFIG_PATH = common.ROOT / "config" / "portfolio_v1.json"
OUT_DIR = common.ROOT / "data" / "research" / "portfolio"
RACE_SCHEMA = "portfolio-race-v1"
SUMMARY_SCHEMA = "portfolio-summary-v1"
ENGINE_CODE = "research/market_baseline.py"
TRIO = "3連複"
Trio = tuple[int, int, int]


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ 組み方

def choose(dist: dict[Trio, float], x: int, n: int, max_exposure: int | None, min_without_x: int,
           node_limit: int = 2_000_000) -> dict[str, Any]:
    """確率の和が最大になる n 組を、制約の下で選ぶ（全組からの深さ優先探索、上界で枝刈り）。

    上界は「残りの枠を、まだ見ていない組の中で確率の大きい順に埋めた和」で、制約を無視した値なので、
    枝刈りしても最適解は落とさない。node_limit に達したら、その時点の最良を proven_optimal=False で返す。
    """
    ranked = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
    trios = [t for t, _ in ranked]
    probs = [p for _, p in ranked]
    has_x = [x in t for t in trios]
    best: dict[str, Any] = {"sum": -1.0, "picks": None}
    nodes = 0
    exposure: dict[int, int] = {}

    def dfs(start: int, picked: list[int], total: float, without_x: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > node_limit:
            return False
        if len(picked) == n:
            if without_x >= min_without_x and total > best["sum"] + 1e-15:
                best.update(sum=total, picks=list(picked))
            return True
        need = n - len(picked)
        for i in range(start, len(trios) - need + 1):
            # 上界：i から先の大きい順に need 個（確率は降順に並んでいる）
            if total + sum(probs[i:i + need]) <= best["sum"] + 1e-15:
                break
            new_without = without_x + (not has_x[i])
            if min_without_x - new_without > need - 1:
                continue
            t = trios[i]
            if max_exposure is not None and any(exposure.get(h, 0) + 1 > max_exposure for h in t):
                continue
            for h in t:
                exposure[h] = exposure.get(h, 0) + 1
            picked.append(i)
            ok = dfs(i + 1, picked, total + probs[i], new_without)
            picked.pop()
            for h in t:
                exposure[h] -= 1
            if not ok:
                return False
        return True

    complete = dfs(0, [], 0.0, 0)
    if best["picks"] is None:
        # 探索を終えて解が無ければ制約を満たせない。上限で打ち切って解が無いだけなら、満たせないとは言えない
        return {"status": "infeasible" if complete else "search_incomplete", "proven_optimal": complete,
                "nodes": nodes, "tickets": []}
    return {"status": "chosen", "proven_optimal": complete, "nodes": nodes,
            "tickets": [list(trios[i]) for i in best["picks"]]}


def evaluate(tickets: list[list[int]], dist: dict[Trio, float], x: int, odds: dict[Trio, float],
             variants: dict[str, dict[Trio, float]], result: dict[str, Any] | None, unit: int) -> dict[str, Any]:
    """組の集合（3連複だけ）を、同じ確率で測る。"""
    marg = mb.top3_marginals(dist)
    p_x = marg.get(x, 0.0)
    keys = [tuple(sorted(t)) for t in tickets]
    rows, exposure = [], {}
    for t in keys:
        for h in t:
            exposure[h] = exposure.get(h, 0) + 1
        rows.append({"horses": list(t), "p": dist.get(t, 0.0),
                     "branch": "primary_X_in" if x in t else "alternate_X_out",
                     "odds": odds.get(t), "listed": t in odds})
    p_hit = sum(r["p"] for r in rows)
    p_in = sum(r["p"] for r in rows if x in r["horses"])
    p_out = p_hit - p_in
    axis = max(sorted(exposure), key=lambda h: exposure[h]) if exposure else None
    stake = unit * len(rows)
    out: dict[str, Any] = {
        "tickets": rows,
        "n_tickets": len(rows),
        "stake": stake,
        "p_hit": p_hit,
        "p_hit_given_x_in": p_in / p_x if p_x > 0 else None,
        "p_hit_given_x_out": p_out / (1.0 - p_x) if p_x < 1 else None,
        "tickets_without_x": sum(1 for r in rows if x not in r["horses"]),
        "max_exposure": {"horse": axis, "tickets": exposure[axis]} if axis is not None else None,
        "p_hit_variants": ({"min": min(sum(v.get(t, 0.0) for t in keys) for v in variants.values()),
                            "max": max(sum(v.get(t, 0.0) for t in keys) for v in variants.values())}
                           if variants and rows else None),
        # 収録オッズのある組だけで測れる。未収録の組を含むときは出さない（0 や推定オッズで埋めない）
        "expected_return_market": (sum(r["p"] * r["odds"] for r in rows) / len(rows)
                                   if rows and all(r["listed"] for r in rows) else None),
    }
    finish = (result or {}).get("finish") or []
    if len(finish) >= 3:
        top3 = tuple(sorted(int(h) for h in finish[:3]))
        pays = ((result or {}).get("dividends") or {}).get(TRIO) or []
        payout = sum(int(d.get("pay") or 0) * unit // 100 for d in pays
                     if tuple(sorted(int(h) for h in d.get("horses") or [])) in keys)
        out["realized"] = {"top3": list(top3), "hit": top3 in keys, "payout": payout,
                           "dividend_found": bool(pays)}
    return out


# ------------------------------------------------------------------ 1レース

def _scenario_v1(race_id: str, repo: Path) -> list[dict[str, Any]]:
    """scenario-frame/v1 の記録の 3連複の券（記録は読むだけ）。正式な有効性は research.chat_experiment の判定を写す。"""
    rows = []
    for rel, record in ma._scenario_records(race_id, repo):
        bets = [b for b in record.get("bets") or [] if b.get("type") == TRIO and len(b.get("horses") or []) == 3]
        rows.append({"record": rel, "action": record.get("action"), "tickets": [list(b["horses"]) for b in bets],
                     **ma._official_validity(rel, repo)})
    return rows


def portfolio_race(race_id: str, result: dict[str, Any] | None, config: dict[str, Any],
                   baseline_config: dict[str, Any], repo: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"schema": RACE_SCHEMA, "race_id": race_id, "used_for_prediction": False,
                           "official_results_untouched": True, "pace": config["pace"]}
    found = ma.prerace_versions(race_id, repo)
    post, versions = found["post"], found["versions"]
    out["post_at"] = ma._iso(post)
    if not versions:
        return {**out, "status": "excluded", "reason": "no_prerace_commit"}
    final = versions[-1]
    snap = final["snapshot"]
    race = ma.raw_race(final["sha"], snap.get("week_id"), race_id, repo)
    out["pinned"] = {"commit": final["sha"], "frozen_at": snap.get("frozen_at")}
    if race is None:
        return {**out, "status": "excluded", "reason": "raw_missing_at_pinned_commit"}
    timing = {"trio": mb._timing(race, "sanrenpuku", post, final["committed_at"]),
              "win": mb._timing(race, "win", post, final["committed_at"])}
    out["odds_timing_status"] = timing
    if any(v != "pre_race_verified" for v in timing.values()):
        return {**out, "status": "excluded", "reason": "odds_timing_not_pre_race_verified"}
    p = mb.win_probs(race)
    runners = sorted(p)
    entries = [e.get("num") for e in race.get("entries") or [] if isinstance(e.get("num"), int)]
    if set(entries) - set(runners):
        return {**out, "status": "excluded", "reason": "runner_without_win_odds_unconfirmed"}
    if len(runners) < 3:
        return {**out, "status": "excluded", "reason": "fewer_than_3_runners_with_odds"}
    dh = baseline_config["discounted_harville"]
    trios = list(itertools.combinations(runners, 3))
    shapes = {"harville": mb.harville_trio(p),
              "discounted_harville": mb.harville_trio(p, (dh["lambda_second"], dh["lambda_third"])),
              "uniform": {t: 1.0 / len(trios) for t in trios}}
    dist, info = mb.market_trio(race, runners, baseline_config["trio_payout"], shapes["harville"])
    if not dist:
        return {**out, "status": "excluded", "reason": "no_trio_odds"}
    variants = {mb.variant_key(pay, shape, cap): mb.market_trio(race, runners, pay, shapes[shape], use_cap=cap)[0]
                for pay in mb.SENSITIVITY_PAYOUTS for shape in mb.SENSITIVITY_SHAPES for cap in (True, False)}
    odds, _ = ma._trio_parse(race)
    marg = mb.top3_marginals(dist)
    x = max(sorted(marg), key=lambda h: marg[h])
    out["engine"] = {"name": config["engine"]["name"], "market_trio_method": info["method"],
                     "listed": info["listed"], "unlisted": info["unlisted"]}
    out["x"] = {"horse": x, "p_top3": marg[x]}
    unit, n = config["budget"]["unit_yen"], config["budget"]["max_tickets"]
    strategies = {}
    for name, rule in config["strategies"].items():
        picked = choose(dist, x, n, rule["max_exposure"], rule["min_tickets_without_x"],
                        config["search"]["node_limit"])
        strategies[name] = {"status": picked["status"], "proven_optimal": picked["proven_optimal"],
                            "search_nodes": picked["nodes"],
                            **(evaluate(picked["tickets"], dist, x, odds, variants, result, unit)
                               if picked["tickets"] else
                               {"reason": "constraints_not_satisfiable" if picked["status"] == "infeasible"
                                else "search_node_limit"})}
    out["strategies"] = strategies
    out["scenario_frame_v1"] = [{**{k: v for k, v in rec.items() if k != "tickets"},
                                 **evaluate(rec["tickets"], dist, x, odds, variants, result, unit)}
                                for rec in _scenario_v1(race_id, repo) if rec["tickets"]]
    out["status"] = "evaluated" if len((result or {}).get("finish") or []) >= 3 else "pending_result"
    return out


# ------------------------------------------------------------------ 集計

def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _block(items: list[dict[str, Any]]) -> dict[str, Any]:
    done = [s for s in items if s.get("realized")]
    return {
        "races": len(items),
        "mean_p_hit": _mean([s["p_hit"] for s in items]),
        "mean_p_hit_given_x_out": _mean([s["p_hit_given_x_out"] for s in items]),
        "mean_p_hit_given_x_in": _mean([s["p_hit_given_x_in"] for s in items]),
        "mean_tickets_without_x": _mean([s["tickets_without_x"] for s in items]),
        "mean_max_exposure": _mean([(s["max_exposure"] or {}).get("tickets") for s in items]),
        "mean_expected_return_market": _mean([s["expected_return_market"] for s in items]),
        "realized_counterfactual": {"races": len(done), "hits": sum(s["realized"]["hit"] for s in done),
                                    "stake": sum(s["stake"] for s in done),
                                    "payout": sum(s["realized"]["payout"] for s in done)},
    }


def summarize(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    used = [r for r in rows if r.get("status") in ("evaluated", "pending_result")]

    def ok(s: dict[str, Any]) -> bool:
        # 探索の上限で最適と示せなかった組は、組み方どうしの比較に混ぜない（not_proven_optimal に別に残す）
        return s["status"] == "chosen" and s["proven_optimal"]

    per_strategy, infeasible = {}, {}
    for name in config["strategies"]:
        per_strategy[name] = _block([r["strategies"][name] for r in used if ok(r["strategies"][name])])
        infeasible[name] = [r["race_id"] for r in used if r["strategies"][name]["status"] == "infeasible"]
    base = "top_prob"
    both = [r for r in used if all(ok(r["strategies"][s]) for s in config["strategies"])]
    cost = {name: {"races": len(both),
                   "mean_delta_p_hit": _mean([r["strategies"][name]["p_hit"] - r["strategies"][base]["p_hit"]
                                              for r in both]),
                   "mean_delta_p_hit_given_x_out": _mean([r["strategies"][name]["p_hit_given_x_out"]
                                                          - r["strategies"][base]["p_hit_given_x_out"] for r in both])}
            for name in config["strategies"] if name != base}
    v1 = [rec for r in used for rec in r["scenario_frame_v1"]]
    excluded: dict[str, list[str]] = {}
    for r in rows:
        if r.get("status") in ("excluded", "error"):
            excluded.setdefault(r.get("reason") or r.get("error_type") or r["status"], []).append(r["race_id"])
    errors = [{"race_id": r["race_id"], "error_type": r.get("error_type")} for r in rows if r.get("status") == "error"]
    return {
        "races": len(rows),
        "used": len(used),
        "errors": errors,
        "pending_result": [r["race_id"] for r in used if r["status"] == "pending_result"],
        "excluded": excluded,
        "strategies": per_strategy,
        "infeasible": infeasible,
        "constraint_cost_vs_top_prob": cost,
        "not_proven_optimal": sorted({r["race_id"] for r in used for s in r["strategies"].values()
                                      if not s["proven_optimal"]}),
        "scenario_frame_v1": {"all_records": _block(v1),
                              "official_v1_valid_records": _block([rec for rec in v1 if rec.get("official_v1_valid")])},
    }


def run(repo: Path | None = None, out_dir: Path | None = None, config: dict[str, Any] | None = None,
        results_path: str = "data/race_results.json") -> dict[str, Any]:
    repo = repo or common.ROOT
    out_dir = out_dir or OUT_DIR
    config = config or load_config()
    baseline_config = mb.load_config()
    results = ma._head_json(results_path, repo) or {}
    rows = []
    for race_id in ma._race_ids(repo):
        try:
            row = portfolio_race(race_id, results.get(race_id), config, baseline_config, repo)
        except Exception as exc:  # 1レースの失敗で止めない。理由は型名だけ
            logger.exception("ポートフォリオの計算に失敗: %s", race_id)
            row = {"schema": RACE_SCHEMA, "race_id": race_id, "status": "error", "error_type": type(exc).__name__}
        common.write_json(out_dir / "races" / f"{race_id}.json", row)
        rows.append(row)
    code = ma._git(["rev-parse", f"HEAD:{ENGINE_CODE}"], repo)
    summary = {
        "schema": SUMMARY_SCHEMA,
        "generated_at": datetime.datetime.now(common.JST).isoformat(timespec="seconds"),
        "config_version": config.get("version"),
        "config_hash": mb.config_hash(config),
        "engine": config["engine"]["name"],
        "engine_code_blob": code.stdout.decode().strip() if code.returncode == 0 else None,
        "used_for_prediction": False,
        "official_results_untouched": True,
        "note": ("3連複ポートフォリオ層（observe-only、事前登録ではない）。確率は市場だけの分布で、モデルの優位性を含まない。"
                 "的中確率は確率の和、期待回収は収録オッズのある組だけで測る市場の値で、構造上ほぼ払戻率になる。"
                 "発走後の的中・払戻は反実仮想で、正式成績・ROI には入れない。展開のデータは無いので pace_unknown。"),
        **summarize(rows, config),
    }
    common.write_json(out_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="3連複ポートフォリオ層（observe-only）")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    summary = run(args.repo, args.out)
    print(json.dumps({k: summary[k] for k in ("races", "used", "excluded", "errors")}, ensure_ascii=False))
    # 1レースでも計算に失敗したら、出力を書き終えた後に終了コード 1 にする（失敗を成功と見誤らない）。
    # 契約による除外（excluded）や、制約を満たせない組み方（infeasible）は失敗ではない
    if summary["errors"]:
        logger.warning("portfolio: %d レースの計算に失敗: %s", len(summary["errors"]),
                       ", ".join(e["race_id"] for e in summary["errors"]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
