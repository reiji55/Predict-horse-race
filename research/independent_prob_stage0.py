"""Issue #57 Stage 0 監査（読むだけ）：独立した確率推定の研究に入る前の、入力の出所・欠損・時点の確認。

1. 出所の照合（レースごと）：race-performance-v1 の発走前の評価（data/shadow/model_evaluation/race-performance-v1/races/）
   が見た凍結版を、main の first-parent 履歴で独立に確かめる。
   - 発走時刻の時点で main に入っていた snapshot の版（first-parent の順で、発走前にコミットされた最後の版）を選ぶ。
     ブランチで発走前に作っても、main に入ったのが発走後なら発走前の版にしない（PR #54 と同じ規則）。
   - その版の生バイトの SHA-256 と評価の SHA-256、race_id、frozen_at、同じコミットの raw の有無と馬の集合、
     capture の入力 raw の指紋（input_raw_hash）、単勝の source_time・observed_at・コミット・発走の順序を照合する。
   - 照合はどれも三値（verified・mismatch・unknown）。全部 verified のレースだけを「監査済み」の集計に入れ、
     記録どおりの集計（as_recorded）とは分けて出す。評価の記録そのものは直さない・作り直さない。
   - Git の時刻（committer date）は操作する人が設定できるので、公開の絶対の証明ではない（順序の確認に使うだけ）。
2. 馬ごとの表：同じ版の raw の出走馬を全頭出す。評価の行が無い馬は p・q・y を null にして理由を付ける。
   分母は、入力の品質（raw の全頭）・確率の評価（評価の行）・市場との比較（市場の確率がある馬）を分ける。
3. 層別と確率の形：旗（騎手か調教師の成績が無い馬の割合・朝の凍結）で分けた集計、温度の感度、勝ち馬の順位、
   市場の q の区分別の表。数字は同じ標本の中の探索的な記述で、最適性・優位性の結論は出さない。
4. odds-live の検証（11/7・11/8）：その日の最後のレース一覧（check が ok）を期待の集合として、
   レース × 時点（pre・close・post1・post2）× 5券種で、欠け・状態・時点（unknown を成功にしない）・
   post2 の final_check（券種ごと）を確かめ、結果のあるレースでは post2 の表と払戻を照らす。
   収集時の状態と点検の結果を分け、点検を全部通った時点だけを audit ok に数える。final_check は保存された判定を
   読み、post1・post2 の表から同じ規則で計算し直して照らす。`stable_after_close` は「締切後に変わらなかった」ことで、
   公式の確定オッズの確認ではない。払戻との照合は観測の点検と分けて数える。
   Git の実行の失敗は止める（空として扱わない）。そのコミットにパスが無いことだけは unknown にする。

再現：既定では 2026-11-12 の報告の固定の参照（main・odds-live のコミット、期間）で読む（RUN）。
入力が固定の参照と違う run には stage0-r1 以外の --run-id が要り、出力は別の場所に分ける（r1 を上書きしない）。
出力は data/research/independent_prob/stage0/ にだけ書く。予想・正式成績・事前登録した評価は変えない。
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

from logic import race_performance as rp
from research import common
from research import market_audit as ma
from scraper.capture_odds_live import FINAL_CHECK_BASIS, SALES_CLOSE

SCHEMA = "independent-prob-stage0-v2"
OUT_DIR = common.ROOT / "data" / "research" / "independent_prob" / "stage0"
EVAL_DIR_REL = "data/shadow/model_evaluation/race-performance-v1/races"
RESULTS_REL = "data/race_results.json"
# 2026-11-12 の報告（Issue #57 のコメント）を再現する固定の参照。後日の再実行は、別の run として参照を指定する。
RUN = {"run_id": "stage0-r1", "main": "050117a4357a9e0fc9002572a819b356621a1864",
       "odds_live": "ef15aac78df7bf436774a7213957dd82e5a1b0f4", "from": "20261010", "to": "20261108",
       "odds_live_days": ["20261107", "20261108"]}
LEADING_FLAG_SHARE = 0.3          # 騎手か調教師の成績が無い馬（どちらか）がこの割合を超えたら旗
EARLY_FREEZE = datetime.timedelta(hours=4)   # 凍結から発走までがこれより長ければ旗（朝のオッズ、H-010）
RATIO_BINS = ((0.0, 0.5), (0.5, 0.8), (0.8, 1.25), (1.25, 2.0), (2.0, math.inf))
Q_BINS = ((0.0, 0.02), (0.02, 0.05), (0.05, 0.1), (0.1, 0.2), (0.2, math.inf))
TEMPERATURE_K = (0.5, 0.8, 0.9, 1.0, 1.05, 1.1, 1.25, 1.5, 2.0)   # p を k 乗して正規化し直す（T ≈ T0 / k）
PHASES = ("pre", "close", "post1", "post2")
OL_MARKETS = ("win", "place", "umaren", "wide", "sanrenpuku")
PAYOUT_MARKETS = {"3連複": "sanrenpuku", "馬連": "umaren", "ワイド": "wide"}
VERIFIED, MISMATCH, UNKNOWN = "verified", "mismatch", "unknown"
EPS = 1e-12


class GitError(RuntimeError):
    """Git の読み出しの失敗。監査は空として続けず止める（fail closed）。"""


def _git(args: list[str], repo: Path, ok_missing: bool = False) -> bytes | None:
    """Git を読む。ok_missing のときだけ「そのコミットにそのパスが無い」を None にする（照合は unknown になる）。
    それ以外の失敗（参照が無い・リポジトリが壊れている・時間切れなど）は GitError で止める。"""
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=120)
    if out.returncode != 0:
        err = out.stderr.decode("utf-8", "replace")
        if ok_missing and ("does not exist in" in err or "exists on disk, but not in" in err):
            return None
        raise GitError(f"git {args[0]} failed ({out.returncode})")
    return out.stdout


def resolve(ref: str, repo: Path) -> str:
    return _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo).decode().strip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(value: datetime.datetime | None) -> str | None:
    return ma._iso(value)


def _json(data: bytes | None) -> Any:
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


# ------------------------------------------------------------------ 1. 出所の照合

def main_versions(rel: str, main: str, repo: Path) -> list[dict[str, Any]]:
    """main の first-parent 履歴で rel を変えたコミット（古い順）と、そのコミットでの生バイト（消したなら None）。"""
    out = _git(["log", "--first-parent", "--format=%H %cI", main, "--", rel], repo).decode()
    rows = []
    for line in reversed(out.splitlines()):
        sha, _, stamp = line.partition(" ")
        rows.append({"sha": sha, "committed_at": common.parse_dt(stamp),
                     "blob": _git(["show", f"{sha}:{rel}"], repo, ok_missing=True)})
    return rows


def version_at_post(rows: list[dict[str, Any]], post: datetime.datetime) -> tuple[dict[str, Any] | None, list[str]]:
    """発走時刻の時点で main に入っていた版：first-parent の順で、発走前にコミットされた最後の版。

    同じ時刻のコミットは first-parent の順で後のものを取る。コミット時刻が順に並んでいなければ、
    どれが発走時刻に入っていたかを時刻から決められないので、issue を返す（順序の照合を unknown にする）。
    """
    issues = []
    times = [r["committed_at"] for r in rows]
    if any(t is None for t in times) or any(b < a for a, b in zip(times, times[1:])):
        issues.append("commit_time_not_monotonic")
    before = [r for r in rows if r["committed_at"] is not None and r["committed_at"] < post]
    if not before:
        issues.append("no_prerace_commit")
        return None, issues
    return before[-1], issues


def raw_race(sha: str, week_id: str, race_id: str, repo: Path) -> dict[str, Any] | None:
    """そのコミットの raw のレース。raw が無い・読めない・レースが無いときは None（照合は unknown）。Git の失敗は止める。"""
    raw = _json(_git(["show", f"{sha}:raw/{week_id}.json"], repo, ok_missing=True))
    if not isinstance(raw, dict):
        return None
    return next((r for r in raw.get("races") or [] if r.get("id") == race_id), None)


def _tri(ok: bool | None) -> str:
    return UNKNOWN if ok is None else VERIFIED if ok else MISMATCH


def _same_time(*values: Any) -> bool | None:
    parsed = [common.parse_dt(v) for v in values]
    if any(p is None for p in parsed):
        return None
    return all(p == parsed[0] for p in parsed)


def provenance(rec: dict[str, Any], main: str, repo: Path) -> dict[str, Any]:
    """1レースの出所の照合。checks は三値、audit_status は verified（全部）・mismatch（どれか）・unverified。"""
    rid = rec.get("race_id")
    post = common.post_at(rid, rec.get("post_time"))
    champ = (rec.get("models") or {}).get("champion") or {}
    ev_snap = champ.get("snapshot") or {}
    prob = rec.get("probability") or {}
    checks: dict[str, str] = {}
    out: dict[str, Any] = {"post": post, "checks": checks, "issues": [], "version": None, "snapshot": None,
                           "raw_race": None}
    checks["evaluation"] = _tri(rec.get("status") == "evaluated" and prob.get("status") == "evaluated")
    if post is None:
        checks["post_time"] = UNKNOWN
        return _finish(out)
    rows = main_versions(f"data/snapshots/{rid}.json", main, repo)
    ver, issues = version_at_post(rows, post)
    out["issues"] = issues
    checks["commit_order"] = UNKNOWN if "commit_time_not_monotonic" in issues else VERIFIED
    if ver is None:
        checks["snapshot_version"] = UNKNOWN
        return _finish(out)
    out["version"] = ver
    snap = _json(ver["blob"])
    out["snapshot"] = snap if isinstance(snap, dict) else None
    snap = out["snapshot"] or {}
    frozen = common.parse_dt(snap.get("frozen_at"))
    # 凍結は発走前で、その版のコミットより後ではない（同じ秒は可）
    checks["snapshot_version"] = _tri(bool(snap) and snap.get("pre_race") is True and frozen is not None
                                      and frozen < post and frozen <= ver["committed_at"])
    digest = _sha256(ver["blob"]) if ver["blob"] is not None else None
    out["snapshot_sha256"] = digest
    ev_sha = ev_snap.get("sha256")
    if ev_sha is None or digest is None:
        checks["snapshot_digest"] = UNKNOWN
    elif ev_sha == digest:
        checks["snapshot_digest"] = VERIFIED
    else:
        checks["snapshot_digest"] = MISMATCH
        earlier = {_sha256(r["blob"]) for r in rows if r is not ver and r["blob"] is not None}
        out["issues"].append("evaluated_other_version" if ev_sha in earlier else "evaluated_unknown_version")
    checks["race_id"] = _tri(snap.get("race_id") == rid if snap else None)
    checks["frozen_at"] = _tri(_same_time(rec.get("frozen_at"), ev_snap.get("frozen_at"), snap.get("frozen_at")))
    race = raw_race(ver["sha"], snap.get("week_id"), rid, repo) if snap.get("week_id") else None
    out["raw_race"] = race
    out["raw_rel"] = f"raw/{snap.get('week_id')}.json" if snap.get("week_id") else None
    checks["raw"] = VERIFIED if race else UNKNOWN
    scored = {int(h["num"]) for h in prob.get("horses") or []}
    marks = {int(m["num"]) for m in snap.get("marks") or [] if isinstance(m.get("num"), int)}
    runners = {int(e["num"]) for e in (race or {}).get("entries") or [] if isinstance(e.get("num"), int)}
    checks["horse_set"] = UNKNOWN if not race else _tri(bool(scored) and scored == marks and marks <= runners)
    in_hash = (((rec.get("models") or {}).get("challenger") or {}).get("race_performance") or {}).get("input_raw_hash")
    checks["input_raw"] = UNKNOWN if not (race and in_hash) else _tri(in_hash == rp.canonical_sha256(race))
    meta = (((race or {}).get("odds_market_meta") or {}).get("markets") or {}).get("win") or {}
    timing = ma.odds_timing(ma._source_dt(meta.get("source_time")), common.parse_dt(meta.get("observed_at")),
                            post, ver["committed_at"])
    out["win_timing"] = timing
    checks["win_odds_timing"] = {"pre_race_verified": VERIFIED, "unknown": UNKNOWN}.get(
        timing["odds_timing_status"], MISMATCH)
    return _finish(out)


def _finish(out: dict[str, Any]) -> dict[str, Any]:
    values = set(out["checks"].values())
    out["audit_status"] = "mismatch" if MISMATCH in values else "verified" if values == {VERIFIED} else "unverified"
    return out


# ------------------------------------------------------------------ 2. 馬ごとの表

def race_tables(rec: dict[str, Any], prov: dict[str, Any], result: dict[str, Any] | None,
                eval_blob_sha: str, eval_rel: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rid = rec["race_id"]
    prob = rec.get("probability") or {}
    race = prov.get("raw_race") or {}
    snap = prov.get("snapshot") or {}
    entries = {int(e["num"]): e for e in race.get("entries") or [] if isinstance(e.get("num"), int)}
    scored = {int(h["num"]): h for h in prob.get("horses") or []}
    marks = {int(m["num"]) for m in snap.get("marks") or [] if isinstance(m.get("num"), int)}
    finish = [int(n) for n in (result or {}).get("finish") or []]
    post = prov.get("post")
    frozen = common.parse_dt(rec.get("frozen_at"))
    field = len(entries)
    miss_j = sorted(n for n, e in entries.items() if not e.get("jockey_stats"))
    miss_t = sorted(n for n, e in entries.items() if not e.get("trainer_stats"))
    miss_any = sorted(set(miss_j) | set(miss_t))
    miss_p = sorted(n for n, e in entries.items() if not e.get("past_runs"))
    flags = []
    if field and len(miss_any) / field > LEADING_FLAG_SHARE:
        flags.append("leading_stats_partial")
    if post is not None and frozen is not None and post - frozen > EARLY_FREEZE:
        flags.append("early_freeze")
    market = (prob.get("market") or {}).get("status")
    if market != "available":
        flags.append(f"market:{market}")
    ver = prov.get("version") or {}
    row = {
        "race_id": rid, "race_day": rid[:8], "audit_status": prov["audit_status"], "checks": prov["checks"],
        "issues": prov["issues"],
        "source": {"evaluation": {"path": eval_rel, "blob_sha256": eval_blob_sha},
                   "snapshot": {"commit": ver.get("sha"), "committed_at": _iso(ver.get("committed_at")),
                                "sha256": prov.get("snapshot_sha256")},
                   "raw": {"path": prov.get("raw_rel"), "commit": ver.get("sha"),
                           "race_sha256": rp.canonical_sha256(race) if race else None},
                   "capture": {"score_source": prob.get("score_source"),
                               "matched": ((rec.get("models") or {}).get("champion") or {}).get("capture")}},
        "evaluation_status": rec.get("status"), "probability_status": prob.get("status"),
        "temperature": prob.get("temperature"),
        "frozen_at": rec.get("frozen_at"), "post_at": _iso(post),
        "freeze_to_post_minutes": int((post - frozen).total_seconds() // 60) if post and frozen else None,
        "win_odds_timing": (prov.get("win_timing") or {}).get("odds_timing_status"),
        "field_raw": field, "scored": len(scored),
        "unscored": sorted(set(entries) - set(scored)),
        "missing": {"jockey_stats": miss_j, "trainer_stats": miss_t, "jockey_or_trainer": miss_any,
                    "past_runs": miss_p},
        "log_loss": {"champion": ((prob.get("models") or {}).get("champion") or {}).get("log_loss"),
                     "market": (prob.get("market") or {}).get("log_loss")},
        "flags": flags,
    }
    horses = []
    for num in sorted(set(entries) | set(scored)):
        e, h = entries.get(num), scored.get(num)
        reason = None
        if h is None:
            reason = "not_in_snapshot_marks" if num not in marks else "not_in_probability"
        elif e is None:
            reason = "not_in_raw"
        horses.append({
            "race_id": rid, "race_day": rid[:8], "num": num, "audit_status": prov["audit_status"],
            "in_raw": e is not None, "in_snapshot_marks": num in marks, "in_probability": h is not None,
            "not_evaluated_reason": reason if h is None else None, "input_note": reason if h is not None else None,
            "in_result_finish": (num in finish) if finish else None,
            "y": h.get("y") if h else None,
            "p_champion": h.get("p_champion") if h else None, "q_market": h.get("p_q_market") if h else None,
            "win_odds_at_freeze": e.get("win_odds") if e else None,
            "has_jockey_stats": bool(e.get("jockey_stats")) if e else None,
            "has_trainer_stats": bool(e.get("trainer_stats")) if e else None,
            "past_runs": len(e.get("past_runs") or []) if e else None,
            "race_flags": flags,
        })
    return row, horses


# ------------------------------------------------------------------ 3. 層別と確率の形

def _rank(value: float, values: list[float]) -> float:
    """順位（同じ値は平均の順位）。1 が最上位。"""
    higher = sum(1 for v in values if v > value)
    ties = sum(1 for v in values if v == value)
    return higher + (ties + 1) / 2


def diagnostics(horses: list[dict[str, Any]], races: list[dict[str, Any]], basis: str,
                exclude_flags: set[str] = frozenset()) -> dict[str, Any]:
    """basis: as_recorded（評価の記録どおり）か audited（出所の照合が全部 verified のレースだけ）。"""
    keep = {r["race_id"] for r in races
            if r["log_loss"]["market"] is not None and r["log_loss"]["champion"] is not None
            and not exclude_flags & set(r["flags"]) and (basis != "audited" or r["audit_status"] == "verified")}
    rows = [h for h in horses if h["race_id"] in keep and h["in_probability"] and h["q_market"] is not None]
    rr = [r for r in races if r["race_id"] in keep]
    out: dict[str, Any] = {"basis": basis, "excluded_flags": sorted(exclude_flags), "races": len(rr),
                           "horses": len(rows)}
    if not rr:
        return out
    lc = sum(r["log_loss"]["champion"] for r in rr) / len(rr)
    lm = sum(r["log_loss"]["market"] for r in rr) / len(rr)
    out["mean_log_loss"] = {"champion": round(lc, 4), "market": round(lm, 4), "champion_minus_market": round(lc - lm, 4)}
    out["races_model_better"] = sum(1 for r in rr if r["log_loss"]["champion"] < r["log_loss"]["market"])
    out["p_over_q_bins"] = [_bin(f"{lo}–{hi if hi != math.inf else '∞'}",
                                 [h for h in rows if lo <= h["p_champion"] / max(h["q_market"], EPS) < hi])
                            for lo, hi in RATIO_BINS]
    out["q_bins"] = [_bin(f"{lo}–{hi if hi != math.inf else '∞'}", [h for h in rows if lo <= h["q_market"] < hi])
                     for lo, hi in Q_BINS]
    by_race: dict[str, list[dict[str, Any]]] = {}
    for h in rows:
        by_race.setdefault(h["race_id"], []).append(h)
    out["temperature_sensitivity"] = _temperature(by_race)
    out["winner_rank"] = _winner_rank(by_race)
    return out


def _bin(label: str, b: list[dict[str, Any]]) -> dict[str, Any]:
    jockey_known = [h for h in b if h["has_jockey_stats"] is not None]
    return {"bin": label, "horses": len(b),
            "expected_wins_market": round(sum(h["q_market"] for h in b), 3),
            "expected_wins_model": round(sum(h["p_champion"] for h in b), 3),
            "actual_wins": sum(h["y"] for h in b),
            "jockey_stats_missing": sum(1 for h in jockey_known if not h["has_jockey_stats"]),
            "jockey_stats_known": len(jockey_known)}


def _temperature(by_race: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """p を k 乗して正規化し直したときの平均の単勝 log loss（同じ標本の中の粗い比較。最適性は示さない）。"""
    out = {}
    for k in TEMPERATURE_K:
        losses = []
        for hs in by_race.values():
            z = sum(max(h["p_champion"], EPS) ** k for h in hs)
            w = next((h for h in hs if h["y"] == 1), None)
            if w is not None:
                losses.append(-math.log(max(w["p_champion"], EPS) ** k / z))
        out[f"k={k}"] = round(sum(losses) / len(losses), 6) if losses else None
    losses = [math.log(len(hs)) for hs in by_race.values() if any(h["y"] == 1 for h in hs)]
    out["uniform"] = round(sum(losses) / len(losses), 6) if losses else None
    return out


def _winner_rank(by_race: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    ranks = {"model": [], "market": []}
    for hs in by_race.values():
        w = next((h for h in hs if h["y"] == 1), None)
        if w is None:
            continue
        ranks["model"].append(_rank(w["p_champion"], [h["p_champion"] for h in hs]))
        ranks["market"].append(_rank(w["q_market"], [h["q_market"] for h in hs]))
    return {who: {"races": len(r), "mean_rank": round(sum(r) / len(r), 3) if r else None,
                  "top3": sum(1 for x in r if x <= 3)} for who, r in ranks.items()}


# ------------------------------------------------------------------ 4. odds-live

def _ol_tree(ref: str, day: str, repo: Path) -> list[str]:
    return _git(["ls-tree", "-r", "--name-only", ref, "--", f"{day}/"], repo).decode().split()


def _ol_read(ref: str, rel: str, repo: Path) -> Any:
    data = _git(["show", f"{ref}:{rel}"], repo)
    return json.loads(gzip.decompress(data) if rel.endswith(".gz") else data)


def _expected_post(row: dict[str, Any]) -> datetime.datetime | None:
    """一覧の date・post_time（JST）から発走時刻。読めなければ None。"""
    try:
        return datetime.datetime.strptime(f"{row['date']} {str(row['post_time']).strip()}",
                                          "%Y-%m-%d %H:%M").replace(tzinfo=common.JST)
    except (KeyError, TypeError, ValueError):
        return None


def odds_live_check(ref: str, days: list[str], results: dict[str, Any], repo: Path) -> dict[str, Any]:
    """odds-live の独立の点検。時点ごとに、収集時の状態（collection）と点検の結果（audit）を分ける。

    audit が ok なのは、その時点の点検を全部通ったときだけ（ファイルの中身が期待のレース・時点・発走時刻と一致、
    5券種がそろって ok、時点の順序、post2 の final_check の照合）。払戻との照合は観測の正しさとは別の点検として
    payout に分けて数える。final_check は保存された判定を読み、post1・post2 の表から同じ規則で計算し直して照らす。
    """
    out = {}
    for day in days:
        files = _ol_tree(ref, day, repo)
        lists = sorted(f for f in files if f.startswith(f"{day}/races/") and f.endswith(".json"))
        race_list = _ol_read(ref, lists[-1], repo) if lists else None
        check = (race_list or {}).get("check") or {}
        list_ok = check.get("status") == "ok"
        rows = [r for r in (race_list or {}).get("races") or [] if r.get("race_key")] if list_ok else []
        observed: dict[str, dict[str, str]] = {}
        for f in files:
            parts = f.split("/")
            if len(parts) == 3 and parts[2].endswith(".json.gz"):
                observed.setdefault(parts[1], {})[parts[2][:-len(".json.gz")]] = f
        problems: list[str] = []
        if not list_ok:
            problems.append(f"{day}:race_list_not_ok")
        keys = [r["race_key"] for r in rows]
        if len(set(keys)) != len(keys):
            problems.append(f"{day}:race_list_duplicate_keys")
        # check の場ごとの数は障害戦も数える。一覧（期待の集合）は障害戦・発走時刻の無いレースを外す（odds-live の規則）。
        # 差は問題にせず、数だけ残す（外したレースの種別はこの一覧からは確かめられない）
        by_venue = sum((check.get("races_by_venue") or {}).values())
        races = []
        for row in rows:
            key = row["race_key"]
            post = _expected_post(row)
            docs = {ph: _ol_read(ref, observed[key][ph], repo) for ph in PHASES if ph in observed.get(key, {})}
            race = {"race_key": key, "post_at": _iso(post), "phases": {}}
            for ph in PHASES:
                race["phases"][ph] = _ol_phase(key, ph, post, docs.get(ph), docs.get("post1"), problems)
            if docs.get("post2") is not None and results.get(key):
                race["payout"] = _payout_match(docs["post2"], results[key])
            races.append(race)
        for key in sorted(set(observed) - set(keys)):
            problems.append(f"{key}:not_in_race_list")
        payout_problems = [f"{r['race_key']}:{PAYOUT_MARKETS[jp]}:payout_{pm['match']}"
                           for r in races for jp, pm in (r.get("payout") or {}).items() if pm["match"] != "match"]
        out[day] = {"race_list": {"file": lists[-1] if lists else None, "status": check.get("status"),
                                  "races": len(rows), "races_by_venue_total": by_venue,
                                  "not_in_list": by_venue - len(rows) if list_ok else None},
                    "phase_collection": _count_states(races, "collection"),
                    "phase_audit": _count_states(races, "audit"),
                    "problem_counts": _count_problems(problems), "problems": problems,
                    "payout": {"races": sum(1 for r in races if r.get("payout")),
                               "problem_counts": _count_problems(payout_problems), "problems": payout_problems},
                    "races_detail": races}
    return out


def _ol_phase(key: str, ph: str, post: datetime.datetime | None, d: dict[str, Any] | None,
              post1: dict[str, Any] | None, problems: list[str]) -> dict[str, Any]:
    mine: list[str] = []
    if d is None:
        problems.append(f"{key}:{ph}:missing_file")
        return {"collection": "missing_file", "audit": "missing_file"}
    status = d.get("status")
    entry: dict[str, Any] = {"collection": "ok" if status == "ok" else f"{status}:{d.get('reason')}", "markets": {}}
    if d.get("race_key") != key:
        mine.append("race_key_mismatch")
    if d.get("phase") != ph:
        mine.append("phase_mismatch")
    if post is None:
        mine.append("post_time_unknown")
    elif common.parse_dt(d.get("post_at")) != post:
        mine.append("post_at_mismatch")
    if status != "ok":
        # missed（記録を始める前に発走）・partial・failed は成功に数えない。理由を残して区別する
        mine.append(entry["collection"])
    else:
        for name in OL_MARKETS:
            m = (d.get("markets") or {}).get(name)
            if m is None:
                entry["markets"][name] = {"state": "missing"}
                mine.append(f"{name}:market_missing")
                continue
            entry["markets"][name] = me = {"state": m.get("status"), "rows": m.get("rows"),
                                           "source_time": m.get("source_time_jst"), "observed_at": m.get("observed_at")}
            if m.get("status") != "ok":
                mine.append(f"{name}:market_{m.get('status')}")
            me["timing"] = t = _phase_timing(ph, common.parse_dt(m.get("source_time_jst")),
                                             common.parse_dt(m.get("observed_at")), post)
            if t != "verified":
                mine.append(f"{name}:timing_{t}")
        if ph == "post2":
            entry["final_check"], fc_problems = _final_check_audit(d, post1, post)
            mine.extend(fc_problems)
    problems.extend(f"{key}:{ph}:{p}" for p in mine)
    entry["audit"] = "ok" if not mine else "problem"
    entry["audit_problems"] = mine
    return entry


def _phase_timing(ph: str, source: datetime.datetime | None, observed: datetime.datetime | None,
                  post: datetime.datetime | None) -> str:
    """pre・close は source ≦ observed < 発走（market_audit.odds_timing と同じ）。post1・post2 は観測が発走以降で、
    source ≦ observed。発走後の観測の source は締め切り直前のこともあるので、source ≧ 発走は求めない。"""
    if ph in ("pre", "close"):
        t = ma.odds_timing(source, observed, post, None)["odds_timing_status"]
        return {"pre_race_verified": "verified", "post_race_detected": "post_race"}.get(t, t)
    if source is None or observed is None or post is None:
        return "unknown"
    if observed < post:
        return "observed_before_post"
    return "inconsistent" if source > observed else "verified"


def _recompute_final(post1: dict[str, Any] | None, post2: dict[str, Any], post: datetime.datetime | None) -> dict[str, Any]:
    """scraper/capture_odds_live.py の final_check と同じ規則で、post1・post2 の表から計算し直す（券種ごと）。"""
    if post1 is None or post1.get("status") == "missed" or post is None:
        return {n: None for n in OL_MARKETS}
    out = {}
    for n in OL_MARKETS:
        m1 = (post1.get("markets") or {}).get(n) or {}
        m2 = (post2.get("markets") or {}).get(n) or {}
        if m1.get("status") != "ok" or m2.get("status") != "ok":
            out[n] = False
            continue
        t1, t2 = common.parse_dt(m1.get("source_time_jst")), common.parse_dt(m2.get("source_time_jst"))
        if t1 is None or t2 is None or t1 < post - SALES_CLOSE or t2 < post - SALES_CLOSE:
            out[n] = False
        else:
            out[n] = m1.get("odds") is not None and m1.get("odds") == m2.get("odds")
    return out


def _final_check_audit(d: dict[str, Any], post1: dict[str, Any] | None,
                       post: datetime.datetime | None) -> tuple[dict[str, Any] | None, list[str]]:
    fc = d.get("final_check")
    if not isinstance(fc, dict):
        return None, ["final_check_missing"]
    problems = []
    per = {n: ((fc.get("markets") or {}).get(n) or {}).get("verified") for n in OL_MARKETS}
    overall = fc.get("verified")
    recomputed = _recompute_final(post1, d, post)
    if fc.get("basis") is None:
        problems.append("final_check_basis_unknown")
    elif fc.get("basis") != FINAL_CHECK_BASIS:
        problems.append("final_check_basis_differs")
    if overall is None:
        problems.append("final_check_overall_unknown")
    for n in OL_MARKETS:
        if per[n] is None:
            problems.append(f"{n}:final_check_unknown")
        elif per[n] is not True:
            problems.append(f"{n}:final_check_not_verified")
        if recomputed[n] is None:
            problems.append(f"{n}:final_check_not_recomputable")
        elif per[n] is not None and recomputed[n] != per[n]:
            problems.append(f"{n}:final_check_recomputed_differs")
    if overall is not None and all(v is not None for v in per.values()) and bool(overall) != all(per.values()):
        problems.append("final_check_overall_disagrees")
    return {"verified": overall, "basis": fc.get("basis"), "markets": per, "recomputed": recomputed}, problems


def _count_states(races: list[dict[str, Any]], key: str) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for r in races:
        for ph, e in r["phases"].items():
            counts.setdefault(ph, {}).setdefault(e[key], 0)
            counts[ph][e[key]] += 1
    return counts


def _count_problems(problems: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in problems:
        kind = p.rsplit(":", 1)[-1].split("(")[0]
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def _payout_match(d: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
    """post2 の締切後のオッズ（100円あたり）と、実際の払戻の照合。

    1行ずつ match・mismatch（ワイドは within_range・outside_range）・combo_not_listed（取得元の表に無い組：
    価格は不明）・price_unavailable（その券種の表が取れていない）を付ける。まとめは不一致を優先し、
    次に不明（unknown：一致の行があっても不明の行があれば partially_unknown）、全部一致のときだけ match。
    """
    out = {}
    for jp, key in PAYOUT_MARKETS.items():
        divs = (res.get("dividends") or {}).get(jp) or []
        m = (d.get("markets") or {}).get(key) or {}
        odds = m.get("odds") if m.get("status") == "ok" else None
        rows = []
        for div in divs:
            combo = "-".join(map(str, sorted(int(h) for h in div.get("horses") or [])))
            pay = int(div["pay"])
            o = odds.get(combo) if isinstance(odds, dict) else None
            if not isinstance(odds, dict):
                check = "price_unavailable"
            elif o is None:
                check = "combo_not_listed"
            elif isinstance(o, list) and len(o) == 2:
                check = "within_range" if round(float(o[0]) * 100) <= pay <= round(float(o[1]) * 100) else "outside_range"
            else:
                check = "match" if round(float(o) * 100) == pay else "mismatch"
            rows.append({"combo": combo, "pay": pay, "odds": o, "check": check})
        if not rows:
            continue
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["check"]] = counts.get(r["check"], 0) + 1
        good = counts.get("match", 0) + counts.get("within_range", 0)
        bad = counts.get("mismatch", 0) + counts.get("outside_range", 0)
        unknown = counts.get("combo_not_listed", 0) + counts.get("price_unavailable", 0)
        match = ("mismatch" if bad else "partially_unknown" if unknown and good else "unknown" if unknown
                 else "match")
        out[jp] = {"rows": rows, "counts": counts, "match": match}
    return out


# ------------------------------------------------------------------ 実行

def load_inputs(main: str, start: str, end: str, repo: Path) -> tuple[list[tuple[str, bytes]], dict[str, Any]]:
    names = _git(["ls-tree", "--name-only", main, "--", f"{EVAL_DIR_REL}/"], repo).decode().split()
    evals = [(n, _git(["show", f"{main}:{n}"], repo)) for n in sorted(names)
             if n.endswith(".json") and start <= Path(n).name[:8] <= end]
    results = json.loads(_git(["show", f"{main}:{RESULTS_REL}"], repo))
    return evals, results


def run_target(main: str, ol_ref: str, start: str, end: str, days: list[str], run_id: str | None,
               out_dir: Path | None) -> tuple[str, Path]:
    """run の名前と出力先。固定の参照（RUN）と同じ入力のときだけ stage0-r1 として既定の場所に書く。

    入力（main・odds-live のコミット、期間、odds-live の日）が一つでも違えば別の run で、stage0-r1 以外の
    --run-id が要り、出力は既定で data/research/independent_prob/{run_id}/ に分ける。別の run が stage0-r1 の
    場所に書くことはできない（--out で指定しても拒否する）。
    """
    pinned = (main == RUN["main"] and ol_ref == RUN["odds_live"] and start == RUN["from"] and end == RUN["to"]
              and days == list(RUN["odds_live_days"]))
    if pinned and run_id in (None, RUN["run_id"]):
        return RUN["run_id"], out_dir or OUT_DIR
    if pinned:
        raise ValueError("固定の参照と同じ入力の run は stage0-r1（--run-id を付けないか stage0-r1）")
    if not run_id or run_id == RUN["run_id"] or not run_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("固定の参照と違う入力の run には、stage0-r1 以外の --run-id（英数字・-・_）が要る")
    target = out_dir or OUT_DIR.parent / run_id
    if target.resolve() == OUT_DIR.resolve():
        raise ValueError(f"別の run（{run_id}）は stage0-r1 の出力先に書けない")
    return run_id, target


def guard_overwrite(out_dir: Path, summary: dict[str, Any]) -> None:
    """出力先に別の run か、同じ run の違う入力の結果があれば書かない（研究の記録を混ぜない）。"""
    path = out_dir / "summary.json"
    if not path.exists():
        return
    old = json.loads(path.read_text(encoding="utf-8"))
    if old.get("run_id") != summary["run_id"] or old.get("inputs") != summary["inputs"]:
        raise ValueError(f"{out_dir} には別の run か違う入力の結果がある（run_id={old.get('run_id')}）。上書きしない")


def run(repo: Path | None = None, out_dir: Path | None = None, main_ref: str | None = None,
        odds_live_ref: str | None = None, start: str | None = None, end: str | None = None,
        odds_live_days: list[str] | None = None, run_id: str | None = None) -> dict[str, Any]:
    repo = repo or common.ROOT
    main = resolve(main_ref or RUN["main"], repo)
    ol_ref = resolve(odds_live_ref or RUN["odds_live"], repo)
    start, end = start or RUN["from"], end or RUN["to"]
    days = list(odds_live_days or RUN["odds_live_days"])
    run_id, out_dir = run_target(main, ol_ref, start, end, days, run_id, out_dir)
    evals, results = load_inputs(main, start, end, repo)
    races, horses = [], []
    for rel, blob in evals:
        rec = json.loads(blob)
        prov = provenance(rec, main, repo)
        row, hs = race_tables(rec, prov, results.get(rec["race_id"]), _sha256(blob), rel)
        races.append(row)
        horses.extend(hs)
    strata = [diagnostics(horses, races, "as_recorded"),
              diagnostics(horses, races, "audited"),
              diagnostics(horses, races, "as_recorded", {"leading_stats_partial"}),
              diagnostics(horses, races, "as_recorded", {"early_freeze"}),
              diagnostics(horses, races, "as_recorded", {"leading_stats_partial", "early_freeze"})]
    ol = odds_live_check(ol_ref, days, results, repo)
    raw_h = [h for h in horses if h["in_raw"]]
    audit_counts: dict[str, int] = {}
    for r in races:
        audit_counts[r["audit_status"]] = audit_counts.get(r["audit_status"], 0) + 1
    inputs_digest = _sha256("".join(f"{rel}\0{_sha256(blob)}\n" for rel, blob in evals).encode())
    summary = {
        "schema": SCHEMA, "issue": 57, "run_id": run_id,
        "used_for_prediction": False, "official_results_untouched": True,
        "inputs": {"main_commit": main, "odds_live_commit": ol_ref, "race_days": [start, end],
                   "odds_live_days": days, "evaluation": EVAL_DIR_REL, "evaluation_records": len(evals),
                   "evaluation_inputs_sha256": inputs_digest, "results": RESULTS_REL,
                   "results_sha256": _sha256(_git(["show", f"{main}:{RESULTS_REL}"], repo))},
        "provenance": {"audit_status": dict(sorted(audit_counts.items())),
                       "not_verified": {r["race_id"]: {k: v for k, v in r["checks"].items() if v != VERIFIED}
                                        for r in races if r["audit_status"] != "verified"}},
        "denominators": {
            "races": len(races),
            "input_quality_horses": len(raw_h),
            "probability_horses": sum(1 for h in horses if h["in_probability"]),
            "market_horses": strata[0]["horses"],
            "not_evaluated": [{"race_id": h["race_id"], "num": h["num"], "reason": h["not_evaluated_reason"],
                               "in_result_finish": h["in_result_finish"]}
                              for h in horses if not h["in_probability"]],
        },
        "missing_horses_of_raw": {"jockey_stats": sum(1 for h in raw_h if not h["has_jockey_stats"]),
                                  "trainer_stats": sum(1 for h in raw_h if not h["has_trainer_stats"]),
                                  "jockey_or_trainer": sum(1 for h in raw_h
                                                           if not (h["has_jockey_stats"] and h["has_trainer_stats"])),
                                  "past_runs_zero": sum(1 for h in raw_h if h["past_runs"] == 0)},
        "flagged_races": {r["race_id"]: r["flags"] for r in races if r["flags"]},
        "strata": strata,
        "odds_live": {day: {k: v for k, v in d.items() if k != "races_detail"} for day, d in ol.items()},
        "note": ("探索的な記述（Issue #57 Stage 0）。勝率の比較で、3連複の確率ではない。同じ標本の中の比較で、"
                 "最適性・優位性・有意性の結論は出さない。旗は記録を直すためではなく層別のため。"
                 "Git の時刻は順序の確認に使うだけで、公開の絶対の証明ではない。"),
    }
    guard_overwrite(out_dir, summary)
    common.write_json(out_dir / "horses.json", horses)
    common.write_json(out_dir / "races.json", races)
    common.write_json(out_dir / "odds_live.json", ol)
    common.write_json(out_dir / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Issue #57 Stage 0 監査（読むだけ）")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--main-ref", default=None, help=f"既定は固定の参照 {RUN['main'][:7]}（{RUN['run_id']}）")
    p.add_argument("--odds-live-ref", default=None, help=f"既定は固定の参照 {RUN['odds_live'][:7]}")
    p.add_argument("--from", dest="start", default=None, help="レースの日 YYYYMMDD（含む）")
    p.add_argument("--to", dest="end", default=None, help="レースの日 YYYYMMDD（含む）")
    p.add_argument("--odds-live-days", nargs="*", default=None)
    p.add_argument("--run-id", default=None)
    args = p.parse_args(argv)
    try:
        s = run(out_dir=args.out, main_ref=args.main_ref, odds_live_ref=args.odds_live_ref, start=args.start,
            end=args.end, odds_live_days=args.odds_live_days, run_id=args.run_id)
    except ValueError as exc:
        print(f"中止：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: s[k] for k in ("run_id", "provenance", "denominators", "missing_horses_of_raw")},
                     ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
