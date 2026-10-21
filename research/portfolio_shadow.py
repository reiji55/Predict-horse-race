"""3連複ポートフォリオの前向き shadow portfolio-shadow-v1（observe-only）。Issue #39 の PR-E。

仕様は docs/research/PORTFOLIO_SHADOW_PREREG_V1.md と config/portfolio_shadow_v1.json（登録コミットで凍結）。
この実装は仕様に無い選択をしない。仕様の節番号（§）をコメントに残す。

    登録（§1）  main の first-parent 履歴で config が初めて現れたコミットから、登録日時・SHA-256・参照の版を導出する。
                今の仕様ファイルが違えば spec_changed で止まる。組み方のコード（research/portfolio.py）・engine
                （research/market_baseline.py）・固定（research/market_audit.py）とその設定は、登録コミットの版を Git から読む。
    組（§2）    凍結した portfolio_race で、登録後の各レースの組と的中確率を計算する。入力は発走前に固定した版だけなので、
                計算が発走後でも、発走前に決めた組と同じになる。
    指標（§4）  主指標は top_prob の C = Σ(hit − p_hit) / n。副指標は報告だけ。
    判定（§5）  30・60 は報告だけ。100 または 2027-06-30 の区間で、結果を 14 日待ってから1回だけ判定する。

出力は data/research/portfolio_shadow/ にだけ書く。予想・正式成績・UI・Champion・scenario-frame/v1 は変えない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import random
import statistics
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

from research import common

logger = logging.getLogger("research.portfolio_shadow")

OUT_DIR = common.ROOT / "data" / "research" / "portfolio_shadow"
SPEC_REL = "docs/research/PORTFOLIO_SHADOW_PREREG_V1.md"
CONFIG_REL = "config/portfolio_shadow_v1.json"
REFERENCE_CODE = {"ma": "research/market_audit.py", "mb": "research/market_baseline.py", "pf": "research/portfolio.py"}
REFERENCE_CONFIG = {"portfolio": "config/portfolio_v1.json", "baseline": "config/market_baseline_v1.json"}
RESULTS_REL = "data/race_results.json"
RACE_SCHEMA = "portfolio-shadow-race-v1"
SUMMARY_SCHEMA = "portfolio-shadow-summary-v1"
VERSION = "portfolio-shadow-v1"

# ------------------------------------------------------------------ 登録した固定値（設定と一致することをテストで確かめる）

STRATEGIES = ("top_prob", "x_out_2", "exposure_cap_3")
PRIMARY = "top_prob"
RECORDED_ONLY = ("cap3_x_out_2",)
CHECKPOINTS = (30, 60)
DECISION_RACES = 100
DEADLINE = datetime.date(2027, 6, 30)
RESULT_WAIT_DAYS = 14
BOOT_SEED = 20261021
BOOT_REPS = 10000
BOOT_LOWER_RANK = 250
BOOT_UPPER_RANK = 9750
MIN_DECISION_RACES = 30
MIN_DECISION_DAYS = 10
CALIBRATION_TOLERANCE = 0.05
TRIO = "3連複"


# ------------------------------------------------------------------ Git

def _git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=60)


def _git_bytes(rev: str, rel: str, repo: Path) -> bytes | None:
    out = _git(["show", f"{rev}:{rel}"], repo)
    return out.stdout if out.returncode == 0 else None


def _blob(rev: str, rel: str, repo: Path) -> str | None:
    out = _git(["rev-parse", "--verify", "--quiet", f"{rev}:{rel}"], repo)
    if out.returncode != 0:
        return None
    return out.stdout.decode().strip() or None


def _head_json(rel: str, repo: Path) -> Any:
    data = _git_bytes("HEAD", rel, repo)
    try:
        return json.loads(data.decode("utf-8")) if data is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _main_ref(repo: Path) -> str | None:
    for ref in ("origin/main", "main"):
        if _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo).returncode == 0:
            return ref
    return None


# ------------------------------------------------------------------ 登録（§1）

def registration(repo: Path) -> dict[str, Any]:
    ref = _main_ref(repo)
    if ref is None:
        return {"status": "registration_not_found", "reason": "main_ref_missing"}
    out = _git(["log", "--first-parent", "--format=%H %cI", ref, "--", CONFIG_REL], repo)
    lines = [ln for ln in out.stdout.decode().splitlines() if ln.strip()] if out.returncode == 0 else []
    if not lines:
        return {"status": "registration_not_found", "reason": "config_not_in_main_first_parent_history", "ref": ref}
    commit, _, stamp = lines[-1].partition(" ")
    registered_at = common.parse_dt(stamp)
    frozen = {}
    for rel in (SPEC_REL, CONFIG_REL):
        reg = _git_bytes(commit, rel, repo)
        path = repo / rel
        cur = path.read_bytes() if path.exists() else None
        frozen[rel] = {"registered_sha256": common.sha256_bytes(reg) if reg is not None else None,
                       "current_sha256": common.sha256_bytes(cur) if cur is not None else None,
                       "identical": reg is not None and cur == reg}
    reference = {}
    for rel in (*REFERENCE_CODE.values(), *REFERENCE_CONFIG.values()):
        reg_blob, cur_blob = _blob(commit, rel, repo), _blob("HEAD", rel, repo)
        reference[rel] = {"registered_blob": reg_blob, "current_blob": cur_blob,
                          "identical": reg_blob is not None and reg_blob == cur_blob}
    return {
        "status": "ok" if all(f["identical"] for f in frozen.values()) else "spec_changed",
        "ref": ref, "commit": commit,
        "registered_at": registered_at.astimezone(common.JST).isoformat(timespec="seconds") if registered_at else None,
        "config_sha256": frozen[CONFIG_REL]["registered_sha256"],
        "spec_sha256": frozen[SPEC_REL]["registered_sha256"],
        "frozen_files": frozen,
        "reference_files": reference,
        "reference_used": "registered_commit",
        "note": "Git から導出した記録（出力であって仕様ではない）。仕様の本体は登録コミットの文書と設定。",
    }


def load_reference(repo: Path, commit: str) -> dict[str, Any]:
    """登録コミットの参照コード（market_audit・market_baseline・portfolio）と設定を読み込む（§1）。"""
    mods: dict[str, types.ModuleType] = {}
    for name, rel in REFERENCE_CODE.items():
        src = _git_bytes(commit, rel, repo)
        if src is None:
            raise FileNotFoundError(f"{rel} が登録コミットに無い")
        mod = types.ModuleType(f"research._registered_shadow_{Path(rel).stem}")
        mod.__file__ = f"{commit}:{rel}"
        # リポジトリ自身の、登録コミットに入っているコードだけを実行する
        exec(compile(src, mod.__file__, "exec", dont_inherit=True), mod.__dict__)
        mods[name] = mod
    # 参照どうしの import も登録コミットの版に差し替える
    mods["mb"].ma = mods["ma"]
    mods["pf"].ma = mods["ma"]
    mods["pf"].mb = mods["mb"]
    configs = {}
    for name, rel in REFERENCE_CONFIG.items():
        data = _git_bytes(commit, rel, repo)
        if data is None:
            raise FileNotFoundError(f"{rel} が登録コミットに無い")
        configs[name] = json.loads(data.decode("utf-8"))
    return {"ma": mods["ma"], "mb": mods["mb"], "pf": mods["pf"], **configs}


# ------------------------------------------------------------------ 1レース（§2・§3）

def shadow_race(race_id: str, result: dict[str, Any] | None, ref: dict[str, Any], repo: Path,
                registered_at: datetime.datetime) -> dict[str, Any]:
    ma, mb, pf = ref["ma"], ref["mb"], ref["pf"]
    base = pf.portfolio_race(race_id, result, ref["portfolio"], ref["baseline"], repo)
    found = ma.prerace_versions(race_id, repo)
    post = found["post"]
    row: dict[str, Any] = {"schema": RACE_SCHEMA, "race_id": race_id, "race_day": race_id[:8],
                           "post_at": ma._iso(post), "used_for_prediction": False, "official_results_untouched": True,
                           "role": "eligible" if post is not None and post > registered_at else "before_registration",
                           "pinned": base.get("pinned"), "pace": base.get("pace")}
    if base.get("status") == "excluded":
        return {**row, "status": "excluded", "reason": base["reason"], "contract": "prerace"}
    row["x"] = base["x"]
    row["strategies"] = {}
    for name in (*STRATEGIES, *RECORDED_ONLY):
        s = base["strategies"][name]
        row["strategies"][name] = {
            "status": s["status"], "proven_optimal": s["proven_optimal"],
            "tickets": [t["horses"] for t in s.get("tickets") or []],
            "p_hit": s.get("p_hit"), "p_hit_given_x_out": s.get("p_hit_given_x_out"),
            "p_hit_variants": s.get("p_hit_variants"), "stake": s.get("stake"),
            "realized": s.get("realized"), "evaluated": name in STRATEGIES}
    row["scenario_frame_v1"] = [{"record": v["record"], "official_v1_valid": v.get("official_v1_valid"),
                                 "p_hit": v.get("p_hit"), "realized": v.get("realized")}
                                for v in base.get("scenario_frame_v1") or []]
    finish = (result or {}).get("finish") or []
    if len(finish) < 3:
        return {**row, "status": "pending_result"}
    # §3 の結果の契約
    top3 = sorted(int(h) for h in finish[:3])
    trio_divs = ((result or {}).get("dividends") or {}).get(TRIO) or []
    row["result_used"] = {"top3": top3, "trio_dividends": len(trio_divs)}
    if len(trio_divs) >= 2:
        return {**row, "status": "excluded", "reason": "dead_heat_top3", "contract": "result"}
    final = found["versions"][-1]
    race = ma.raw_race(final["sha"], final["snapshot"].get("week_id"), race_id, repo)
    runners = set(mb.win_probs(race)) if race else set()
    if not set(top3) <= runners:
        return {**row, "status": "excluded", "reason": "top3_horse_without_prerace_odds", "contract": "result"}
    row["x_in_top3"] = row["x"]["horse"] in top3
    return {**row, "status": "evaluated"}


# ------------------------------------------------------------------ 集計（§4）

def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _usable(r: dict[str, Any], name: str) -> bool:
    s = r["strategies"][name]
    return s["status"] == "chosen" and s["proven_optimal"] and s.get("realized") is not None


def bootstrap(day_values: list[list[float]]) -> dict[str, Any]:
    """§5.2：開催日の復元抽出（レースに等しい重み）、percentile の 250・9750 番目。"""
    n_day = len(day_values)
    if n_day == 0:
        return {"n_day": 0, "lower": None, "upper": None}
    sums = [sum(d) for d in day_values]
    counts = [len(d) for d in day_values]
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
    return {"n_day": n_day, "replicates": BOOT_REPS, "seed": BOOT_SEED,
            "lower": stats[BOOT_LOWER_RANK - 1], "upper": stats[BOOT_UPPER_RANK - 1]}


def strategy_report(ev: list[dict[str, Any]], name: str) -> dict[str, Any]:
    rows = [r for r in ev if _usable(r, name)]
    excluded = {"not_proven_optimal": sorted(r["race_id"] for r in ev if not r["strategies"][name]["proven_optimal"]),
                "infeasible": sorted(r["race_id"] for r in ev if r["strategies"][name]["status"] != "chosen")}
    hits = [int(r["strategies"][name]["realized"]["hit"]) for r in rows]
    ps = [r["strategies"][name]["p_hit"] for r in rows]
    pays = [r["strategies"][name]["realized"]["payout"] for r in rows]
    stake = sum(r["strategies"][name]["stake"] for r in rows)
    x_out = [r for r in rows if not r["x_in_top3"]]
    variants = [r["strategies"][name]["p_hit_variants"] for r in rows]
    return {
        "races": len(rows),
        "excluded": excluded,
        "C": _mean([h - p for h, p in zip(hits, ps)]),
        "C_with_variant_min": _mean([h - (v or {}).get("min", p) for h, p, v in zip(hits, ps, variants)]),
        "C_with_variant_max": _mean([h - (v or {}).get("max", p) for h, p, v in zip(hits, ps, variants)]),
        "mean_p_hit": _mean(ps),
        "hit_rate": _mean(hits),
        "hits": sum(hits),
        "mean_variant_width": _mean([(v["max"] - v["min"]) if v else None for v in variants]),
        "x_out": {"races": len(x_out),
                  "hit_rate": _mean([int(r["strategies"][name]["realized"]["hit"]) for r in x_out]),
                  "mean_p_hit_given_x_out": _mean([r["strategies"][name]["p_hit_given_x_out"] for r in x_out])},
        "return": {"stake": stake, "payout": sum(pays), "roi": sum(pays) / stake if stake else None,
                   "payout_sd": statistics.pstdev(pays) if len(pays) >= 2 else None},
    }


def window_report(rows: list[dict[str, Any]], with_bootstrap: bool = True) -> dict[str, Any]:
    ev = sorted((r for r in rows if r["status"] == "evaluated"), key=lambda r: r["race_id"])
    primary = [r for r in ev if _usable(r, PRIMARY)]
    by_day: dict[str, list[float]] = {}
    for r in primary:
        s = r["strategies"][PRIMARY]
        by_day.setdefault(r["race_day"], []).append(int(s["realized"]["hit"]) - s["p_hit"])
    v1_pairs = []
    for r in primary:
        for rec in r["scenario_frame_v1"]:
            if rec.get("official_v1_valid") and rec.get("realized") is not None:
                v1_pairs.append((bool(r["strategies"][PRIMARY]["realized"]["hit"]), bool(rec["realized"]["hit"])))
    out: dict[str, Any] = {
        "races_in_window": len(rows), "evaluated": len(ev), "primary_races": len(primary), "n_day": len(by_day),
        "pending_result": sorted(r["race_id"] for r in rows if r["status"] == "pending_result"),
        "excluded_by_result_contract": sorted(r["race_id"] for r in rows if r["status"] == "excluded"),
        "strategies": {name: strategy_report(ev, name) for name in STRATEGIES},
        "x_calibration": _mean([int(r["x_in_top3"]) - r["x"]["p_top3"] for r in ev]),
        "v1_comparison": {"races": len(v1_pairs), "both": sum(a and b for a, b in v1_pairs),
                          "top_prob_only": sum(a and not b for a, b in v1_pairs),
                          "v1_only": sum(b and not a for a, b in v1_pairs),
                          "neither": sum(not a and not b for a, b in v1_pairs)},
    }
    out["primary_C"] = out["strategies"][PRIMARY]["C"]
    if with_bootstrap:
        out["bootstrap"] = bootstrap([by_day[d] for d in sorted(by_day)])
    return out


def decide(report: dict[str, Any]) -> str:
    """§5.3：判定不能 → over / under → calibrated → inconclusive の順。"""
    boot = report["bootstrap"]
    if report["primary_races"] < MIN_DECISION_RACES or boot["n_day"] < MIN_DECISION_DAYS:
        return "undecidable"
    if boot["upper"] < 0:
        return "over"
    if boot["lower"] > 0:
        return "under"
    if abs(report["primary_C"]) <= CALIBRATION_TOLERANCE:
        return "calibrated"
    return "inconclusive"


# ------------------------------------------------------------------ 区間（§5.1）

def _date(day: str) -> datetime.date:
    return datetime.date(int(day[:4]), int(day[4:6]), int(day[6:8]))


def decision_state(rows: list[dict[str, Any]], as_of: datetime.date) -> dict[str, Any]:
    """区間を開催日単位で切る。数えるのは発走前の契約を満たす対象のレース（結果待ちを含む）。

    計算に失敗したレース（契約を満たすか分からない）のある開催日から先では、境界を決めない。
    """
    counted = [r for r in rows if r.get("role") == "eligible"
               and (r["status"] in ("evaluated", "pending_result") or r.get("contract") == "result")]
    unresolved_days = sorted({r["race_day"] for r in rows if r.get("role") == "eligible" and r["status"] == "error"})
    days = sorted({r["race_day"] for r in counted} | set(unresolved_days))
    per_day: dict[str, int] = {}
    for r in counted:
        per_day[r["race_day"]] = per_day.get(r["race_day"], 0) + 1
    cum, reached, resolved_through = 0, {}, None
    for day in days:
        if unresolved_days and day >= unresolved_days[0]:
            break
        resolved_through = day
        cum += per_day.get(day, 0)
        for target in (*CHECKPOINTS, DECISION_RACES):
            if cum >= target and target not in reached and _date(day) <= DEADLINE:
                reached[target] = day
    state: dict[str, Any] = {"counted_races": len(counted), "reached": reached, "resolved_through": resolved_through,
                             "unresolved_days": unresolved_days, "checkpoints": {}}
    for target in CHECKPOINTS:
        if target in reached:
            window = [r for r in counted if r["race_day"] <= reached[target]]
            state["checkpoints"][str(target)] = {"end_day": reached[target], "report_only": True,
                                                 **window_report(window)}
    end_day = reached.get(DECISION_RACES)
    if end_day is None and as_of > DEADLINE:
        if any(_date(d) <= DEADLINE for d in unresolved_days):
            state["status"] = "awaiting_unresolved"
            return state
        before = [d for d in days if _date(d) <= DEADLINE]
        if not before:
            state.update(status="undecidable", window=None, reason="no_eligible_day_by_deadline")
            return state
        end_day = before[-1]
    if end_day is None:
        state["status"] = "checkpoint_60" if 60 in reached else "checkpoint_30" if 30 in reached else "collecting"
        return state
    wait_until = _date(end_day) + datetime.timedelta(days=RESULT_WAIT_DAYS)
    state["window"] = {"end_day": end_day, "result_wait_until": wait_until.isoformat()}
    if as_of < wait_until:
        state["status"] = "awaiting_results"
        return state
    window = [r for r in counted if r["race_day"] <= end_day]
    for r in window:
        if r["status"] == "pending_result":
            r.update(status="excluded", reason="result_unavailable", contract="result")
    report = window_report(window)
    state["window"].update(report)
    state["window"]["races"] = sorted(r["race_id"] for r in window)
    state["status"] = decide(report)
    return state


# ------------------------------------------------------------------ 記録（§6）

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


def decision_record(state: dict[str, Any], rows: list[dict[str, Any]], reg: dict[str, Any]) -> dict[str, Any]:
    window = state["window"]
    by_id = {r["race_id"]: r for r in rows}
    inputs = {rid: {"status": by_id[rid]["status"], "reason": by_id[rid].get("reason"),
                    "pinned_commit": (by_id[rid].get("pinned") or {}).get("commit"),
                    "result_used": by_id[rid].get("result_used")} for rid in window["races"]}
    outputs = _round({"status": state["status"], "window": window})
    return {"registration_commit": reg["commit"], "config_sha256": reg["config_sha256"], "end_day": window["end_day"],
            "inputs": inputs, "outputs": outputs, "inputs_digest": _digest(inputs), "outputs_digest": _digest(outputs),
            "digest": _digest({"registration_commit": reg["commit"], "inputs": inputs, "outputs": outputs})}


def compare_decision(fixed: dict[str, Any] | None, current: dict[str, Any] | None) -> dict[str, Any] | None:
    if not fixed:
        return None
    if current is None:
        return {"matches": False, "reason": "recomputed_not_final"}
    return {"matches": fixed.get("digest") == current["digest"],
            "decision_input_changed": fixed.get("inputs_digest") != current["inputs_digest"],
            "decision_output_changed": fixed.get("outputs_digest") != current["outputs_digest"],
            "changed_races": sorted(k for k in set(fixed.get("inputs", {})) | set(current["inputs"])
                                    if fixed.get("inputs", {}).get(k) != current["inputs"].get(k))}


def update_ledger(state: dict[str, Any], out_dir: Path, as_of: datetime.date, write: bool) -> list[int]:
    """30・60・100 の到達日を最初の値で残す（上書きしない）。違えば events に追記し、違った target を返す。"""
    path = out_dir / "ledger.json"
    ledger = common.read_json(path) or {"boundaries": {}, "events": []}
    ledger["note"] = "区間の到達日を最初に決まった値で残す台帳。値は上書きせず、後からの変化は events に追記する。"
    seen = {json.dumps([e.get("target"), e.get("current")]) for e in ledger["events"]}
    changed = []
    for target, day in sorted(state["reached"].items()):
        old = ledger["boundaries"].get(str(target))
        if old is None:
            ledger["boundaries"][str(target)] = {"end_day": day, "first_seen_on": as_of.isoformat()}
        elif old["end_day"] != day:
            changed.append(target)
    for target, old in ledger["boundaries"].items():
        if int(target) not in state["reached"]:
            changed.append(int(target))
    for target in sorted(set(changed)):
        cur = state["reached"].get(target)
        key = json.dumps([target, cur])
        if key not in seen:
            seen.add(key)
            ledger["events"].append({"type": "boundary_changed", "target": target,
                                     "ledger": ledger["boundaries"][str(target)]["end_day"], "current": cur,
                                     "detected_on": as_of.isoformat()})
    if write:
        common.write_json(path, ledger)
    return sorted(set(changed))


# ------------------------------------------------------------------ 実行

def run(repo: Path | None = None, out_dir: Path | None = None, as_of: datetime.date | None = None,
        reference: dict[str, Any] | None = None, simulate: bool = False) -> dict[str, Any]:
    """simulate=True は今の入力のまま日付だけを変えた計算。本番の出力先には書けず、decision.json・台帳は書かない。"""
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
                   "note": "登録の導出に失敗したか、凍結した仕様が登録コミットと違うため、評価を出さない（§1）。"}
        common.write_json(out_dir / "summary.json", summary)
        return summary
    registered_at = common.parse_dt(reg["registered_at"])
    ref = reference or load_reference(repo, reg["commit"])
    results = _head_json(RESULTS_REL, repo) or {}
    rows = []
    for race_id in ref["ma"]._race_ids(repo):
        try:
            row = shadow_race(race_id, results.get(race_id), ref, repo, registered_at)
        except Exception as exc:  # 1レースの失敗で止めない。理由は型名だけ
            logger.exception("portfolio-shadow の計算に失敗: %s", race_id)
            # 発走時刻が読めないこともあるので、登録日以後の開催日なら対象として扱う（境界を決めない側に倒す）
            row = {"schema": RACE_SCHEMA, "race_id": race_id, "race_day": race_id[:8], "status": "error",
                   "error_type": type(exc).__name__,
                   "role": "eligible" if _date(race_id[:8]) >= registered_at.astimezone(common.JST).date()
                   else "before_registration"}
        rows.append(row)

    state = decision_state(rows, as_of)
    final = state["status"] in ("calibrated", "over", "under", "inconclusive", "undecidable") \
        and state.get("window") is not None
    current = decision_record(state, rows, reg) if final else None
    decision_path = out_dir / "decision.json"
    fixed = common.read_json(decision_path)
    if current is not None and not fixed and not simulate:
        fixed = {**current, "decided_on": as_of.isoformat()}
        common.write_json(decision_path, fixed)
    check = compare_decision(fixed, current)
    boundary_changed = update_ledger(state, out_dir, as_of, write=not simulate)
    errors = [{"race_id": r["race_id"], "error_type": r.get("error_type")} for r in rows if r["status"] == "error"]
    warnings = []
    if errors:
        warnings.append("race_errors")
    if check is not None and not check["matches"]:
        warnings.append("decision_recomputed_differs_from_fixed")
    if boundary_changed:
        warnings.append("boundary_changed")

    for r in rows:
        common.write_json(out_dir / "races" / f"{r['race_id']}.json", _round(r))
    eligible = [r for r in rows if r.get("role") == "eligible"]
    excluded: dict[str, list[str]] = {}
    for r in eligible:
        if r["status"] == "excluded":
            excluded.setdefault(r["reason"], []).append(r["race_id"])
    summary = {
        **base,
        "status": state["status"],
        "warnings": warnings,
        "errors": errors,
        "races": len(rows),
        "eligible": len(eligible),
        "evaluated": sum(1 for r in eligible if r["status"] == "evaluated"),
        "pending_result": sorted(r["race_id"] for r in eligible if r["status"] == "pending_result"),
        "excluded": excluded,
        "decision": _round(state),
        "decision_fixed": fixed,
        "decision_check": check,
        "boundary_changed": boundary_changed,
        "to_date": _round(window_report(eligible, with_bootstrap=False)),
        "note": ("portfolio-shadow-v1 の前向き評価（observe-only）。判定は §5 の判定の時点でだけ決め、途中の数値は報告だけ。"
                 "engine は市場だけの確率で、期待回収で組み方の優劣は決まらない。的中・払戻は shadow で、正式成績・ROI には入れない。"),
    }
    if simulate:
        summary["simulation_note"] = ("今の入力（HEAD の結果）のまま判定の日付だけを変えた計算。その日時点に存在した入力の再生ではない。"
                                      "decision.json・台帳は書かない。")
    common.write_json(out_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="3連複ポートフォリオの前向き shadow（observe-only）")
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
    # 失敗を成功と見誤らないよう、登録の不一致・レースの失敗・判定や台帳との不一致は終了コード 1
    if summary.get("warnings"):
        for w in summary["warnings"]:
            logger.warning("portfolio-shadow-v1: %s", w)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
