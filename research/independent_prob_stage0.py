"""Issue #57 Stage 0 監査（読むだけ）：独立した確率推定の研究に入る前の、入力の出所・欠損・時点の確認。

1. 馬ごとの表：race-performance-v1 の発走前の評価（data/shadow/model_evaluation/race-performance-v1/races/）の
   p_champion・q_market・結果に、出所（評価が見た凍結版の SHA-256、main で発走前に snapshot を最後に更新したコミット、
   frozen_at、発走時刻、score の出所、単勝オッズの source_time）と、その凍結版の raw での入力の欠け
   （騎手・調教師の成績・過去走）を付ける。
2. 品質の層別：レースごとに、騎手・調教師の成績が抜けた馬の割合（PR #56 の件）と、凍結から発走までの時間で旗を立て、
   旗のあるレースを除いても初期診断（Issue #57）の数字が変わらないかを見る。記録は直さない・作り直さない（旗だけ）。
3. odds-live の検証（11/7・11/8）：レースと時点（pre・close・post1・post2）と券種ごとに、状態・source_time が
   発走前か（pre・close）・post2 の final_check、結果のあるレースでは post2 の確定前オッズと払戻の一致。
   ファイルがあることを「取れた」「確定オッズ」とはみなさない。

出力は data/research/independent_prob/stage0/ にだけ書く。予想・正式成績・事前登録した評価は変えない。
数字は探索的な記述で、優位性の結論は出さない（Issue #57）。
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

from research import common
from research import market_audit as ma

OUT_DIR = common.ROOT / "data" / "research" / "independent_prob" / "stage0"
EVAL_DIR_REL = "data/shadow/model_evaluation/race-performance-v1/races"
RESULTS_REL = "data/race_results.json"
ODDS_LIVE_REF = "origin/odds-live"
ODDS_LIVE_DAYS = ("20261107", "20261108")
LEADING_FLAG_SHARE = 0.3          # 騎手か調教師の成績が抜けた馬がこの割合を超えたら旗
EARLY_FREEZE = datetime.timedelta(hours=4)   # 凍結から発走までがこれより長ければ旗（朝のオッズ、H-010）
RATIO_BINS = ((0.0, 0.5), (0.5, 0.8), (0.8, 1.25), (1.25, 2.0), (2.0, math.inf))
MARKETS = {"3連複": "sanrenpuku", "馬連": "umaren", "ワイド": "wide"}
EPS = 1e-12


def _git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=120)


def _iso(value: datetime.datetime | None) -> str | None:
    return ma._iso(value)


# ------------------------------------------------------------------ 1. 馬ごとの表

def race_inputs(race_id: str, repo: Path) -> dict[str, Any]:
    """main で発走前に snapshot を最後に更新したコミットと、その raw の入力の欠け。"""
    found = ma.prerace_versions(race_id, repo)
    if not found["versions"]:
        return {"status": "no_prerace_commit", "post": found["post"]}
    final = found["versions"][-1]
    snap = final["snapshot"]
    race = ma.raw_race(final["sha"], snap.get("week_id"), race_id, repo)
    entries = {int(e["num"]): e for e in (race or {}).get("entries") or [] if isinstance(e.get("num"), int)}
    meta = ((race or {}).get("odds_market_meta") or {}).get("markets") or {}
    return {"status": "ok", "post": found["post"], "commit": final["sha"], "committed_at": final["committed_at"],
            "frozen_at": snap.get("frozen_at"), "entries": entries,
            "win_source_time": (meta.get("win") or {}).get("source_time"),
            "collection_leading": (((race or {}).get("_week_report") or {}).get("leading"))}


def horse_table(repo: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    horses, races = [], []
    for path in sorted((repo / EVAL_DIR_REL).glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        rid = rec["race_id"]
        prob = rec.get("probability") or {}
        snap_meta = ((rec.get("models") or {}).get("champion") or {}).get("snapshot") or {}
        inp = race_inputs(rid, repo)
        post = inp.get("post")
        frozen = common.parse_dt(rec.get("frozen_at"))
        entries = inp.get("entries") or {}
        field = len(entries)
        miss_j = sorted(n for n, e in entries.items() if not e.get("jockey_stats"))
        miss_t = sorted(n for n, e in entries.items() if not e.get("trainer_stats"))
        miss_p = sorted(n for n, e in entries.items() if not e.get("past_runs"))
        flags = []
        if field and max(len(miss_j), len(miss_t)) / field > LEADING_FLAG_SHARE:
            flags.append("leading_stats_partial")
        if post is not None and frozen is not None and post - frozen > EARLY_FREEZE:
            flags.append("early_freeze")
        market = (prob.get("market") or {}).get("status")
        if market != "available":
            flags.append(f"market:{market}")
        race_row = {
            "race_id": rid, "race_day": rid[:8], "status": rec.get("status"), "probability_status": prob.get("status"),
            "score_source": prob.get("score_source"), "temperature": prob.get("temperature"),
            "snapshot_sha256": snap_meta.get("sha256"), "prerace_commit": inp.get("commit"),
            "frozen_at": rec.get("frozen_at"), "post_at": _iso(post),
            "freeze_to_post_minutes": int((post - frozen).total_seconds() // 60) if post and frozen else None,
            "win_source_time": inp.get("win_source_time"), "field": field,
            "missing": {"jockey_stats": miss_j, "trainer_stats": miss_t, "past_runs": miss_p},
            "log_loss": {"champion": ((prob.get("models") or {}).get("champion") or {}).get("log_loss"),
                         "market": (prob.get("market") or {}).get("log_loss")},
            "flags": flags,
        }
        races.append(race_row)
        for h in prob.get("horses") or []:
            e = entries.get(int(h["num"]), {})
            horses.append({
                "race_id": rid, "race_day": rid[:8], "num": h["num"], "y": h["y"],
                "p_champion": h.get("p_champion"), "q_market": h.get("p_q_market"),
                "win_odds_at_freeze": e.get("win_odds"),
                "has_jockey_stats": bool(e.get("jockey_stats")), "has_trainer_stats": bool(e.get("trainer_stats")),
                "past_runs": len(e.get("past_runs") or []),
                "race_flags": flags,
            })
    return horses, races


# ------------------------------------------------------------------ 2. 層別

def diagnostics(horses: list[dict[str, Any]], races: list[dict[str, Any]], exclude_flags: set[str]) -> dict[str, Any]:
    keep = {r["race_id"] for r in races
            if r["log_loss"]["market"] is not None and not exclude_flags & set(r["flags"])}
    rows = [h for h in horses if h["race_id"] in keep and h["q_market"]]
    rr = [r for r in races if r["race_id"] in keep]
    out: dict[str, Any] = {"excluded_flags": sorted(exclude_flags), "races": len(rr), "horses": len(rows)}
    if not rr:
        return out
    lc = sum(r["log_loss"]["champion"] for r in rr) / len(rr)
    lm = sum(r["log_loss"]["market"] for r in rr) / len(rr)
    out["mean_log_loss"] = {"champion": round(lc, 4), "market": round(lm, 4), "champion_minus_market": round(lc - lm, 4)}
    out["races_model_better"] = sum(1 for r in rr if r["log_loss"]["champion"] < r["log_loss"]["market"])
    bins = []
    for lo, hi in RATIO_BINS:
        b = [h for h in rows if lo <= h["p_champion"] / max(h["q_market"], EPS) < hi]
        bins.append({"p_over_q": f"{lo}–{hi if hi != math.inf else '∞'}", "horses": len(b),
                     "expected_wins_market": round(sum(h["q_market"] for h in b), 3),
                     "expected_wins_model": round(sum(h["p_champion"] for h in b), 3),
                     "actual_wins": sum(h["y"] for h in b)})
    out["p_over_q_bins"] = bins
    return out


# ------------------------------------------------------------------ 3. odds-live

def _ol_files(repo: Path, day: str) -> list[str]:
    out = _git(["ls-tree", "-r", "--name-only", ODDS_LIVE_REF, f"{day}/"], repo)
    return [f for f in out.stdout.decode().split() if f.endswith(".json.gz")]


def _ol_read(repo: Path, rel: str) -> dict[str, Any]:
    return json.loads(gzip.decompress(_git(["show", f"{ODDS_LIVE_REF}:{rel}"], repo).stdout))


def odds_live_check(repo: Path, results: dict[str, Any]) -> dict[str, Any]:
    days = {}
    for day in ODDS_LIVE_DAYS:
        races: dict[str, dict[str, Any]] = {}
        for rel in _ol_files(repo, day):
            d = _ol_read(repo, rel)
            race = races.setdefault(d["race_key"], {"race_key": d["race_key"], "post_at": d.get("post_at"), "phases": {}})
            post = common.parse_dt(d.get("post_at"))
            markets = {}
            for name, m in (d.get("markets") or {}).items():
                st = common.parse_dt(m.get("source_time_jst"))
                markets[name] = {"status": m.get("status"), "rows": m.get("rows"), "source_time": m.get("source_time_jst"),
                                 "source_before_post": (st < post) if (st and post) else None}
            entry = {"status": d.get("status"), "reason": d.get("reason"), "markets": markets}
            if d.get("phase") == "post2":
                entry["final_check"] = {"verified": (d.get("final_check") or {}).get("verified"),
                                        "basis": (d.get("final_check") or {}).get("basis")}
                res = results.get(d["race_key"])
                if res:
                    entry["payout_match"] = _payout_match(d, res)
            race["phases"][d["phase"]] = entry
        problems = []
        for r in races.values():
            for ph in ("pre", "close"):
                e = r["phases"].get(ph)
                if e and e["status"] == "ok" and any(m["source_before_post"] is False for m in e["markets"].values()):
                    problems.append(f"{r['race_key']}:{ph}:source_time_not_before_post")
            p2 = r["phases"].get("post2")
            if p2 and not (p2.get("final_check") or {}).get("verified"):
                problems.append(f"{r['race_key']}:post2:final_check_not_verified")
            for mk, pm in (p2 or {}).get("payout_match", {}).items():
                if pm.get("match") is False:
                    problems.append(f"{r['race_key']}:post2:{mk}_payout_mismatch")
        counts: dict[str, dict[str, int]] = {}
        for r in races.values():
            for ph, e in r["phases"].items():
                key = e["status"] if e["status"] == "ok" else f"{e['status']}:{e.get('reason')}"
                counts.setdefault(ph, {}).setdefault(key, 0)
                counts[ph][key] += 1
        days[day] = {"races": len(races), "phase_status": counts, "problems": problems,
                     "races_detail": [races[k] for k in sorted(races)]}
    return days


def _payout_match(d: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
    """post2 の確定前オッズ（100円あたり）と、実際の払戻の一致。払戻 ＝ オッズ × 100 なら一致。"""
    out = {}
    for jp, key in MARKETS.items():
        divs = (res.get("dividends") or {}).get(jp) or []
        odds = ((d.get("markets") or {}).get(key) or {}).get("odds") or {}
        rows = []
        for div in divs:
            horses = sorted(int(h) for h in div.get("horses") or [])
            combo = "-".join(map(str, horses))
            o = odds.get(combo)
            pay = int(div["pay"])
            if o is None:
                check = "combo_not_listed"          # 取得元の表に無い組（人気の無い組は載らない）。価格は分からない
            elif isinstance(o, list) and len(o) == 2:  # ワイドは [下限, 上限]。払戻がその幅に入るか
                check = "within_range" if round(float(o[0]) * 100) <= pay <= round(float(o[1]) * 100) else "outside_range"
            else:
                check = "match" if round(float(o) * 100) == pay else "mismatch"
            rows.append({"combo": combo, "pay": pay, "odds": o, "check": check})
        if rows:
            checks = {r["check"] for r in rows}
            out[jp] = {"rows": rows,
                       "match": None if "combo_not_listed" in checks else not ({"mismatch", "outside_range"} & checks)}
    return out


# ------------------------------------------------------------------ 実行

def run(repo: Path | None = None, out_dir: Path | None = None) -> dict[str, Any]:
    repo = repo or common.ROOT
    out_dir = out_dir or OUT_DIR
    results = json.loads((repo / RESULTS_REL).read_text(encoding="utf-8"))
    horses, races = horse_table(repo)
    strata = [diagnostics(horses, races, set()),
              diagnostics(horses, races, {"leading_stats_partial"}),
              diagnostics(horses, races, {"early_freeze"}),
              diagnostics(horses, races, {"leading_stats_partial", "early_freeze"})]
    ol = odds_live_check(repo, results)
    missing = {k: sum(1 for h in horses if not h[k]) for k in ("has_jockey_stats", "has_trainer_stats")}
    summary = {
        "schema": "independent-prob-stage0-v1", "issue": 57,
        "generated_at": datetime.datetime.now(common.JST).isoformat(timespec="seconds"),
        "used_for_prediction": False, "official_results_untouched": True,
        "inputs": {"evaluation": EVAL_DIR_REL, "results": RESULTS_REL, "odds_live": ODDS_LIVE_REF},
        "races": len(races), "horses": len(horses),
        "missing_horses": {"jockey_stats": missing["has_jockey_stats"], "trainer_stats": missing["has_trainer_stats"],
                           "past_runs_zero": sum(1 for h in horses if h["past_runs"] == 0)},
        "flagged_races": {r["race_id"]: r["flags"] for r in races if r["flags"]},
        "strata": strata,
        "odds_live": {day: {k: v for k, v in d.items() if k != "races_detail"} for day, d in ol.items()},
        "note": ("探索的な記述（Issue #57 Stage 0）。勝率の比較で、3連複の確率ではない。旗は記録を直すためではなく層別のため。"
                 "優位性・有意性の結論は出さない。"),
    }
    common.write_json(out_dir / "horses.json", horses)
    common.write_json(out_dir / "races.json", races)
    common.write_json(out_dir / "odds_live.json", ol)
    common.write_json(out_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Issue #57 Stage 0 監査（読むだけ）")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)
    s = run(out_dir=args.out)
    print(json.dumps({k: s[k] for k in ("races", "horses", "missing_horses", "flagged_races")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
