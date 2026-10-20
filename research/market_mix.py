"""市場＋能力モデルの混合 market-mix-v1 の当てはめと評価（observe-only）。Issue #39 の PR-C2。

仕様は docs/research/MARKET_MIX_PREREG_V1.md と config/market_mix_v1.json（登録コミットで凍結）。
この実装は仕様に無い選択をしない。仕様の節番号（§）をコメントに残す。

    登録（§1）    main の first-parent 履歴で config が初めて現れたコミットから、登録日時・設定の SHA-256・
                  参照コードの版を Git で導出する。今の仕様ファイルが登録コミットと違えば spec_changed で止まる。
                  参照コード（market_audit・market_baseline）は登録コミットの版を Git から読み込んで使う。
    入力（§3）    発走前に固定した raw・オッズの時点・オッズの無い馬・p_model・結果の契約。
    モデル（§4）  p_mix ∝ exp(a·log p_mkt + b·log p_model)、T1 = harville(p_mix)、T2 = w·market_trio + (1−w)·T1。
    当てはめ（§5）開催日ごとの walk-forward。訓練レースは race_id の昇順で、ある開催日の訓練は
                  その前の開催日の訓練の続きになるので、レースごとの損失の格子を一度だけ作って足し上げる
                  （足す順番は毎回最初から足すのと同じなので、結果も同じ）。
    評価（§6）    主指標 D_r、副指標、30・60 のチェックポイント（報告だけ）、100 または 2027-06-30 での判定、
                  開催日単位の bootstrap。判定は結果を 14 日待ってから1回だけ計算し、decision.json に固定する。
    感度（§7）    未収録組の補い方 18 通りで w を当てはめ直す。

出力は data/research/market_mix/ にだけ書く。予想・正式成績・UI・Champion・既存の shadow 採点は変えない。
取得元のレスポンス本文・URL・リクエスト固有の値は写さない（数値・時刻・Git のハッシュだけ）。
"""
from __future__ import annotations

import argparse
import datetime
import itertools
import json
import logging
import math
import operator
import random
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

from research import common

logger = logging.getLogger("research.market_mix")

OUT_DIR = common.ROOT / "data" / "research" / "market_mix"
SPEC_REL = "docs/research/MARKET_MIX_PREREG_V1.md"
CONFIG_REL = "config/market_mix_v1.json"
REFERENCE_CODE = {"ma": "research/market_audit.py", "mb": "research/market_baseline.py"}
RPS_RACES_DIR = "data/shadow/model_evaluation/race-performance-v1/races"
RESULTS_REL = "data/race_results.json"
RACE_SCHEMA = "market-mix-race-v1"
SUMMARY_SCHEMA = "market-mix-summary-v1"
VERSION = "market-mix-v1"

# ------------------------------------------------------------------ 登録した固定値（§4〜§7。設定と一致することをテストで確かめる）

EPS = 1e-12
TIE_TOL = 1e-12
A_STEPS = range(0, 151)        # a = i/50
B_STEPS = range(-100, 101)     # b = j/50
W_STEPS = range(0, 21)         # w = k/20
MIN_TRAIN_RACES = 10
PRIMARY_PAYOUT = 0.75
VARIANT_PAYOUTS = (0.725, 0.75, 0.775)
VARIANT_SHAPES = ("harville", "discounted_harville", "uniform")
DH_LAMBDAS = (0.81, 0.65)
CHECKPOINTS = (30, 60)
DECISION_RACES = 100
DEADLINE = datetime.date(2027, 6, 30)
RESULT_WAIT_DAYS = 14
BOOT_SEED = 20261019
BOOT_REPS = 10000
BOOT_LOWER_RANK = 250          # 昇順で 1 から数える
BOOT_UPPER_RANK = 9750
MIN_DECISION_RACES = 30
MIN_DECISION_DAYS = 10
P_MODELS = {"primary": "p_champion", "secondary": "p_challenger"}
# 結果の契約（§3.6）で外れたレースは、区間の数え方（§6.2）では数える
RESULT_CONTRACT_REASONS = ("dead_heat_top3", "top3_horse_without_prerace_odds")

Trio = tuple[int, int, int]


def variant_key(payout: float, shape: str, cap: bool) -> str:
    return f"payout={payout}|shape={shape}|cap={'on' if cap else 'off'}"


VARIANTS = [(p, s, c) for p in VARIANT_PAYOUTS for s in VARIANT_SHAPES for c in (True, False)]
PRIMARY_VARIANT = variant_key(PRIMARY_PAYOUT, "harville", True)


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
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _main_ref(repo: Path) -> str | None:
    for ref in ("origin/main", "main"):
        if _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo).returncode == 0:
            return ref
    return None


# ------------------------------------------------------------------ 登録（§1）

