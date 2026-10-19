"""発走前の市場オッズ監査（observe-only）。Issue #38 (A) / #39 の PR-A。

目的は2つ。次の確率モデル（#39）を作る前に、入力の「時点」と「欠け」を機械的に確かめておく。

1. 発走前データの固定と、オッズの時点・カバー率の監査
   raw は発走後に結果取り込み・再実行で書き換わる（例：10/12 発走後の 6d06bd6、10/17 発走後の 55318d1）。
   main の最新 raw でオッズを読むと、発走後のオッズが混ざる（リーク）。そこで各レースについて、
   「発走前に snapshot を最後に更新したコミット」に raw を固定し、そのコミットの 3連複オッズの
   source_time / observed_at を発走時刻と比べ、収録組数・Σ1/odds を記録する。
   あわせて、現在の main の raw に発走後のオッズが入っているか（入っていれば学習・評価に使ってはいけない）も記録する。

2. 買い目の依存度の監査（#38 の A）
   同じ発走前の版にある固定3人・チャッピーのカードと、scenario-frame の記録の各券について、
   馬ごとの露出（何点に入っているか）、最も露出の多い馬が 3着外のとき残る券、3連複の市場の近似確率を付ける。

市場の近似確率について:
   P_mkt(組) ≈ 0.75 / オッズ（3連複の払戻率 75% と市場の効率性を仮定した近似）。
   オッズが収録されていない組（1000倍超など）は「確率 0」ではなく「不明」として扱い、
   全組で正規化した分布（proper な log loss に必要）はここでは作らない（PR-B の担当）。
   そのため、この出力の確率・和はすべて approx=true の参考値で、精密な値のように扱わない。

出力は data/research/market_audit/ にだけ書く。予想・正式成績・UI・既存の shadow 採点は読まない／変えない。
取得元のレスポンス本文・URL・リクエスト固有の値は写さない（数値と時刻だけ）。
"""
from __future__ import annotations

import argparse
import datetime
import itertools
import json
import logging
import math
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

from research import common
from research.decision_log import canonical_snapshot_path

logger = logging.getLogger("research.market_audit")

OUT_DIR = common.ROOT / "data" / "research" / "market_audit"
RACE_SCHEMA = "market-audit-race-v1"
SUMMARY_SCHEMA = "market-audit-summary-v1"
LOGLOSS_SCHEMA = "market-audit-paired-win-logloss-v1"
TRIO_PAYOUT = 0.75          # JRA 3連複の払戻率（控除率 25%）。近似に使うだけ
TRIO = "3連複"
SCENARIO_FRAME_DIR = "data/chat_experiments/scenario-frame/races"
RPS_RACES_DIR = "data/shadow/model_evaluation/race-performance-v1/races"
MODEL_EVAL_CODE = "research/model_evaluation.py"
NOTE = (
    "発走前の市場オッズ監査（observe-only）。raw は snapshot を発走前に最後に更新したコミットに固定して読む。"
    "3連複の確率は 0.75/オッズ の近似（approx）で、未収録の組は不明として扱う。全組で正規化した分布ではないので、"
    "和や条件付きの値を精密な確率として扱わない。予想・正式成績・UI には使わない。"
)


# ------------------------------------------------------------------ Git

def _git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=60)


def _commits(rel: str, repo: Path) -> list[tuple[str, datetime.datetime]]:
    out = _git(["log", "--format=%H %cI", "--", rel], repo)
    rows = []
    for line in out.stdout.decode().splitlines() if out.returncode == 0 else []:
        sha, _, stamp = line.partition(" ")
        when = common.parse_dt(stamp)
        if sha and when:
            rows.append((sha, when))
    return rows


def _show_json(sha: str, rel: str, repo: Path) -> Any:
    out = _git(["show", f"{sha}:{rel}"], repo)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _head_json(rel: str, repo: Path) -> Any:
    return _show_json("HEAD", rel, repo)


# ------------------------------------------------------------------ 時刻

def _source_dt(value: Any) -> datetime.datetime | None:
    """odds_market_meta の source_time（'YYYY-MM-DD HH:MM:SS'、JST）を読む。"""
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = common.parse_dt(value.strip().replace(" ", "T"))
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=common.JST)
    return parsed


def _iso(value: datetime.datetime | None) -> str | None:
    return value.astimezone(common.JST).isoformat(timespec="seconds") if value else None


