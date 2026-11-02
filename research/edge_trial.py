"""edge-loop の試験の版 edge-v1（印の中の5組）の前向きの記録（observe-only）。

仕様は docs/research/EDGE_V1_PREREG.md と config/edge_v1.json（登録コミットで凍結）。
この実装は仕様に無い選択をしない。仕様の節番号（§）をコメントに残す。

    登録（§1）  main の first-parent 履歴で config が初めて現れたコミットから、登録日時と SHA-256 を導出する。
                今の文書・設定が違えば spec_changed で止まる。券の決め方（research/edge_rules.py）は登録コミットの版を
                Git から読む。市場の確率の engine は、本線と同じものにするため、portfolio-shadow-v1 の登録コミットの版を
                portfolio-shadow-v1 と同じ方法で読む。
    券（§2）    発走前の最終の凍結版の印の馬の中で、市場の確率が高い5組。入力は発走前に固定した版だけなので、
                計算が発走後でも、発走前に決めた券と同じになる。
    比較（§5）  本線 edge-v0 は portfolio-shadow-v1 の記録の x_out_2。共通のレースの d を、見る時点でだけ判定する。

出力は data/research/edge_loop/edge-v1/ にだけ書く。予想・正式成績・UI・Champion・scenario-frame/v1・
チャットの参考券は変えない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import random
import sys
import types
from pathlib import Path
from typing import Any, Collection

from research import common
from research import portfolio_shadow as ps

logger = logging.getLogger("research.edge_trial")

VERSION = "edge-v1"
OUT_DIR = common.ROOT / "data" / "research" / "edge_loop" / VERSION
SPEC_REL = "docs/research/EDGE_V1_PREREG.md"
CONFIG_REL = "config/edge_v1.json"
RULE_REL = "research/edge_rules.py"
MAIN_RACES_REL = "data/research/portfolio_shadow/races"
RESULTS_REL = "data/race_results.json"
# 今の版のまま使う依存（engine の読み込みに portfolio_shadow を使う）。登録コミットから変わったら警告する
LIVE_DEPENDENCIES = ("research/portfolio_shadow.py",)
RACE_SCHEMA = "edge-trial-race-v1"
SUMMARY_SCHEMA = "edge-trial-summary-v1"
TRIO = "3連複"

# ------------------------------------------------------------------ 登録した固定値（設定と一致することをテストで確かめる）

MARK_SYMBOLS = ("◎", "○", "▲", "△", "✕")
MIN_MARKED = 3
MAX_TICKETS = 5
UNIT_YEN = 100
MAIN_STRATEGY = "x_out_2"
CHECKPOINTS = (60, 120, 240, 480)
RESULT_WAIT_DAYS = 14
RESULT_UNAVAILABLE_DAYS = 14
BOOT_SEED = 20261101
BOOT_REPS = 10000
BOOT_LOWER_RANK = 50
BOOT_UPPER_RANK = 9950


# ------------------------------------------------------------------ 登録（§1）

def registration(repo: Path) -> dict[str, Any]:
    ref = ps._main_ref(repo)
    if ref is None:
        return {"status": "registration_not_found", "reason": "main_ref_missing"}
    out = ps._git(["log", "--first-parent", "--format=%H %cI", ref, "--", CONFIG_REL], repo)
    lines = [ln for ln in out.stdout.decode().splitlines() if ln.strip()] if out.returncode == 0 else []
    if not lines:
        return {"status": "registration_not_found", "reason": "config_not_in_main_first_parent_history", "ref": ref}
    commit, _, stamp = lines[-1].partition(" ")
    registered_at = common.parse_dt(stamp)
    frozen = {}
    for rel in (SPEC_REL, CONFIG_REL):
        reg = ps._git_bytes(commit, rel, repo)
        path = repo / rel
        cur = path.read_bytes() if path.exists() else None
        frozen[rel] = {"registered_sha256": common.sha256_bytes(reg) if reg is not None else None,
                       "current_sha256": common.sha256_bytes(cur) if cur is not None else None,
                       "identical": reg is not None and cur == reg}
    rule_reg, rule_cur = ps._blob(commit, RULE_REL, repo), ps._blob("HEAD", RULE_REL, repo)
    engine = ps.registration(repo)
    live = {rel: {"registered_blob": ps._blob(commit, rel, repo), "current_blob": ps._blob("HEAD", rel, repo)}
            for rel in LIVE_DEPENDENCIES}
    status = "ok"
    if not all(f["identical"] for f in frozen.values()):
        status = "spec_changed"
    elif rule_reg is None:
        status = "rule_module_missing_at_registration"
    elif engine.get("status") != "ok":
        status = "engine_registration_not_ok"
    return {
        "status": status, "ref": ref, "commit": commit,
        "registered_at": registered_at.astimezone(common.JST).isoformat(timespec="seconds") if registered_at else None,
        "config_sha256": frozen[CONFIG_REL]["registered_sha256"],
        "spec_sha256": frozen[SPEC_REL]["registered_sha256"],
        "frozen_files": frozen,
        "rule_module": {"path": RULE_REL, "registered_blob": rule_reg, "current_blob": rule_cur,
                        "used": "registered", "current_differs": rule_reg != rule_cur},
        "engine": {"from": "portfolio-shadow-v1", "status": engine.get("status"), "commit": engine.get("commit")},
        "live_dependencies": live,
        "live_dependency_changed": sorted(rel for rel, d in live.items() if d["registered_blob"] != d["current_blob"]),
        "note": "Git から導出した記録（出力であって仕様ではない）。仕様の本体は登録コミットの文書と設定。",
    }


def load_rule(repo: Path, commit: str) -> types.ModuleType:
    """登録コミットの版の research/edge_rules.py を読み込む（§1）。標準ライブラリしか import しないファイル。"""
    src = ps._git_bytes(commit, RULE_REL, repo)
    if src is None:
        raise FileNotFoundError(f"{RULE_REL} が登録コミットに無い")
    mod = types.ModuleType("research.edge_rules_registered")
    mod.__file__ = str(repo / RULE_REL)
    mod.__registered_source__ = f"{commit}:{RULE_REL}"
    # リポジトリ自身の、登録コミットに入っているコードだけを実行する
    exec(compile(src, f"{commit}:{RULE_REL}", "exec", dont_inherit=True), mod.__dict__)
    return mod


# ------------------------------------------------------------------ 1レース（§2・§3）

def main_values(main_row: dict[str, Any] | None) -> dict[str, Any]:
    """本線 edge-v0（portfolio-shadow-v1 の x_out_2）の同じレースの値（§3・§5）。"""
    if not main_row:
        return {"status": "missing", "usable": False}
    s = (main_row.get("strategies") or {}).get(MAIN_STRATEGY) or {}
    realized = s.get("realized") or {}
    usable = (main_row.get("status") == "evaluated" and s.get("status") == "chosen"
              and s.get("proven_optimal") is True and s.get("realized") is not None)
    return {"status": main_row.get("status"), "usable": usable,
            "pinned_commit": (main_row.get("pinned") or {}).get("commit"),
            "tickets": s.get("tickets"), "p_pre": s.get("p_hit"), "stake": s.get("stake"),
            "hit": realized.get("hit"), "payout": realized.get("payout"),
            "dividend_found": realized.get("dividend_found")}


def _excluded(row: dict[str, Any], reason: str, contract: str) -> dict[str, Any]:
    return {**row, "status": "excluded", "reason": reason, "contract": contract}


def trial_race(race_id: str, result: dict[str, Any] | None, ref: dict[str, Any], rule: types.ModuleType,
               repo: Path, registered_at: datetime.datetime, main_row: dict[str, Any] | None) -> dict[str, Any]:
    ma, mb, baseline = ref["ma"], ref["mb"], ref["baseline"]
    found = ma.prerace_versions(race_id, repo)
    post, versions = found["post"], found["versions"]
    row: dict[str, Any] = {"schema": RACE_SCHEMA, "version": VERSION, "race_id": race_id, "race_day": race_id[:8],
                           "post_at": ma._iso(post), "used_for_prediction": False, "official_results_untouched": True}
    if post is not None and post <= registered_at:
        # 登録前に発走したレースは対象にしないので、券も計算しない
        return {**row, "role": "before_registration", "status": "before_registration"}
    row["role"] = "eligible" if post is not None else "unknown_post"
    # §2-1：発走前の固定とオッズの時点（portfolio_race と同じ契約）
    if not versions:
        return _excluded(row, "no_prerace_commit", "prerace")
    final = versions[-1]
    snap = final["snapshot"]
    row["pinned"] = {"commit": final["sha"], "frozen_at": snap.get("frozen_at"), "model_id": snap.get("model_id")}
    race = ma.raw_race(final["sha"], snap.get("week_id"), race_id, repo)
    if race is None:
        return _excluded(row, "raw_missing_at_pinned_commit", "prerace")
    timing = {"trio": mb._timing(race, "sanrenpuku", post, final["committed_at"]),
              "win": mb._timing(race, "win", post, final["committed_at"])}
    row["odds_timing_status"] = timing
    if any(v != "pre_race_verified" for v in timing.values()):
        return _excluded(row, "odds_timing_not_pre_race_verified", "prerace")
    p = mb.win_probs(race)
    runners = sorted(p)
    entries = [e.get("num") for e in race.get("entries") or [] if isinstance(e.get("num"), int)]
    if set(entries) - set(runners):
        return _excluded(row, "runner_without_win_odds_unconfirmed", "prerace")
    if len(runners) < 3:
        return _excluded(row, "fewer_than_3_runners_with_odds", "prerace")
    # §2-2：市場の確率（本線と同じ engine・払戻率・形）
    dist, info = mb.market_trio(race, runners, baseline["trio_payout"], mb.harville_trio(p))
    if not dist:
        return _excluded(row, "no_trio_odds", "prerace")
    row["engine"] = {"market_trio_method": info["method"], "listed": info["listed"], "unlisted": info["unlisted"]}
    # §2-3〜6：印の馬の中で q が大きい順に5組（登録コミットの版の決め方）
    marked = rule.marked_horses(snap.get("marks"), runners, MARK_SYMBOLS)
    tickets = rule.marks_top_n(dist, marked, MAX_TICKETS, MIN_MARKED)
    row.update(marked=marked, action="bet" if tickets else "pass", tickets=[list(t) for t in tickets],
               p_pre=sum(dist[t] for t in tickets), stake=UNIT_YEN * len(tickets))
    main = main_values(main_row)
    main["same_pinned"] = main.get("pinned_commit") == final["sha"]
    row["main"] = main
    finish = (result or {}).get("finish") or []
    if len(finish) < 3:
        return {**row, "status": "pending_result"}
    # §3 の結果の契約
    top3 = sorted(int(h) for h in finish[:3])
    trio_divs = ((result or {}).get("dividends") or {}).get(TRIO) or []
    row["result_used"] = {"top3": top3, "trio_dividends": len(trio_divs)}
    if len(trio_divs) >= 2:
        return _excluded(row, "dead_heat_top3", "result")
    if not set(top3) <= set(runners):
        return _excluded(row, "top3_horse_without_prerace_odds", "result")
    hit = tuple(top3) in tickets
    div = next((d for d in trio_divs if sorted(int(h) for h in d.get("horses") or []) == top3), None)
    payout = int(div["pay"]) * UNIT_YEN // 100 if (hit and div is not None) else 0
    row["realized"] = {"top3": top3, "hit": hit, "payout": payout, "dividend_found": div is not None}
    return {**row, "status": "evaluated"}


# ------------------------------------------------------------------ 集計（§4）

def _payout_known(hit: Any, dividend_found: Any) -> bool:
    """払戻が分かるか。外れなら 0 円で確定。的中なのに配当が無いときは分からない（0 円とみなさない）。"""
    return (not hit) or bool(dividend_found)


def resolution(r: dict[str, Any], unavailable: Collection[str] = ()) -> tuple[str, str | None]:
    """§5 のレースの解決：common（共通）・not_common（対象外）・awaiting（結果か本線の記録を待っている）・error。

    台帳で result_unavailable と確定したレースは、後から結果や本線の記録が来ても not_common のまま。
    """
    if r["race_id"] in unavailable:
        return "not_common", "result_unavailable"
    status = r.get("status")
    if status == "error":
        return "error", "v1_error"
    if status == "excluded":
        return "not_common", r.get("reason")
    if status == "pending_result":
        return "awaiting", "v1_pending_result"
    if status != "evaluated":
        return "awaiting", f"v1_{status}"
    m = r.get("main") or {}
    if m.get("usable") and m.get("same_pinned"):
        return "common", None
    if m.get("status") == "error":
        return "error", "main_error"
    if m.get("status") in (None, "missing", "pending_result"):
        return "awaiting", f"main_{m.get('status') or 'missing'}"
    if m.get("usable"):
        return "not_common", "main_pinned_differs"
    return "not_common", "main_not_usable"


def is_common(r: dict[str, Any], unavailable: Collection[str] = ()) -> bool:
    """§5 の共通のレース：この版が evaluated で、本線の記録が使え、同じ凍結版を見ている（台帳で外したものを除く）。"""
    return resolution(r, unavailable)[0] == "common"


def _block(hits: list[int], ps_: list[float], stakes: list[int], pays: list[int | None]) -> dict[str, Any]:
    n, h, e = len(hits), sum(hits), sum(ps_)
    known = [(s, p) for s, p in zip(stakes, pays) if p is not None]
    stake, payout = sum(s for s, _ in known), sum(p for _, p in known)
    return {"races": n, "H": h, "E": e, "H_over_E": h / e if e > 0 else None,
            "H_minus_E_per_race": (h - e) / n if n else None,
            "stake": stake, "payout": payout, "roi": payout / stake if stake else None,
            "races_payout_unknown": sum(1 for p in pays if p is None)}


def _v1_values(r: dict[str, Any]) -> tuple[int, float, int, int | None]:
    real = r["realized"]
    hit = int(bool(real["hit"]))
    return hit, r["p_pre"], r["stake"], real["payout"] if _payout_known(real["hit"], real["dividend_found"]) else None


def _v0_values(r: dict[str, Any]) -> tuple[int, float, int, int | None]:
    m = r["main"]
    hit = int(bool(m["hit"]))
    return hit, m["p_pre"], m["stake"], m["payout"] if _payout_known(m["hit"], m["dividend_found"]) else None


def report(rows: list[dict[str, Any]], unavailable: Collection[str] = ()) -> dict[str, Any]:
    ev = sorted((r for r in rows if r.get("role") == "eligible" and r["status"] == "evaluated"),
                key=lambda r: r["race_id"])
    v1 = [_v1_values(r) for r in ev]
    both = [r for r in ev if is_common(r, unavailable)]
    c1, c0 = [_v1_values(r) for r in both], [_v0_values(r) for r in both]
    d = [(a[0] - a[1]) - (b[0] - b[1]) for a, b in zip(c1, c0)]
    return {
        "evaluated": len(ev),
        "pass_races": sum(1 for r in ev if r["action"] == "pass"),
        "v1": _block(*map(list, zip(*v1))) if v1 else _block([], [], [], []),
        "common": {
            "races": len(both),
            "n_day": len({r["race_day"] for r in both}),
            "v1": _block(*map(list, zip(*c1))) if c1 else _block([], [], [], []),
            "v0": _block(*map(list, zip(*c0))) if c0 else _block([], [], [], []),
            "mean_d": sum(d) / len(d) if d else None,
            "hit_overlap": {"both": sum(1 for a, b in zip(c1, c0) if a[0] and b[0]),
                            "v1_only": sum(1 for a, b in zip(c1, c0) if a[0] and not b[0]),
                            "v0_only": sum(1 for a, b in zip(c1, c0) if b[0] and not a[0]),
                            "neither": sum(1 for a, b in zip(c1, c0) if not a[0] and not b[0])},
        },
        "not_common": sorted(r["race_id"] for r in ev if not is_common(r, unavailable)),
    }


# ------------------------------------------------------------------ 判定（§5）

def bootstrap(day_values: list[list[float]]) -> dict[str, Any]:
    """§5：開催日の復元抽出（レースに等しい重み）、並べた値の 50・9950 番目（99%）。"""
    n_day = len(day_values)
    if n_day == 0:
        return {"n_day": 0, "lower": None, "upper": None}
    sums = [sum(v) for v in day_values]
    counts = [len(v) for v in day_values]
    rng = random.Random(BOOT_SEED)
    stats = []
    for _ in range(BOOT_REPS):
        s = c = 0
        for _ in range(n_day):
            i = rng.randrange(n_day)
            s += sums[i]
            c += counts[i]
        stats.append(s / c)
    stats.sort()
    return {"n_day": n_day, "replicates": BOOT_REPS, "seed": BOOT_SEED, "level": 0.99,
            "lower": stats[BOOT_LOWER_RANK - 1], "upper": stats[BOOT_UPPER_RANK - 1]}


def _date(day: str) -> datetime.date:
    return datetime.date(int(day[:4]), int(day[4:6]), int(day[6:8]))


def load_ledger(out_dir: Path) -> dict[str, Any]:
    """§5 の台帳。開催日から14日たっても結果がそろわず、判定から外すと確定したレース（result_unavailable）。"""
    ledger = common.read_json(out_dir / "ledger.json") or {}
    ledger.setdefault("result_unavailable", {})
    ledger["note"] = ("開催日から14日たっても、この版の結果か本線の記録がそろわなかったレース。判定から外すことを"
                      "確定した日と理由を残し、後から結果が来ても共通のレースに戻さない（§5）。上書きしない。")
    return ledger


def apply_cutoff(rows: list[dict[str, Any]], ledger: dict[str, Any], as_of: datetime.date) -> list[str]:
    """§5：開催日から14日たっても awaiting のレースを、台帳で result_unavailable に確定する。計算の失敗は外さない。"""
    newly = []
    for r in rows:
        if r.get("role") != "eligible":
            continue
        state, reason = resolution(r, ledger["result_unavailable"])
        if state == "awaiting" and as_of >= _date(r["race_day"]) + datetime.timedelta(days=RESULT_UNAVAILABLE_DAYS):
            ledger["result_unavailable"][r["race_id"]] = {"fixed_on": as_of.isoformat(), "reason": reason}
            newly.append(r["race_id"])
    return newly


def checkpoint_state(rows: list[dict[str, Any]], as_of: datetime.date,
                     unavailable: Collection[str] = ()) -> dict[str, Any]:
    """共通のレースの数が 60・120・240・480 に初めて達した開催日を求め、14日たったものを判定する（§5）。

    未解決のレース（結果か本線の記録を待っているもの、計算の失敗）が1つでもある開催日から先では、到達日を決めない。
    そのため、到達日までのレースは、すべて共通か対象外に決まっている（結果がそろってから判定する）。
    結果がそろわないレースは、開催日から14日で台帳により対象外に確定する（apply_cutoff）。計算の失敗は直るまで止める。
    """
    eligible = [r for r in rows if r.get("role") == "eligible"]
    res = {r["race_id"]: resolution(r, unavailable) for r in eligible}
    unresolved = sorted({r["race_day"] for r in eligible if res[r["race_id"]][0] in ("awaiting", "error")})
    common_rows = sorted((r for r in eligible if res[r["race_id"]][0] == "common"), key=lambda r: r["race_id"])
    per_day: dict[str, int] = {}
    for r in common_rows:
        per_day[r["race_day"]] = per_day.get(r["race_day"], 0) + 1
    cum, reached, resolved_through = 0, {}, None
    for day in sorted({r["race_day"] for r in eligible}):
        if unresolved and day >= unresolved[0]:
            break
        resolved_through = day
        cum += per_day.get(day, 0)
        for target in CHECKPOINTS:
            if cum >= target and target not in reached:
                reached[target] = day
    state: dict[str, Any] = {
        "common_races": len(common_rows), "reached": {str(k): v for k, v in reached.items()},
        "resolved_through": resolved_through, "unresolved_days": unresolved,
        "awaiting": sorted(rid for rid, (st, _) in res.items() if st == "awaiting"),
        "errors": sorted(rid for rid, (st, _) in res.items() if st == "error"),
        "checkpoints": {}}
    for target, end_day in reached.items():
        wait_until = _date(end_day) + datetime.timedelta(days=RESULT_WAIT_DAYS)
        cp: dict[str, Any] = {"end_day": end_day, "result_wait_until": wait_until.isoformat()}
        if as_of < wait_until:
            state["checkpoints"][str(target)] = {**cp, "status": "awaiting_results"}
            continue
        window = [r for r in common_rows if r["race_day"] <= end_day]
        by_day: dict[str, list[float]] = {}
        for r in window:
            a, b = _v1_values(r), _v0_values(r)
            by_day.setdefault(r["race_day"], []).append((a[0] - a[1]) - (b[0] - b[1]))
        boot = bootstrap([by_day[d] for d in sorted(by_day)])
        rep = report(window, unavailable)["common"]
        roi1, roi0 = rep["v1"]["roi"], rep["v0"]["roi"]
        met = (boot["lower"] is not None and boot["lower"] > 0 and roi1 is not None and roi0 is not None
               and roi1 >= roi0)
        resolutions = {r["race_id"]: list(res[r["race_id"]]) for r in sorted(eligible, key=lambda r: r["race_id"])
                       if r["race_day"] <= end_day}
        state["checkpoints"][str(target)] = {**cp, "status": "final", "races": [r["race_id"] for r in window],
                                             "resolutions": resolutions, "bootstrap": boot, "report": rep,
                                             "criterion_met": met}
    return state


def _round(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v) for v in obj]
    return obj


def _digest(obj: Any) -> str:
    return common.sha256_bytes(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def checkpoint_record(cp: dict[str, Any], rows: list[dict[str, Any]], reg: dict[str, Any]) -> dict[str, Any]:
    by_id = {r["race_id"]: r for r in rows}
    inputs = {"races": {rid: {"pinned_commit": (by_id[rid].get("pinned") or {}).get("commit"),
                              "tickets": by_id[rid].get("tickets"), "result_used": by_id[rid].get("result_used"),
                              "main": {k: (by_id[rid].get("main") or {}).get(k) for k in ("tickets", "hit", "p_pre")}}
                        for rid in cp["races"]},
              "resolutions": cp["resolutions"]}
    outputs = _round({k: cp[k] for k in ("end_day", "bootstrap", "report", "criterion_met")})
    return {"registration_commit": reg["commit"], "config_sha256": reg["config_sha256"], "inputs": inputs,
            "outputs": outputs, "inputs_digest": _digest(inputs), "outputs_digest": _digest(outputs),
            "digest": _digest({"registration_commit": reg["commit"], "inputs": inputs, "outputs": outputs})}


# ------------------------------------------------------------------ 実行（§6）

def run(repo: Path | None = None, out_dir: Path | None = None, as_of: datetime.date | None = None,
        reference: dict[str, Any] | None = None, rule: types.ModuleType | None = None,
        simulate: bool = False) -> dict[str, Any]:
    """simulate=True は今の入力のまま日付だけを変えた計算。本番の出力先には書けず、checkpoints.json・ledger.json は書かない。"""
    repo = repo or common.ROOT
    out_dir = out_dir or OUT_DIR
    if simulate and out_dir.resolve() == OUT_DIR.resolve():
        raise ValueError("simulation は本番の出力先に書かない（--out で別の場所を指定する）")
    as_of = as_of or datetime.datetime.now(common.JST).date()
    reg = registration(repo)
    common.write_json(out_dir / "registration.json", reg)
    base = {"schema": SUMMARY_SCHEMA, "version": VERSION,
            "generated_at": datetime.datetime.now(common.JST).isoformat(timespec="seconds"),
            "as_of": as_of.isoformat(), "as_of_mode": "simulation_on_current_inputs" if simulate else "live",
            "used_for_prediction": False, "official_results_untouched": True,
            "registration": {k: reg.get(k) for k in ("status", "commit", "registered_at", "config_sha256")}}
    if reg["status"] != "ok":
        summary = {**base, "status": reg["status"], "warnings": [reg["status"]],
                   "note": "登録の導出に失敗したか、凍結した仕様が登録コミットと違うため、記録を出さない（§1）。"}
        common.write_json(out_dir / "summary.json", summary)
        return summary
    registered_at = common.parse_dt(reg["registered_at"])
    ref = reference or ps.load_reference(repo, reg["engine"]["commit"])
    rule = rule or load_rule(repo, reg["commit"])
    results = ps._head_json(RESULTS_REL, repo) or {}
    rows = []
    for race_id in ref["ma"]._race_ids(repo):
        main_row = ps._head_json(f"{MAIN_RACES_REL}/{race_id}.json", repo)
        try:
            row = trial_race(race_id, results.get(race_id), ref, rule, repo, registered_at, main_row)
        except Exception as exc:  # 1レースの失敗で止めない。理由は型名だけ
            logger.exception("edge-v1 の計算に失敗: %s", race_id)
            row = {"schema": RACE_SCHEMA, "version": VERSION, "race_id": race_id, "race_day": race_id[:8],
                   "status": "error", "error_type": type(exc).__name__,
                   "role": "eligible" if _date(race_id[:8]) >= registered_at.astimezone(common.JST).date()
                   else "before_registration"}
        rows.append(row)

    ledger = load_ledger(out_dir)
    newly_unavailable = apply_cutoff(rows, ledger, as_of)
    unavailable = set(ledger["result_unavailable"])
    if not simulate:
        common.write_json(out_dir / "ledger.json", ledger)
    for r in rows:
        if r["race_id"] in unavailable:
            r["ledger"] = {"state": "result_unavailable", **ledger["result_unavailable"][r["race_id"]]}
    state = checkpoint_state(rows, as_of, unavailable)
    path = out_dir / "checkpoints.json"
    fixed = common.read_json(path) or {}
    mismatched = []
    for target, cp in state["checkpoints"].items():
        if cp["status"] != "final":
            continue
        current = checkpoint_record(cp, rows, reg)
        if target not in fixed:
            if not simulate:
                fixed[target] = {**current, "decided_on": as_of.isoformat()}
        elif fixed[target].get("digest") != current["digest"]:
            mismatched.append(target)
    if not simulate and fixed:
        common.write_json(path, fixed)
    met = sorted((t for t, rec in fixed.items() if rec["outputs"].get("criterion_met")), key=int)
    last = str(CHECKPOINTS[-1])
    if met:
        status = "criterion_met"
    elif last in fixed:
        status = "stopped_not_met"
    elif any(cp["status"] == "awaiting_results" for cp in state["checkpoints"].values()):
        status = "awaiting_results"
    else:
        status = "collecting"

    errors = [{"race_id": r["race_id"], "error_type": r.get("error_type")} for r in rows if r["status"] == "error"]
    warnings = []
    if errors:
        warnings.append("race_errors")
    if mismatched:
        warnings.append("checkpoint_recomputed_differs_from_fixed")
    if reg.get("live_dependency_changed"):
        warnings.append("live_dependency_changed")
    for r in rows:
        common.write_json(out_dir / "races" / f"{r['race_id']}.json", _round(r))
    eligible = [r for r in rows if r.get("role") == "eligible"]
    excluded: dict[str, list[str]] = {}
    for r in eligible:
        if r["status"] == "excluded":
            excluded.setdefault(r["reason"], []).append(r["race_id"])
    summary = {
        **base,
        "status": status,
        "warnings": warnings,
        "errors": errors,
        "races": len(rows),
        "eligible": len(eligible),
        "evaluated": sum(1 for r in eligible if r["status"] == "evaluated"),
        "pending_result": sorted(r["race_id"] for r in eligible if r["status"] == "pending_result"),
        "excluded": excluded,
        "checkpoints": _round(state),
        "checkpoints_fixed": sorted(fixed, key=int),
        "checkpoints_mismatched": mismatched,
        "criterion_met_at": met,
        "result_unavailable": sorted(unavailable),
        "result_unavailable_newly_fixed": newly_unavailable,
        "to_date": _round(report(eligible, unavailable)),
        "note": ("edge-v1（印の中の5組）の前向きの記録（observe-only）。p は p_pre。判定は §5 の見る時点でだけ行い、"
                 "途中の数値は報告だけ。的中・払戻は shadow で、正式成績・ROI・チャットの参考券には入れない。"),
    }
    if simulate:
        summary["simulation_note"] = ("今の入力（HEAD の結果）のまま判定の日付だけを変えた計算。その日時点に存在した入力の再生ではない。"
                                      "checkpoints.json・ledger.json は書かない。")
    common.write_json(out_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="edge-loop の試験の版 edge-v1 の前向きの記録（observe-only）")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--as-of", type=datetime.date.fromisoformat, default=None,
                        help="判定の日付（JST）。指定すると今の入力のまま日付だけを変えた計算になり、--out で別の出力先が要る")
    args = parser.parse_args(argv)
    if args.as_of is not None and (args.out is None or args.out.resolve() == OUT_DIR.resolve()):
        parser.error("--as-of は本番の出力先に書かないよう、--out で別の出力先を指定する")
    logging.basicConfig(level=logging.INFO)
    summary = run(args.repo, args.out, args.as_of, simulate=args.as_of is not None)
    print(json.dumps({k: summary.get(k) for k in ("status", "warnings", "eligible", "evaluated", "excluded")},
                     ensure_ascii=False))
    # 失敗を成功と見誤らないよう、登録の不一致・レースの失敗・固定した判定との不一致は終了コード 1
    if summary.get("warnings"):
        for w in summary["warnings"]:
            logger.warning("edge-v1: %s", w)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