def registration(repo: Path) -> dict[str, Any]:
    """登録コミット・登録日時・凍結した仕様の SHA-256・参照コードの版を Git から導出する。"""
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
        cur_path = repo / rel
        cur = cur_path.read_bytes() if cur_path.exists() else None
        frozen[rel] = {"registered_sha256": common.sha256_bytes(reg) if reg is not None else None,
                       "current_sha256": common.sha256_bytes(cur) if cur is not None else None,
                       "identical": reg is not None and cur == reg}
    reference = {rel: {"registered_blob": _blob(commit, rel, repo), "current_blob": _blob("HEAD", rel, repo)}
                 for rel in REFERENCE_CODE.values()}
    for v in reference.values():
        v["identical"] = v["registered_blob"] is not None and v["registered_blob"] == v["current_blob"]
    return {
        "status": "ok" if all(f["identical"] for f in frozen.values()) else "spec_changed",
        "ref": ref,
        "commit": commit,
        "registered_at": registered_at.astimezone(common.JST).isoformat(timespec="seconds") if registered_at else None,
        "config_sha256": frozen[CONFIG_REL]["registered_sha256"],
        "spec_sha256": frozen[SPEC_REL]["registered_sha256"],
        "frozen_files": frozen,
        "reference_code": reference,
        "reference_code_used": "registered_commit",
        "note": "Git から導出した記録（出力であって仕様ではない）。仕様の本体は登録コミットの文書と設定。",
    }


def load_reference(repo: Path, commit: str) -> tuple[types.ModuleType, types.ModuleType]:
    """登録コミットの market_audit・market_baseline を読み込む（§1：参照コードは登録コミットの版）。

    その後に参照コードが変わっても、v1 の計算は登録コミットの版で行う。market_baseline が使う
    market_audit も登録コミットの版に差し替える。
    """
    mods: dict[str, types.ModuleType] = {}
    for name, rel in REFERENCE_CODE.items():
        src = _git_bytes(commit, rel, repo)
        if src is None:
            raise FileNotFoundError(f"{rel} が登録コミットに無い")
        mod = types.ModuleType(f"research._registered_{Path(rel).stem}")
        mod.__file__ = f"{commit}:{rel}"
        # リポジトリ自身の、登録コミットに入っているコードだけを実行する
        exec(compile(src, mod.__file__, "exec", dont_inherit=True), mod.__dict__)
        mods[name] = mod
    mods["mb"].ma = mods["ma"]
    return mods["ma"], mods["mb"]


# ------------------------------------------------------------------ 入力（§3）

def _p_model(race_id: str, runners: list[int], repo: Path) -> dict[str, Any]:
    """race-performance-v1 の個票から p_model を読む。契約を満たさなければ reason を返す。"""
    rel = f"{RPS_RACES_DIR}/{race_id}.json"
    doc = _head_json(rel, repo)
    if not isinstance(doc, dict):
        return {"reason": "p_model_file_missing", "source": {"path": rel, "blob_sha": None}}
    champ = (doc.get("models") or {}).get("champion") or {}
    capture = champ.get("capture") or {}
    source = {"path": rel, "blob_sha": _blob("HEAD", rel, repo), "frozen_at": doc.get("frozen_at"),
              "model_id": champ.get("model_id"), "config_hash": doc.get("config_hash"),
              "evaluation_config_hash": doc.get("evaluation_config_hash"),
              "snapshot_sha256": (champ.get("snapshot") or {}).get("sha256"),
              "capture": {"matched": capture.get("matched"), "fidelity": capture.get("fidelity")}}
    horses = {h.get("num"): h for h in (doc.get("probability") or {}).get("horses") or []
              if isinstance(h, dict) and isinstance(h.get("num"), int)}
    raw = {field: {n: horses[n].get(field) for n in runners if n in horses} for field in P_MODELS.values()}
    out: dict[str, Any] = {"source": source, "raw": raw}
    source["horses_with_p_model"] = sum(_valid_p(v) for v in raw["p_champion"].values())
    if doc.get("status") != "evaluated":
        out["reason"] = "p_model_not_evaluated"
    elif doc.get("phase") != "forward":
        out["reason"] = "p_model_not_forward"
    elif capture.get("matched") is not True or capture.get("fidelity") is not True:
        out["reason"] = "p_model_capture_not_verified"
    out["probs"] = {key: _normalised(raw[field], runners) for key, field in P_MODELS.items()}
    if "reason" not in out and out["probs"]["primary"] is None:
        out["reason"] = "p_model_missing_runner"
    return out