# ------------------------------------------------------------------ 発走前の版

def prerace_versions(race_id: str, repo: Path) -> dict[str, Any]:
    """snapshot の版のうち、発走前にコミットされた pre_race の版（コミット時刻順）。

    戻り値: {"post": 発走時刻, "versions": [{"sha", "committed_at", "snapshot"}...], "post_race_commits": n}
    """
    rel = canonical_snapshot_path(race_id)
    versions, post_race_commits, post = [], 0, None
    for sha, when in sorted(_commits(rel, repo), key=lambda c: c[1]):
        snap = _show_json(sha, rel, repo)
        if not isinstance(snap, dict):
            continue
        post = post or common.post_at(race_id, snap.get("post_time"))
        frozen = common.parse_dt(snap.get("frozen_at"))
        if post is None or when >= post:
            post_race_commits += 1
            continue
        if snap.get("pre_race") is True and frozen is not None and frozen < post:
            versions.append({"sha": sha, "committed_at": when, "snapshot": snap})
    return {"post": post, "versions": versions, "post_race_commits": post_race_commits}


def raw_race(sha: str, week_id: str, race_id: str, repo: Path) -> dict[str, Any] | None:
    raw = _show_json(sha, f"raw/{week_id}.json", repo)
    if not isinstance(raw, dict):
        return None
    return next((r for r in raw.get("races") or [] if r.get("id") == race_id), None)


# ------------------------------------------------------------------ 市場

def trio_table(race: dict[str, Any]) -> dict[tuple[int, int, int], float]:
    """収録されている 3連複オッズ {(a,b,c): odds}。不正な値は捨てる。"""
    table = {}
    for key, odds in ((race.get("combo_odds") or {}).get(TRIO) or {}).items():
        try:
            combo = tuple(sorted(int(x) for x in str(key).split("-")))
            value = float(odds)
        except (TypeError, ValueError):
            continue
        if len(combo) == 3 and len(set(combo)) == 3 and value > 0 and math.isfinite(value):
            table[combo] = value
    return table


def market_block(race: dict[str, Any], post: datetime.datetime | None) -> dict[str, Any]:
    """3連複オッズの時点・カバー率。確率はすべて approx。"""
    runners = [e.get("num") for e in race.get("entries") or [] if isinstance(e.get("num"), int)]
    table = trio_table(race)
    meta = ((race.get("odds_market_meta") or {}).get("markets") or {}).get("sanrenpuku") or {}
    source = _source_dt(meta.get("source_time"))
    observed = common.parse_dt(meta.get("observed_at"))
    win_source = _source_dt(race.get("odds_updated_at"))
    inverse = sum(1.0 / v for v in table.values())
    total = math.comb(len(runners), 3) if len(runners) >= 3 else 0
    late = [x for x in (source, observed) if x is not None and post is not None and x >= post]
    return {
        "runners": len(runners),
        "trio_combos_listed": len(table),
        "trio_combos_total": total,
        "trio_inverse_odds_sum": round(inverse, 4),
        "trio_mass_listed_approx": round(inverse * TRIO_PAYOUT, 4),
        "trio_max_listed_odds": max(table.values()) if table else None,
        "trio_source_time": _iso(source),
        "trio_observed_at": _iso(observed),
        "win_source_time": _iso(win_source),
        "minutes_before_post": (round((post - source).total_seconds() / 60, 1)
                                if post is not None and source is not None else None),
        "post_race_odds": bool(late),
        "timing_known": source is not None and observed is not None,
        "approx": True,
    }


def trio_probs(race: dict[str, Any]) -> dict[tuple[int, int, int], float]:
    """収録されている組の近似確率 0.75/odds（未収録の組は含めない＝不明）。"""
    return {combo: TRIO_PAYOUT / odds for combo, odds in trio_table(race).items()}


def top3_mass(probs: dict[tuple[int, int, int], float]) -> dict[int, float]:
    """馬ごとの『収録組に限った』3着内の近似確率（Σ その馬を含む組）。下限の目安で、正規化していない。"""
    mass: dict[int, float] = {}
    for combo, p in probs.items():
        for h in combo:
            mass[h] = mass.get(h, 0.0) + p
    return mass


# ------------------------------------------------------------------ 券の依存度

