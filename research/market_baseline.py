"""市場だけで作る確率の基準線（market-baseline-v1、observe-only）。Issue #39 の PR-B。

次の確率モデル（#39 案1：市場＋能力モデルの混合）が勝たなければならない「床」を、
モデルを一切使わずに発走前の市場オッズだけで作り、結果の出たレースで proper な指標を測る。

入力（すべて発走前の版に固定。research/market_audit.py の固定を再利用）:
    raw は「発走前に snapshot を最後に更新したコミット」の版。3連複・単勝ともオッズの時点が
    pre_race_verified（source_time・observed_at が記録され、発走前で、観測の順序も正しい）のレースだけを使う。
    時点が unknown のレース（10/3・10/4）や、発走前のコミットが無いレース（9月）は使わず、除外理由を残す。

作る確率分布（どれも「どの3頭が3着以内に入るか」= 3連複の組の分布。全組で合計 1）:
    market_trio        3連複オッズそのもの。収録されている組は (1/odds)/Z（Z = 1/払戻率）。
                       収録されていない組（1000倍超など）には、残りの質量 1 − Σ を Harville の形で配る（0 にしない）。
                       残りが負や上限超えのときは丸めて、その方法を記録する。
    harville           単勝オッズを正規化した勝率 p から、素の Harville（＝素の Plackett–Luce）で作る。
    discounted_harville 2着・3着の選び方に p^λ2・p^λ3 を使う Harville の補正版。λ は文献値の固定（当てはめない）。
    Henery・Stern などの他の補正式は、モンテカルロの誤差と依存ライブラリの都合でこの PR では入れない。

測る指標（結果が出たレースだけ）:
    trio_logloss  実際の3着以内の組に付けた確率の −log（全組で正規化した分布なので proper）
    top3_brier    各馬の「3着以内に入る確率」（分布の周辺確率）の Brier の平均
    win_logloss   単勝オッズを正規化した勝率での勝ち馬の −log（参考）
    actual_trio_rank 実際の組が確率の高い順で何番目か（参考）
    モデル間の差は同じレースで対にした平均と標準誤差を出すが、有意性は判定しない（significance: not_assessed）。

出力は data/research/market_baseline/ にだけ書く。予想・正式成績・UI・既存の shadow 採点は変えない。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import itertools
import json
import logging
import math
import statistics
import sys
from pathlib import Path
from typing import Any

from research import common
from research import market_audit as ma

logger = logging.getLogger("research.market_baseline")

CONFIG_PATH = common.ROOT / "config" / "market_baseline_v1.json"
OUT_DIR = common.ROOT / "data" / "research" / "market_baseline"
RACE_SCHEMA = "market-baseline-race-v1"
SUMMARY_SCHEMA = "market-baseline-summary-v1"
MODELS = ("market_trio", "harville", "discounted_harville")
BASE_MODEL = "market_trio"
Trio = tuple[int, int, int]


def load_config(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or CONFIG_PATH).read_text(encoding="utf-8"))


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 勝率

def win_probs(race: dict[str, Any]) -> dict[int, float]:
    """単勝オッズの逆数を正規化した勝率。オッズの無い馬（取消など）は含めない。"""
    inv = {}
    for e in race.get("entries") or []:
        num, odds = e.get("num"), e.get("win_odds")
        if isinstance(num, int) and isinstance(odds, (int, float)) and odds > 0 and math.isfinite(odds):
            inv[num] = 1.0 / float(odds)
    total = sum(inv.values())
    return {n: v / total for n, v in inv.items()} if total > 0 else {}


# ------------------------------------------------------------------ Harville 系

def harville_trio(p: dict[int, float], lambdas: tuple[float, float] = (1.0, 1.0)) -> dict[Trio, float]:
    """3頭の組が（順不同で）3着以内に入る確率。全組で合計 1。"""
    horses = sorted(p)
    # 1着→2着→3着の順に、残りの馬の中から選ぶ確率の積を、6通りの並びで足す。
    # 分母（残りの馬の p^λ の和）は、先に全体の和を作ってから引く
    lam2, lam3 = lambdas
    s2 = sum(p[h] ** lam2 for h in horses)
    s3 = sum(p[h] ** lam3 for h in horses)
    out: dict[Trio, float] = {}
    for trio in itertools.combinations(horses, 3):
        total = 0.0
        for a, b, c in itertools.permutations(trio):
            d2 = s2 - p[a] ** lam2
            d3 = s3 - p[a] ** lam3 - p[b] ** lam3
            if d2 > 0 and d3 > 0:
                total += p[a] * (p[b] ** lam2 / d2) * (p[c] ** lam3 / d3)
        out[trio] = total
    return _normalise(out)


def _normalise(dist: dict[Trio, float]) -> dict[Trio, float]:
    total = sum(dist.values())
    return {k: v / total for k, v in dist.items()} if total > 0 else dist


# ------------------------------------------------------------------ 市場の3連複分布

def market_trio(race: dict[str, Any], runners: list[int], payout: float,
                shape: dict[Trio, float]) -> tuple[dict[Trio, float], dict[str, Any]]:
    """3連複オッズから、全組で合計 1 の分布を作る。

    収録組：p(t) = (1/odds)/Z、Z = 1/払戻率。
    未収録組：残りの質量 R = 1 − Σ を、shape（Harville）の比で配る。未収録の組のオッズは収録の最大値以上なので、
    1組あたりの上限 u = 1/(最大オッズ×Z) を置き、R は [0, 未収録数×u] に丸める。
    R ≤ 0 のとき（払戻率の仮定より市場の合計が大きい）は、未収録組に shape の質量と上限の小さい方を割り当て、
    収録組をその分だけ縮める。どの経路を通ったかを記録する。
    """
    table, invalid = ma._trio_parse(race)
    allowed = set(runners)
    listed = {t: v for t, v in table.items() if set(t) <= allowed}
    dropped_scratched = len(table) - len(listed)
    z = 1.0 / payout
    p_listed = {t: (1.0 / odds) / z for t, odds in listed.items()}
    s = sum(p_listed.values())
    all_trios = list(itertools.combinations(sorted(runners), 3))
    unlisted = [t for t in all_trios if t not in p_listed]
    info: dict[str, Any] = {"listed": len(p_listed), "unlisted": len(unlisted), "total": len(all_trios),
                            "invalid_rows": invalid, "dropped_rows_with_runner_without_odds": dropped_scratched,
                            "listed_mass_before_normalisation": round(s, 5)}
    if not p_listed:
        info["method"] = "no_trio_odds"
        return {}, info
    if not unlisted:
        info.update(method="complete_listed_renormalised", residual_mass=0.0)
        return _normalise(p_listed), info
    cap = max(listed.values())
    u = 1.0 / (cap * z)
    upper = len(unlisted) * u
    r = 1.0 - s
    shape_u = sum(shape.get(t, 0.0) for t in unlisted)
    if r <= 0:
        r_eff = min(shape_u, upper)
        method = "residual_negative_used_shape_mass"
    elif r > upper:
        r_eff = upper
        method = "residual_clamped_to_cap"
    else:
        r_eff = r
        method = "residual_from_payout"
    scale = (1.0 - r_eff) / s
    dist = {t: v * scale for t, v in p_listed.items()}
    dist.update(_capped_fill(unlisted, shape, r_eff, u))
    info.update(method=method, residual_mass=round(r_eff, 5), unlisted_cap_per_trio=round(u, 7),
                max_listed_odds=cap)
    return _normalise(dist), info


def _capped_fill(trios: list[Trio], shape: dict[Trio, float], mass: float, cap: float) -> dict[Trio, float]:
    """mass を shape の比で配る。ただし1組あたり cap を超えない（超えた分は残りの組に配り直す）。

    未収録の組のオッズは収録の最大値以上なので、1組の確率は cap = 1/(最大オッズ×Z) 以下のはず。
    mass ≤ 組数×cap の範囲で呼ぶ前提（呼び出し側で丸めている）。
    """
    out = {t: 0.0 for t in trios}
    free = list(trios)
    left = mass
    while left > 1e-15 and free:
        weights = {t: max(shape.get(t, 0.0), 0.0) for t in free}
        total = sum(weights.values())
        share = ({t: left * w / total for t, w in weights.items()} if total > 0
                 else {t: left / len(free) for t in free})
        capped = [t for t in free if out[t] + share[t] >= cap]
        if not capped:
            for t in free:
                out[t] += share[t]
            break
        for t in capped:
            left -= cap - out[t]
            out[t] = cap
        free = [t for t in free if t not in capped]
    return out


# ------------------------------------------------------------------ 指標

def top3_marginals(dist: dict[Trio, float]) -> dict[int, float]:
    out: dict[int, float] = {}
    for trio, p in dist.items():
        for h in trio:
            out[h] = out.get(h, 0.0) + p
    return out


def metrics(dist: dict[Trio, float], top3: Trio, runners: list[int], eps: float) -> dict[str, Any]:
    p_actual = dist.get(top3, 0.0)
    marg = top3_marginals(dist)
    brier = statistics.mean((marg.get(h, 0.0) - (1.0 if h in top3 else 0.0)) ** 2 for h in runners)
    ranked = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
    rank = next((i + 1 for i, (t, _) in enumerate(ranked) if t == top3), None)
    top5 = ranked[:5]
    return {"p_actual_trio": round(p_actual, 7),
            "trio_logloss": round(-math.log(max(p_actual, eps)), 6),
            "top3_brier": round(brier, 6),
            "actual_trio_rank": rank,
            "top5_hit": any(t == top3 for t, _ in top5),
            "top5_prob_sum": round(sum(p for _, p in top5), 5)}


# ------------------------------------------------------------------ 1レース

def _timing(race: dict[str, Any], market: str, post, committed) -> str:
    meta = ((race.get("odds_market_meta") or {}).get("markets") or {}).get(market) or {}
    return ma.odds_timing(ma._source_dt(meta.get("source_time")), common.parse_dt(meta.get("observed_at")),
                          post, committed)["odds_timing_status"]


def baseline_race(race_id: str, result: dict[str, Any] | None, config: dict[str, Any], repo: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"schema": RACE_SCHEMA, "race_id": race_id, "used_for_prediction": False,
                           "official_results_untouched": True}
    found = ma.prerace_versions(race_id, repo)
    post, versions = found["post"], found["versions"]
    out["post_at"] = ma._iso(post)
    if not versions:
        out["status"] = "excluded"
        out["reason"] = "no_prerace_commit"
        return out
    final = versions[-1]
    snap = final["snapshot"]
    race = ma.raw_race(final["sha"], snap.get("week_id"), race_id, repo)
    out["pinned"] = {"commit": final["sha"], "frozen_at": snap.get("frozen_at")}
    if race is None:
        out.update(status="excluded", reason="raw_missing_at_pinned_commit")
        return out
    timing = {"trio": _timing(race, "sanrenpuku", post, final["committed_at"]),
              "win": _timing(race, "win", post, final["committed_at"])}
    out["odds_timing_status"] = timing
    required = config.get("require_timing", "pre_race_verified")
    if any(v != required for v in timing.values()):
        out.update(status="excluded", reason=f"odds_timing_not_{required}")
        return out

    p = win_probs(race)
    runners = sorted(p)
    entries = [e.get("num") for e in race.get("entries") or [] if isinstance(e.get("num"), int)]
    out["runners"] = {"with_win_odds": len(runners), "entries": len(entries),
                      "without_win_odds": sorted(set(entries) - set(runners))}
    if len(runners) < 3:
        out.update(status="excluded", reason="fewer_than_3_runners_with_odds")
        return out
    dh = config["discounted_harville"]
    dists = {"harville": harville_trio(p),
             "discounted_harville": harville_trio(p, (dh["lambda_second"], dh["lambda_third"]))}
    dists["market_trio"], out["market_trio"] = market_trio(race, runners, config["trio_payout"], dists["harville"])
    if not dists["market_trio"]:
        out.update(status="excluded", reason="no_trio_odds")
        return out
    out["distribution_sums"] = {k: round(sum(v.values()), 9) for k, v in dists.items()}

    finish = (result or {}).get("finish") or []
    if len(finish) < 3:
        out["status"] = "pending_result"
        return out
    top3 = tuple(sorted(int(x) for x in finish[:3]))
    if not set(top3) <= set(runners):
        out.update(status="excluded", reason="top3_horse_without_prerace_odds")
        return out
    eps = float(config["eps"])
    out["top3"] = list(top3)
    out["winner"] = int(finish[0])
    out["win_logloss_market"] = round(-math.log(max(p.get(int(finish[0]), 0.0), eps)), 6)
    out["models"] = {name: metrics(dists[name], top3, runners, eps) for name in MODELS}
    out["status"] = "evaluated"
    return out


# ------------------------------------------------------------------ 集計

def _paired(rows: list[dict[str, Any]], model: str, key: str) -> dict[str, Any]:
    diffs = [r["models"][model][key] - r["models"][BASE_MODEL][key] for r in rows]
    if not diffs:
        return {"n": 0}
    return {"n": len(diffs), "mean_diff_vs_market_trio": round(statistics.mean(diffs), 6),
            "se": round(statistics.stdev(diffs) / math.sqrt(len(diffs)), 6) if len(diffs) >= 2 else None,
            "model_better": sum(d < 0 for d in diffs), "market_trio_better": sum(d > 0 for d in diffs)}


def summarize(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    ev = [r for r in rows if r.get("status") == "evaluated"]
    per_model = {}
    for m in MODELS:
        per_model[m] = {
            "races": len(ev),
            "mean_trio_logloss": round(statistics.mean(r["models"][m]["trio_logloss"] for r in ev), 6) if ev else None,
            "mean_top3_brier": round(statistics.mean(r["models"][m]["top3_brier"] for r in ev), 6) if ev else None,
            "top5_hit_races": sum(r["models"][m]["top5_hit"] for r in ev),
            "median_actual_trio_rank": statistics.median(r["models"][m]["actual_trio_rank"] for r in ev) if ev else None,
        }
    paired = {m: {"trio_logloss": _paired(ev, m, "trio_logloss"), "top3_brier": _paired(ev, m, "top3_brier")}
              for m in MODELS if m != BASE_MODEL}
    excluded: dict[str, list[str]] = {}
    for r in rows:
        if r.get("status") == "excluded":
            excluded.setdefault(r["reason"], []).append(r["race_id"])
    return {
        "races": len(rows),
        "evaluated": len(ev),
        "pending_result": [r["race_id"] for r in rows if r.get("status") == "pending_result"],
        "excluded": excluded,
        "mean_win_logloss_market": round(statistics.mean(r["win_logloss_market"] for r in ev), 6) if ev else None,
        "models": per_model,
        "paired_vs_market_trio": paired,
        "significance": "not_assessed",
        "market_trio_methods": sorted({r["market_trio"]["method"] for r in ev}),
    }


def run(repo: Path | None = None, out_dir: Path | None = None, config: dict[str, Any] | None = None,
        results_path: str = "data/race_results.json") -> dict[str, Any]:
    repo = repo or common.ROOT
    out_dir = out_dir or OUT_DIR
    config = config or load_config()
    results = ma._head_json(results_path, repo) or {}
    rows = []
    for race_id in ma._race_ids(repo):
        try:
            row = baseline_race(race_id, results.get(race_id), config, repo)
        except Exception as exc:  # 1レースの失敗で止めない。理由は型名だけ
            logger.exception("基準線の計算に失敗: %s", race_id)
            row = {"schema": RACE_SCHEMA, "race_id": race_id, "status": "error", "error_type": type(exc).__name__}
        common.write_json(out_dir / "races" / f"{race_id}.json", row)
        rows.append(row)
    summary = {
        "schema": SUMMARY_SCHEMA,
        "generated_at": datetime.datetime.now(common.JST).isoformat(timespec="seconds"),
        "config_version": config.get("version"),
        "config_hash": config_hash(config),
        "used_for_prediction": False,
        "official_results_untouched": True,
        "note": ("市場だけの基準線（モデル不使用）。すべて発走前に固定した版のオッズで、時点が pre_race_verified のレースだけ。"
                 "3連複の未収録組には残りの質量を Harville の形で配っている（近似）。少数標本なので有意性は判定しない。"),
        **summarize(rows, config),
    }
    common.write_json(out_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="市場だけで作る確率の基準線（observe-only）")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    summary = run(args.repo, args.out)
    print(json.dumps({k: summary[k] for k in ("races", "evaluated", "excluded")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