def _valid_p(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _normalised(values: dict[int, Any], runners: list[int]) -> dict[int, float] | None:
    """単勝オッズのある全出走馬に p があれば、その馬に限って合計 1 にする（§3.5・§4.1）。"""
    if any(n not in values or not _valid_p(values[n]) for n in runners):
        return None
    total = sum(float(values[n]) for n in runners)
    return {n: float(values[n]) / total for n in runners} if total > 0 else None


def prepare_race(race_id: str, result: dict[str, Any] | None, repo: Path,
                 ma: types.ModuleType, mb: types.ModuleType) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """1レースの契約を確かめ、出力用の行と、計算用のデータ（契約を満たし結果のあるときだけ）を返す。"""
    row: dict[str, Any] = {"schema": RACE_SCHEMA, "race_id": race_id, "race_day": race_id[:8],
                           "used_for_prediction": False, "official_results_untouched": True}
    found = ma.prerace_versions(race_id, repo)
    post, versions = found["post"], found["versions"]
    row["post_at"] = ma._iso(post)
    row["_post"] = post
    if not versions:
        return {**row, "status": "excluded", "reason": "no_prerace_commit"}, None
    final = versions[-1]
    snap = final["snapshot"]
    race = ma.raw_race(final["sha"], snap.get("week_id"), race_id, repo)
    row["pinned"] = {"commit": final["sha"], "frozen_at": snap.get("frozen_at")}
    if race is None:
        return {**row, "status": "excluded", "reason": "raw_missing_at_pinned_commit"}, None
    timing = {"trio": mb._timing(race, "sanrenpuku", post, final["committed_at"]),
              "win": mb._timing(race, "win", post, final["committed_at"])}
    row["odds_timing_status"] = timing
    if any(v != "pre_race_verified" for v in timing.values()):
        return {**row, "status": "excluded", "reason": "odds_timing_not_pre_race_verified"}, None
    p_mkt = mb.win_probs(race)
    runners = sorted(p_mkt)
    entries = [e.get("num") for e in race.get("entries") or [] if isinstance(e.get("num"), int)]
    without = sorted(set(entries) - set(runners))
    row["runners"] = {"with_win_odds": len(runners), "entries": len(entries), "without_win_odds": without}
    if without:
        return {**row, "status": "excluded", "reason": "runner_without_win_odds_unconfirmed"}, None
    if len(runners) < 3:
        return {**row, "status": "excluded", "reason": "fewer_than_3_runners_with_odds"}, None
    trios = list(itertools.combinations(runners, 3))
    shapes = {"harville": mb.harville_trio(p_mkt), "discounted_harville": mb.harville_trio(p_mkt, DH_LAMBDAS),
              "uniform": {t: 1.0 / len(trios) for t in trios}}
    markets, infos = {}, {}
    for payout, shape, cap in VARIANTS:
        key = variant_key(payout, shape, cap)
        markets[key], infos[key] = mb.market_trio(race, runners, payout, shapes[shape], use_cap=cap)
    if not markets[PRIMARY_VARIANT]:
        return {**row, "status": "excluded", "reason": "no_trio_odds"}, None
    row["market_trio"] = {k: infos[PRIMARY_VARIANT].get(k) for k in ("method", "listed", "unlisted", "residual_mass")}

    # §3.5 の p_model は発走前の契約なので、結果の有無と切り離して確かめる。p_model の個票は結果の取り込み後に
    # 作られるので、結果待ちで個票がまだ無いレースは「未確認」（数えない。区間の境界はその日の手前で止める）
    finish = (result or {}).get("finish") or []
    has_result = len(finish) >= 3
    pm = _p_model(race_id, runners, repo)
    row["p_model"] = {"source": pm["source"],
                      "values": {f: {str(n): v for n, v in pm.get("raw", {}).get(f, {}).items()} for f in P_MODELS.values()}}
    if pm.get("reason") == "p_model_file_missing" and not has_result:
        return {**row, "status": "pending_result", "eligibility": "unverified"}, None
    if pm.get("reason"):
        return {**row, "status": "excluded", "reason": pm["reason"], "eligibility": "failed"}, None
    row["eligibility"] = "verified"
    if not has_result:
        return {**row, "status": "pending_result"}, None
    row["result_used"] = {"top3": sorted(int(x) for x in finish[:3]), "winner": int(finish[0]),
                          "trio_dividends": len(((result or {}).get("dividends") or {}).get("3連複") or [])}
    if row["result_used"]["trio_dividends"] >= 2:
        return {**row, "status": "excluded", "reason": "dead_heat_top3"}, None
    top3 = tuple(sorted(int(x) for x in finish[:3]))
    if not set(top3) <= set(runners):
        return {**row, "status": "excluded", "reason": "top3_horse_without_prerace_odds"}, None
    winner = int(finish[0])
    row.update(top3=list(top3), winner=winner, status="ready")
    data = {
        "race_id": race_id, "day": race_id[:8], "post": post, "runners": runners, "top3": top3, "winner": winner,
        "p_mkt": p_mkt, "x": [math.log(p_mkt[n]) for n in runners],
        "y": {k: ([math.log(max(p[n], EPS)) for n in runners] if p else None) for k, p in pm["probs"].items()},
        "market_primary": markets[PRIMARY_VARIANT],
        "market_actual": {k: m.get(top3, 0.0) for k, m in markets.items()},
        "harville_mkt": shapes["harville"],
        "segment": [pm["source"]["model_id"], pm["source"]["config_hash"]],
    }
    data["w_index"] = runners.index(winner)
    return row, data


# ------------------------------------------------------------------ モデル（§4）

def p_mix(x: list[float], y: list[float], a: float, b: float) -> list[float]:
    z = [a * xi + b * yi for xi, yi in zip(x, y)]
    m = max(z)
    e = [math.exp(v - m) for v in z]
    s = sum(e)
    return [v / s for v in e]


def _trio_ll(p: float) -> float:
    return -math.log(max(p, EPS))


def race_ab_terms(race: dict[str, Any], model: str) -> list[float]:
    """1レースの勝ち馬の −log p_mix を、(a, b) の格子の全点で（a の順・b の順に並べて）。"""
    x, y, w = race["x"], race["y"][model], race["w_index"]
    pb = [[math.exp((j / 50) * yi) for yi in y] for j in B_STEPS]
    terms = []
    for i in A_STEPS:
        a = i / 50
        pa = [math.exp(a * xi) for xi in x]
        for j, row_b in zip(B_STEPS, pb):
            # −log p_mix(勝ち馬) = log Σ exp(a·x + b·y) − (a·x_w + b·y_w)
            terms.append(math.log(sum(map(operator.mul, pa, row_b))) - (a * x[w] + (j / 50) * y[w]))
    return terms


def pick_ab(acc: list[float], n: int) -> dict[str, Any]:
    """§4.1：最小値から TIE_TOL 以内を同点とし、|b| → |a−1| → a → b の順で1点に決める。"""
    losses = [v / n for v in acc]
    best = min(losses)
    grid = [(i, j) for i in A_STEPS for j in B_STEPS]
    tied = [g for g, v in zip(grid, losses) if v <= best + TIE_TOL]
    i, j = min(tied, key=lambda g: (abs(g[1]), abs(g[0] - 50), g[0], g[1]))
    return {"a": i / 50, "b": j / 50, "objective": best, "tied_points": len(tied)}


def pick_w(t1: list[float], mkt: list[float]) -> dict[str, Any]:
    """§4.2：訓練レースの平均 −log T2(実際の組)。同点なら大きい w。"""
    losses = {}
    for k in W_STEPS:
        w = k / 20
        s = 0.0
        for t, m in zip(t1, mkt):
            s += _trio_ll(w * m + (1.0 - w) * t)
        losses[k] = s / len(t1)
    best = min(losses.values())
    k = max(k for k, v in losses.items() if v <= best + TIE_TOL)
    return {"w": k / 20, "objective": best}


# ------------------------------------------------------------------ 当てはめ（§5）

def walk_forward(ready: list[dict[str, Any]], eval_days: list[str], model: str,
                 mb: types.ModuleType, variants: bool) -> dict[str, dict[str, Any]]:
    """開催日ごとに a・b・w を当てはめる。訓練は、その日より前の開催日の契約を満たすレース（race_id 昇順）。"""
    pool = sorted((r for r in ready if r["y"][model] is not None), key=lambda r: r["race_id"])
    fits: dict[str, dict[str, Any]] = {}
    acc: list[float] | None = None
    used: list[dict[str, Any]] = []
    k = 0
    for day in sorted(eval_days):
        while k < len(pool) and pool[k]["day"] < day:
            terms = race_ab_terms(pool[k], model)
            acc = terms if acc is None else [s + t for s, t in zip(acc, terms)]
            used.append(pool[k])
            k += 1
        if len(used) < MIN_TRAIN_RACES:
            fits[day] = {"status": "insufficient_training", "n_train": len(used),
                         "train_races": [r["race_id"] for r in used]}
            continue
        ab = pick_ab(acc, len(used))
        t1 = []
        for r in used:
            pm = p_mix(r["x"], r["y"][model], ab["a"], ab["b"])
            t1.append(mb.harville_trio(dict(zip(r["runners"], pm))).get(r["top3"], 0.0))
        keys = [variant_key(*v) for v in VARIANTS] if variants else [PRIMARY_VARIANT]
        w = {key: pick_w(t1, [r["market_actual"][key] for r in used]) for key in keys}
        fits[day] = {"status": "fitted", "n_train": len(used), "train_races": [r["race_id"] for r in used],
                     **ab, "w": w[PRIMARY_VARIANT]["w"], "w_objective": w[PRIMARY_VARIANT]["objective"],
                     "w_variants": {key: v["w"] for key, v in w.items()}}
    return fits


# ------------------------------------------------------------------ 評価（§6）

def _brier(dist: dict[Trio, float], top3: Trio, runners: list[int], mb: types.ModuleType) -> float:
    marg = mb.top3_marginals(dist)
    return sum((marg.get(h, 0.0) - (1.0 if h in top3 else 0.0)) ** 2 for h in runners) / len(runners)


def evaluate_race(r: dict[str, Any], fit: dict[str, Any], model: str, mb: types.ModuleType,
                  variants: bool) -> dict[str, Any]:
    pm = p_mix(r["x"], r["y"][model], fit["a"], fit["b"])
    mix = dict(zip(r["runners"], pm))
    t1 = mb.harville_trio(mix)
    w = fit["w"]
    mkt = r["market_primary"]
    t2 = {t: w * mkt.get(t, 0.0) + (1.0 - w) * t1.get(t, 0.0) for t in set(mkt) | set(t1)}
    top3, winner = r["top3"], r["winner"]
    out = {
        "a": fit["a"], "b": fit["b"], "w": w,
        "trio_logloss": {"T2": _trio_ll(t2.get(top3, 0.0)), "market_trio": _trio_ll(mkt.get(top3, 0.0)),
                         "T1": _trio_ll(t1.get(top3, 0.0)), "harville_mkt": _trio_ll(r["harville_mkt"].get(top3, 0.0))},
        "win_logloss": {"p_mix": _trio_ll(mix[winner]), "p_mkt": _trio_ll(r["p_mkt"][winner])},
        "top3_brier": {"T2": _brier(t2, top3, r["runners"], mb), "market_trio": _brier(mkt, top3, r["runners"], mb)},
    }
    ll = out["trio_logloss"]
    out["D"] = ll["T2"] - ll["market_trio"]
    out["secondary_diffs"] = {"win_logloss": out["win_logloss"]["p_mix"] - out["win_logloss"]["p_mkt"],
                              "top3_brier": out["top3_brier"]["T2"] - out["top3_brier"]["market_trio"],
                              "T1_vs_harville_mkt": ll["T1"] - ll["harville_mkt"]}
    if variants:
        t1_actual = t1.get(top3, 0.0)
        out["D_variants"] = {}
        for key, w_v in fit["w_variants"].items():
            m_v = r["market_actual"][key]
            out["D_variants"][key] = _trio_ll(w_v * m_v + (1.0 - w_v) * t1_actual) - _trio_ll(m_v)
    return out


def bootstrap(day_diffs: list[list[float]]) -> dict[str, Any]:
    """§6.3：開催日の復元抽出（レースに等しい重み）、percentile の 250・9750 番目。"""
    n_day = len(day_diffs)
    if n_day == 0:
        return {"n_day": 0, "lower": None, "upper": None}
    sums = [sum(d) for d in day_diffs]
    counts = [len(d) for d in day_diffs]
    rng = random.Random(BOOT_SEED)
    stats = []
    for _ in range(BOOT_REPS):
        s, c = 0.0, 0
        for _ in range(n_day):
            idx = rng.randrange(n_day)
            s += sums[idx]
            c += counts[idx]
        stats.append(s / c)
    stats.sort()
    return {"n_day": n_day, "replicates": BOOT_REPS, "seed": BOOT_SEED,
            "lower": stats[BOOT_LOWER_RANK - 1], "upper": stats[BOOT_UPPER_RANK - 1]}


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def window_report(rows: list[dict[str, Any]], with_bootstrap: bool = True) -> dict[str, Any]:
    """区間のレースの集計。rows は区間に入るレースの行（race_id 昇順）。"""
    ev = [r for r in rows if r["status"] == "evaluated"]
    by_day: dict[str, list[float]] = {}
    for r in ev:
        by_day.setdefault(r["race_day"], []).append(r["evaluation"]["D"])
    out: dict[str, Any] = {
        "races_in_window": len(rows), "evaluated": len(ev), "n_day": len(by_day),
        "pending_result": [r["race_id"] for r in rows if r["status"] == "pending_result"],
        "excluded_by_result_contract": [r["race_id"] for r in rows if r["status"] == "excluded"],
        "primary_mean_D": _mean([r["evaluation"]["D"] for r in ev]),
        "variants_mean_D": {key: _mean([r["evaluation"]["D_variants"][key] for r in ev])
                            for key in (ev[0]["evaluation"]["D_variants"] if ev else [])},
        "secondary": {m: _mean([r["evaluation"]["secondary_diffs"][m] for r in ev])
                      for m in ("win_logloss", "top3_brier", "T1_vs_harville_mkt")},
        "secondary_p_model_mean_D": _mean([r["secondary_evaluation"]["D"] for r in ev if r.get("secondary_evaluation")]),
        "secondary_p_model_n": sum(1 for r in ev if r.get("secondary_evaluation")),
        "champion_segments": _segments(ev),
    }
    if with_bootstrap:
        out["bootstrap"] = bootstrap([by_day[d] for d in sorted(by_day)])
    return out


def _segments(ev: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    for r in ev:
        src = r["p_model"]["source"]
        key = [src.get("model_id"), src.get("config_hash")]
        if not segs or segs[-1]["key"] != key:
            segs.append({"key": key, "from_day": r["race_day"], "D": []})
        segs[-1]["D"].append(r["evaluation"]["D"])
    return [{"model_id": s["key"][0], "config_hash": s["key"][1], "from_day": s["from_day"], "n": len(s["D"]),
             "mean_D": _mean(s["D"])} for s in segs]


def decide(report: dict[str, Any]) -> str:
    """§6.4：判定不能 → 採用 → 不採用 の順に決める。"""
    n_day = report["bootstrap"]["n_day"]
    if report["evaluated"] < MIN_DECISION_RACES or n_day < MIN_DECISION_DAYS:
        return "undecidable"
    variants = report["variants_mean_D"]
    adopt = (report["primary_mean_D"] < 0 and report["bootstrap"]["upper"] < 0
             and len(variants) == len(VARIANTS) and all(v is not None and v < 0 for v in variants.values()))
    return "adopted" if adopt else "not_adopted"


def decision_state(rows: list[dict[str, Any]], counted_days: list[str], as_of: datetime.date) -> dict[str, Any]:
    """§6.2：区間を開催日単位で切り、チェックポイントと判定の状態を決める。

    数えるのは発走前の契約を確かめたレース（`_counted`）だけ。契約がまだ確かめられないレース（`_unresolved`）の
    ある開催日から先では、区間の境界を決めない。こうすると、境界は個票や結果の届く順番によらず、
    すべてが確かめられたときと同じ開催日になる（PR #43 レビュー P0-1）。
    """
    counted = [r for r in rows if r.get("_counted")]
    unresolved_days = sorted({r["race_day"] for r in rows if r.get("_unresolved")})
    per_day: dict[str, int] = {}
    for r in counted:
        per_day[r["race_day"]] = per_day.get(r["race_day"], 0) + 1
    cum, reached, resolved_through = 0, {}, None
    for day in counted_days:
        if unresolved_days and day >= unresolved_days[0]:
            break
        resolved_through = day
        cum += per_day.get(day, 0)
        for target in (*CHECKPOINTS, DECISION_RACES):
            if cum >= target and target not in reached and _date(day) <= DEADLINE:
                reached[target] = day
    state: dict[str, Any] = {"counted_races": sum(per_day.values()), "reached": reached, "checkpoints": {},
                             "resolved_through": resolved_through,
                             "unresolved": sorted(r["race_id"] for r in rows if r.get("_unresolved"))}
    for target in CHECKPOINTS:
        if target in reached:
            window = [r for r in counted if r["race_day"] <= reached[target]]
            state["checkpoints"][str(target)] = {"end_day": reached[target], "report_only": True,
                                                 **window_report(window)}
    end_day = reached.get(DECISION_RACES)
    if end_day is None and as_of > DEADLINE:
        if any(_date(d) <= DEADLINE for d in unresolved_days):
            state["status"] = "awaiting_eligibility"
            return state
        days = [d for d in counted_days if _date(d) <= DEADLINE]
        end_day = days[-1] if days else None
        if end_day is None:
            state.update(status="undecidable", window=None, reason="no_evaluation_day_by_deadline")
            return state
    if end_day is None:
        state["status"] = ("checkpoint_60" if 60 in reached else "checkpoint_30" if 30 in reached else "collecting")
        return state
    wait_until = _date(end_day) + datetime.timedelta(days=RESULT_WAIT_DAYS)
    state["window"] = {"end_day": end_day, "result_wait_until": wait_until.isoformat()}
    if as_of < wait_until:
        state["status"] = "awaiting_results"
        return state
    window = [r for r in counted if r["race_day"] <= end_day]
    for r in window:
        if r["status"] == "pending_result":
            r["status"], r["reason"] = "excluded", "result_unavailable"
    report = window_report(window)
    state["window"].update(report)
    state["status"] = decide(report)
    return state


def _date(day: str) -> datetime.date:
    return datetime.date(int(day[:4]), int(day[4:6]), int(day[6:8]))


# ------------------------------------------------------------------ 実行

def _p_model_changes(rows: list[dict[str, Any]], out_dir: Path) -> list[dict[str, Any]]:
    """前回の出力から p_model の個票（blob）が変わったレース。値の差と、今の capture の確認結果も残す。"""
    changes = []
    for r in rows:
        cur = (r.get("p_model") or {}).get("source") or {}
        prev_row = common.read_json(out_dir / "races" / f"{r['race_id']}.json") or {}
        prev = (prev_row.get("p_model") or {}).get("source") or {}
        if not prev.get("blob_sha") or not cur.get("blob_sha") or prev["blob_sha"] == cur["blob_sha"]:
            continue
        diff = {}
        for field in P_MODELS.values():
            a = (prev_row.get("p_model") or {}).get("values", {}).get(field) or {}
            b = (r.get("p_model") or {}).get("values", {}).get(field) or {}
            nums = set(a) | set(b)
            diff[field] = {"changed_horses": sorted((n for n in nums if a.get(n) != b.get(n)), key=int),
                           "max_abs_diff": max((abs(a[n] - b[n]) for n in nums if _valid_p(a.get(n)) and _valid_p(b.get(n))),
                                               default=None)}
        changes.append({"race_id": r["race_id"], "previous_blob": prev["blob_sha"], "current_blob": cur["blob_sha"],
                        "values": diff, "capture_now": cur.get("capture"),
                        "previous_frozen_at": prev.get("frozen_at"), "current_frozen_at": cur.get("frozen_at")})
    return changes


def _round(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v) for v in obj]
    return obj


def _canonical_digest(obj: Any) -> str:
    return common.sha256_bytes(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def decision_record(state: dict[str, Any], rows: list[dict[str, Any]], reg: dict[str, Any]) -> dict[str, Any]:
    """確定した判定の記録。入力（区間のレースと各レースの入力の版）と出力（判定と区間の全集計）の digest を付ける。

    次回以降は同じ記録を計算し直して digest を比べる。判定の状態だけでなく、平均・bootstrap・感度・
    評価レースの一覧のどれかが変われば不一致になる（PR #43 レビュー P0-2）。
    """
    window = state["window"]
    end_day = window["end_day"]
    inputs = {}
    for r in sorted(rows, key=lambda r: r["race_id"]):
        if not r.get("_counted") or r["race_day"] > end_day:
            continue
        src = (r.get("p_model") or {}).get("source") or {}
        inputs[r["race_id"]] = {"status": r["status"], "reason": r.get("reason"),
                                "pinned_commit": (r.get("pinned") or {}).get("commit"),
                                "p_model_blob": src.get("blob_sha"), "result_used": r.get("result_used")}
    outputs = _round({"status": state["status"], "window": window})
    return {"registration_commit": reg["commit"], "config_sha256": reg["config_sha256"],
            "end_day": end_day, "races": list(inputs), "inputs": inputs, "outputs": outputs,
            "inputs_digest": _canonical_digest(inputs), "outputs_digest": _canonical_digest(outputs),
            "digest": _canonical_digest({"registration_commit": reg["commit"], "inputs": inputs, "outputs": outputs})}


def compare_decision(fixed: dict[str, Any] | None, current: dict[str, Any] | None) -> dict[str, Any] | None:
    """固定した判定と、今回の計算し直しを比べる。fixed は上書きしない。"""
    if not fixed:
        return None
    if current is None:
        return {"matches": False, "reason": "recomputed_not_final"}
    changed_races = sorted(k for k in set(fixed.get("inputs", {})) | set(current["inputs"])
                           if fixed.get("inputs", {}).get(k) != current["inputs"].get(k))
    return {"matches": fixed.get("digest") == current["digest"],
            "decision_input_changed": fixed.get("inputs_digest") != current["inputs_digest"],
            "decision_output_changed": fixed.get("outputs_digest") != current["outputs_digest"],
            "status_changed": (fixed.get("outputs") or {}).get("status") != current["outputs"]["status"],
            "changed_races": changed_races}


def run(repo: Path | None = None, out_dir: Path | None = None, as_of: datetime.date | None = None,
        reference: tuple[types.ModuleType, types.ModuleType] | None = None, simulate: bool = False) -> dict[str, Any]:
    """simulate=True は、今の入力のまま判定の日付だけを変えた計算（その日時点の入力の再生ではない）。
    decision.json と p_model の変化の記録は書かない。"""
    repo = repo or common.ROOT
    out_dir = out_dir or OUT_DIR
    as_of = as_of or datetime.datetime.now(common.JST).date()
    reg = registration(repo)
    common.write_json(out_dir / "registration.json", reg)
    base = {"schema": SUMMARY_SCHEMA, "version": VERSION,
            "generated_at": datetime.datetime.now(common.JST).isoformat(timespec="seconds"),
            "as_of": as_of.isoformat(),
            "as_of_mode": "simulation_on_current_inputs" if simulate else "live",
            "used_for_prediction": False, "official_results_untouched": True,
            "registration": {k: reg.get(k) for k in ("status", "commit", "registered_at", "config_sha256")}}
    if reg["status"] != "ok":
        summary = {**base, "status": reg["status"], "warnings": [reg["status"]],
                   "note": "登録の導出に失敗したか、凍結した仕様が登録コミットと違うため、評価を出さない（§1）。"}
        common.write_json(out_dir / "summary.json", summary)
        return summary
    registered_at = common.parse_dt(reg["registered_at"])
    ma, mb = reference or load_reference(repo, reg["commit"])
    results = _head_json(RESULTS_REL, repo) or {}

    rows, ready = [], []
    for race_id in ma._race_ids(repo):
        try:
            row, data = prepare_race(race_id, results.get(race_id), repo, ma, mb)
        except Exception as exc:  # 1レースの失敗で止めない。理由は型名だけ
            logger.exception("market-mix の入力の準備に失敗: %s", race_id)
            row, data = {"schema": RACE_SCHEMA, "race_id": race_id, "race_day": race_id[:8], "_post": None,
                         "status": "error", "error_type": type(exc).__name__}, None
        post = row.pop("_post", None)
        row["role"] = "evaluation" if post is not None and registered_at and post > registered_at else "training_only"
        rows.append(row)
        if data is not None:
            ready.append(data)
    by_id = {r["race_id"]: r for r in rows}

    # 発走前の契約が未確認のまま開催日から RESULT_WAIT_DAYS 日たったレースは、確かめられないものとして数えない
    for r in rows:
        if r.get("eligibility") == "unverified" and as_of >= _date(r["race_day"]) + datetime.timedelta(days=RESULT_WAIT_DAYS):
            r.update(status="excluded", reason="eligibility_unverifiable", eligibility="unverifiable")

    # 評価する開催日：登録後に発走し、発走前の契約を満たす（または確認待ちの）レースのある開催日（§5・§6.2）
    eval_days = sorted({r["race_day"] for r in rows if r["role"] == "evaluation"
                        and r.get("eligibility") in ("verified", "unverified")})
    fits = walk_forward(ready, eval_days, "primary", mb, variants=True)
    fits_secondary = walk_forward(ready, eval_days, "secondary", mb, variants=False)

    for r in rows:
        if r["role"] == "training_only":
            if r["status"] == "ready":
                r["status"] = "training_only"
            continue
        fit = fits.get(r["race_day"])
        if fit is None or r.get("eligibility") not in ("verified", "unverified"):
            continue
        if fit["status"] != "fitted":
            r["status"], r["reason"] = "not_evaluated", "insufficient_training"
            continue
        if r["eligibility"] == "verified":
            r["_counted"] = True
        else:
            r["_unresolved"] = True
    for data in ready:
        r = by_id[data["race_id"]]
        if r["status"] != "ready":
            continue
        r["evaluation"] = evaluate_race(data, fits[data["day"]], "primary", mb, variants=True)
        fit2 = fits_secondary.get(data["day"]) or {}
        if data["y"]["secondary"] is not None and fit2.get("status") == "fitted":
            r["secondary_evaluation"] = evaluate_race(data, fit2, "secondary", mb, variants=False)
        r["status"] = "evaluated"

    # p_model の個票の変化は、判定の前に調べる（判定の区間に入るレースの変化を警告にする）
    changes = _p_model_changes(rows, out_dir)
    counted_days = [d for d in eval_days if fits[d]["status"] == "fitted"]
    state = decision_state(rows, counted_days, as_of)
    # §6.2：判定は1回だけ。最初に確定した判定を decision.json に固定し、以後は計算し直した記録と digest で比べる。
    # fixed は上書きしない
    final = state["status"] in ("adopted", "not_adopted", "undecidable") and state.get("window") is not None
    current = decision_record(state, rows, reg) if final else None
    decision_path = out_dir / "decision.json"
    fixed = common.read_json(decision_path)
    if current is not None and not fixed and not simulate:
        fixed = {**current, "decided_on": as_of.isoformat()}
        common.write_json(decision_path, fixed)
    check = compare_decision(fixed, current)
    if changes and not simulate:
        log_path = out_dir / "p_model_source_changes.json"
        history = common.read_json(log_path) or []
        stamp = datetime.datetime.now(common.JST).isoformat(timespec="seconds")
        common.write_json(log_path, history + [{"detected_at": stamp, **c} for c in changes])

    window_races = set((fixed or current or {}).get("races") or [])
    changed_in_window = sorted(c["race_id"] for c in changes if c["race_id"] in window_races)
    errors = sorted(r["race_id"] for r in rows if r["status"] == "error")
    warnings = []
    if check is not None and not check["matches"]:
        warnings.append("decision_recomputed_differs_from_fixed")
    if changed_in_window:
        warnings.append("p_model_source_changed_in_decision_window")
    if errors:
        warnings.append("race_errors")

    for r in rows:
        r.pop("_counted", None)
        r.pop("_unresolved", None)
        common.write_json(out_dir / "races" / f"{r['race_id']}.json", _round(r))
    common.write_json(out_dir / "days.json", _round({"primary": fits, "secondary": fits_secondary}))

    excluded: dict[str, list[str]] = {}
    for r in rows:
        if r["status"] in ("excluded", "not_evaluated", "error"):
            excluded.setdefault(r.get("reason") or r.get("error_type") or r["status"], []).append(r["race_id"])
    evaluated = [r for r in rows if r["status"] == "evaluated"]
    summary = {
        **base,
        "status": state["status"],
        "warnings": warnings,
        "races": len(rows),
        "training_pool": sum(1 for d in ready),
        "evaluated": len(evaluated),
        "pending_result": [r["race_id"] for r in rows if r["status"] == "pending_result"],
        "eligibility_unverified": state["unresolved"],
        "excluded": excluded,
        "evaluation_days": eval_days,
        "decision": _round(state),
        "decision_fixed": fixed,
        "decision_check": check,
        "to_date": _round(window_report(evaluated, with_bootstrap=False)),
        "p_model_source_changed": [c["race_id"] for c in changes],
        "p_model_source_changed_in_decision_window": changed_in_window,
        "significance": "decided_only_at_registered_window",
        "note": ("market-mix-v1 の前向き評価（observe-only）。採否は §6 の判定の時点でだけ決め、途中の数値は報告だけ。"
                 "market_trio の未収録組は推定で補った近似で、市場の真の確率ではない。モデルの優位性や回収率はここから主張しない。"),
    }
    if simulate:
        summary["simulation_note"] = ("今の入力（HEAD の結果・p_model）のまま判定の日付だけを変えた計算。"
                                      "その日時点に存在した入力の再生ではない。decision.json は書かない。")
    common.write_json(out_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="market-mix-v1 の当てはめと評価（observe-only）")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--as-of", type=datetime.date.fromisoformat, default=None,
                        help="判定の日付（JST）。指定すると、今の入力のまま日付だけを変えた計算になり、decision.json を書かない")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    summary = run(args.repo, args.out, args.as_of, simulate=args.as_of is not None)
    print(json.dumps({k: summary.get(k) for k in ("status", "warnings", "races", "training_pool", "evaluated", "excluded")},
                     ensure_ascii=False))
    # 失敗を成功と見誤らないよう、登録の不一致・判定の不一致・レースの失敗は終了コード 1 にする
    if summary["status"] in ("spec_changed", "registration_not_found") or summary.get("warnings"):
        for w in summary.get("warnings") or [summary["status"]]:
            logger.warning("market-mix-v1: %s", w)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