def ticket_audit(bets: list[dict[str, Any]], probs: dict[tuple[int, int, int], float],
                 market_x: int | None) -> dict[str, Any]:
    """1枚のカード（または実験の記録）の券について、露出と残り券と 3連複の近似確率。"""
    rows, exposure, stake = [], {}, 0
    for bet in bets or []:
        horses = sorted(int(h) for h in bet.get("horses") or [] if isinstance(h, (int, float)))
        amt = int(bet.get("amt") or 0)
        stake += amt
        for h in horses:
            exposure[h] = exposure.get(h, 0) + 1
        row = {"type": bet.get("type"), "horses": horses, "amt": amt}
        if bet.get("type") == TRIO and len(horses) == 3:
            p = probs.get(tuple(horses))
            row["p_mkt_approx"] = round(p, 5) if p is not None else None
            row["market_status"] = "listed" if p is not None else "not_listed"
        rows.append(row)
    n = len(rows)
    axis = max(sorted(exposure), key=lambda h: exposure[h]) if exposure else None

    def without(h: int | None) -> dict[str, Any] | None:
        if h is None:
            return None
        left = [r for r in rows if h not in r["horses"]]
        return {"horse": h, "tickets_containing": n - len(left), "tickets_left": len(left),
                "stake_left": sum(r["amt"] for r in left)}

    trio_rows = [r for r in rows if r["type"] == TRIO]
    listed = [r["p_mkt_approx"] for r in trio_rows if r.get("p_mkt_approx") is not None]
    return {
        "tickets": rows,
        "stake": stake,
        "exposure": {str(h): exposure[h] for h in sorted(exposure)},
        "max_exposure_share": round(exposure[axis] / n, 4) if axis is not None and n else None,
        "if_axis_out": without(axis),
        "if_market_x_out": without(market_x),
        "trio_tickets": len(trio_rows),
        "trio_tickets_listed": len(listed),
        # 3連複の別々の組は同時に当たらないので、和＝少なくとも1点当たる近似確率。
        # 未収録の組（1000倍超など）を含むときは和を出さない（0 とみなさない）
        "trio_hit_prob_approx": (round(sum(listed), 5)
                                 if trio_rows and len(listed) == len(trio_rows) else None),
        "trio_hit_prob_status": ("none" if not trio_rows else
                                 "complete" if len(listed) == len(trio_rows) else "has_unlisted_combo"),
    }


def _scenario_records(race_id: str, repo: Path) -> list[tuple[str, dict[str, Any]]]:
    """HEAD にある scenario-frame の記録（相対パス, 記録）。"""
    out = _git(["ls-tree", "-r", "--name-only", "HEAD", f"{SCENARIO_FRAME_DIR}/{race_id}"], repo)
    rows = []
    for rel in sorted(out.stdout.decode().split()) if out.returncode == 0 else []:
        record = _head_json(rel, repo)
        if isinstance(record, dict):
            rows.append((rel, record))
    return rows


# ------------------------------------------------------------------ 1レース

def audit_race(race_id: str, repo: Path) -> dict[str, Any]:
    found = prerace_versions(race_id, repo)
    post, versions = found["post"], found["versions"]
    out: dict[str, Any] = {
        "schema": RACE_SCHEMA, "race_id": race_id, "post_at": _iso(post),
        "approx": True, "used_for_prediction": False, "official_results_untouched": True,
        "prerace_snapshot_versions": len(versions),
        "post_race_snapshot_commits": found["post_race_commits"],
    }
    if not versions:
        # 発走後にまとめてコミットされた snapshot（後からの追加など）は、Git の時刻で発走前性を示せない
        out["status"] = "no_prerace_commit"
        return out
    final = versions[-1]
    snap = final["snapshot"]
    week_id = snap.get("week_id")
    race = raw_race(final["sha"], week_id, race_id, repo) if week_id else None
    out["pinned"] = {"commit": final["sha"], "committed_at": _iso(final["committed_at"]),
                     "frozen_at": snap.get("frozen_at"), "week_id": week_id}
    if race is None:
        out["status"] = "raw_missing_at_pinned_commit"
        return out
    market = market_block(race, post)
    probs = trio_probs(race)
    mass = top3_mass(probs)
    market_x = max(sorted(mass), key=lambda h: mass[h]) if mass else None
    out["market"] = market
    out["market"]["x"] = ({"horse": market_x, "top3_mass_listed_approx": round(mass[market_x], 4)}
                          if market_x is not None else None)

    head_race = None
    if week_id:
        head_raw = _head_json(f"raw/{week_id}.json", repo)
        if isinstance(head_raw, dict):
            head_race = next((r for r in head_raw.get("races") or [] if r.get("id") == race_id), None)
    if head_race is not None:
        head_market = market_block(head_race, post)
        out["head_raw"] = {"post_race_odds": head_market["post_race_odds"],
                           "trio_source_time": head_market["trio_source_time"],
                           "same_as_pinned": head_market["trio_source_time"] == market["trio_source_time"]}
    else:
        out["head_raw"] = {"post_race_odds": None, "trio_source_time": None, "same_as_pinned": None}

    out["cards"] = {c.get("char"): ticket_audit(c.get("bets") or [], probs, market_x)
                    for c in snap.get("cards") or [] if c.get("char")}

    by_frozen = {}
    for v in versions:
        by_frozen.setdefault(v["snapshot"].get("frozen_at"), v)
    experiments = []
    for rel, record in _scenario_records(race_id, repo):
        frozen = (record.get("snapshot_ref") or {}).get("frozen_at")
        version = by_frozen.get(frozen)
        row: dict[str, Any] = {"record": rel, "snapshot_frozen_at": frozen, "action": record.get("action")}
        if version is None:
            row["status"] = "snapshot_version_not_found"
        else:
            v_race = raw_race(version["sha"], version["snapshot"].get("week_id"), race_id, repo)
            v_probs = trio_probs(v_race) if v_race else {}
            v_mass = top3_mass(v_probs)
            v_x = max(sorted(v_mass), key=lambda h: v_mass[h]) if v_mass else None
            row.update(status="audited", pinned_commit=version["sha"],
                       market_post_race_odds=market_block(v_race, post)["post_race_odds"] if v_race else None,
                       **ticket_audit(record.get("bets") or [], v_probs, v_x))
        experiments.append(row)
    out["scenario_frame"] = experiments
    out["status"] = "audited"
    return out


# ------------------------------------------------------------------ 勝ち馬 log loss の再現用

def paired_win_logloss(repo: Path) -> dict[str, Any]:
    """race-performance-v1 の前向き評価の各レースから、Champion と市場の勝ち馬 log loss を同じレースで対にする。

    p_model・p_mkt はどちらも snapshot（発走前の凍結）の marks から作られている（research/model_evaluation.py）。
    """
    out = _git(["ls-tree", "--name-only", "HEAD", f"{RPS_RACES_DIR}/"], repo)
    rows, excluded = [], []
    for rel in sorted(out.stdout.decode().split()) if out.returncode == 0 else []:
        rec = _head_json(rel, repo)
        if not isinstance(rec, dict):
            continue
        race_id = rec.get("race_id")
        if rec.get("status") != "evaluated" or rec.get("phase") != "forward":
            excluded.append({"race_id": race_id, "reason": rec.get("status") or "not_forward"})
            continue
        prob = rec.get("probability") or {}
        winner = prob.get("winner")
        horse = next((h for h in prob.get("horses") or [] if h.get("num") == winner), None)
        market_status = (prob.get("market") or {}).get("status")
        if horse is None or horse.get("p_q_market") is None:
            excluded.append({"race_id": race_id, "reason": market_status or "market_missing"})
            continue
        pc, pm = float(horse["p_champion"]), float(horse["p_q_market"])
        rows.append({"race_id": race_id, "frozen_at": rec.get("frozen_at"), "winner": winner,
                     "p_champion": pc, "p_market": pm,
                     "logloss_champion": round(-math.log(pc), 6), "logloss_market": round(-math.log(pm), 6)})
    diffs = [r["logloss_champion"] - r["logloss_market"] for r in rows]
    code = _git(["log", "-1", "--format=%H", "--", MODEL_EVAL_CODE], repo)
    return {
        "schema": LOGLOSS_SCHEMA,
        "source": RPS_RACES_DIR,
        "evaluation_code": {"path": MODEL_EVAL_CODE,
                            "commit": code.stdout.decode().strip() or None if code.returncode == 0 else None},
        "timing": "p_champion・p_market とも snapshot（発走前の凍結）の marks から。p_market は単勝オッズの逆数を正規化",
        "races": rows,
        "excluded": excluded,
        "n": len(rows),
        "mean_diff_champion_minus_market": round(statistics.mean(diffs), 6) if diffs else None,
        "se_diff": (round(statistics.stdev(diffs) / math.sqrt(len(diffs)), 6) if len(diffs) >= 2 else None),
        "note": "lower is better。正の値は市場の方が勝ち馬に高い確率を付けていたことを示す。少数標本で有意性は無い。",
    }


# ------------------------------------------------------------------ 集計

def _race_ids(repo: Path) -> list[str]:
    out = _git(["ls-tree", "--name-only", "HEAD", "data/snapshots/"], repo)
    names = out.stdout.decode().split() if out.returncode == 0 else []
    return sorted(Path(n).stem for n in names if n.endswith(".json"))


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    audited = [r for r in rows if r.get("status") == "audited"]
    pinned_late = [r["race_id"] for r in audited if r["market"]["post_race_odds"]]
    head_late = [r["race_id"] for r in audited if r["head_raw"].get("post_race_odds")]
    masses = [r["market"]["trio_mass_listed_approx"] for r in audited if r["market"]["trio_combos_listed"]]
    no_trio = [r["race_id"] for r in audited if not r["market"]["trio_combos_listed"]]

    def conc(source: str) -> dict[str, Any]:
        items = []
        for r in audited:
            if source == "scenario_frame":
                items += [x for x in r.get("scenario_frame") or [] if x.get("status") == "audited"]
            else:
                items += [c for name, c in (r.get("cards") or {}).items() if name == source]
        shares = [x["max_exposure_share"] for x in items if x.get("max_exposure_share") is not None]
        left: dict[str, int] = {}
        for x in items:
            if x.get("if_axis_out"):
                key = str(x["if_axis_out"]["tickets_left"])
                left[key] = left.get(key, 0) + 1
        hits = [x["trio_hit_prob_approx"] for x in items if x.get("trio_hit_prob_approx") is not None]
        return {"n": len(items),
                "mean_max_exposure_share": round(statistics.mean(shares), 4) if shares else None,
                "tickets_left_if_axis_out_counts": dict(sorted(left.items(), key=lambda kv: int(kv[0]))),
                "trio_hit_prob_approx_n": len(hits),
                "mean_trio_hit_prob_approx": round(statistics.mean(hits), 4) if hits else None}

    return {
        "races": len(rows),
        "audited": len(audited),
        "not_audited": [{"race_id": r["race_id"], "status": r.get("status")} for r in rows if r not in audited],
        "pinned_post_race_odds": pinned_late,
        "head_raw_post_race_odds": head_late,
        "races_without_trio_odds": no_trio,
        "trio_mass_listed_approx": {"min": min(masses), "median": round(statistics.median(masses), 4),
                                    "max": max(masses)}
        if masses else None,
        "concentration": {name: conc(name) for name in ("kei", "tetsu", "gen", "chappy", "scenario_frame")},
    }


def run(repo: Path | None = None, out_dir: Path | None = None) -> dict[str, Any]:
    repo = repo or common.ROOT
    out_dir = out_dir or OUT_DIR
    rows = []
    for race_id in _race_ids(repo):
        try:
            row = audit_race(race_id, repo)
        except Exception as exc:  # 1レースの失敗で全体を止めない。理由は型名だけ残す
            logger.exception("監査に失敗: %s", race_id)
            row = {"schema": RACE_SCHEMA, "race_id": race_id, "status": "error", "error_type": type(exc).__name__}
        common.write_json(out_dir / "races" / f"{race_id}.json", row)
        rows.append(row)
    summary = {"schema": SUMMARY_SCHEMA, "generated_at": datetime.datetime.now(common.JST).isoformat(timespec="seconds"),
               "note": NOTE, "used_for_prediction": False, "official_results_untouched": True,
               "trio_payout_assumed": TRIO_PAYOUT, **summarize(rows)}
    common.write_json(out_dir / "summary.json", summary)
    common.write_json(out_dir / "paired_win_logloss.json", paired_win_logloss(repo))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="発走前の市場オッズ監査（observe-only）")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    summary = run(args.repo, args.out)
    print(json.dumps({k: summary[k] for k in ("races", "audited", "pinned_post_race_odds", "head_raw_post_race_odds")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
