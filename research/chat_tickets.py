"""チャットで出した本線の参考券の記録（chat-ticket-v1、observe-only）。EDGE_LOOP.md §8.1・§9、CHAT_TICKETS.md。

make      本線 edge-v0（portfolio-shadow-v1 の x_out_2）の券を、main の今の版から、portfolio-shadow-v1 の登録コミットの
          engine で計算し、data/chat_tickets/{race_id}/{YYYYMMDDTHHMMSS}.json に書く。チャットではこの記録の券だけを出し、
          発走前に main にコミットする。
validate  記録の形と規約だけを確かめる（CI。Git の履歴は見ない）。
audit     全履歴で、発走前のコミット・コミット後に変えていない・決め方どおり（記録が見た版から計算し直して一致）を確かめ、
          結果が出たレースは本線の記録（portfolio-shadow-v1）と比べる。data/research/chat_tickets/ にだけ書く。

予想・正式成績・UI・Champion には使わない。成績は shadow で、portfolio-shadow-v1・edge-v1 の判定にも入れない
（本線の成績は、凍結版から portfolio-shadow-v1 が計算する。この記録は「チャットで出した券が決め方どおりで、
発走前に出ていた」ことを残すためのもの）。記録には数値・時刻・コミットだけを書き、取得元の本文や URL は書かない。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

from research import common
from research import market_audit as ma
from research import portfolio_shadow as ps

logger = logging.getLogger("research.chat_tickets")

RECORD_SCHEMA = "chat-ticket-v1"
RACE_SCHEMA = "chat-ticket-audit-race-v1"
SUMMARY_SCHEMA = "chat-ticket-summary-v1"
RECORDS_REL = "data/chat_tickets"
OUT_DIR = common.ROOT / "data" / "research" / "chat_tickets"
SHADOW_RACES_REL = "data/research/portfolio_shadow/races"
RESULTS_REL = "data/race_results.json"
MAIN_VERSION = "edge-v0"
STRATEGY = "x_out_2"
UNIT_YEN = 100
MAX_TICKETS = 5
TRIO = "3連複"
NOTE = ("チャットで出した本線（edge-v0 = portfolio-shadow-v1 の x_out_2）の参考券。research/chat_tickets.py make で作り、"
        "発走前に main にコミットした。予想・正式成績・UI には使わない。")

RACE_ID_RE = re.compile(r"^\d{8}-[a-z]+-\d{2}$")
STAMP_RE = re.compile(r"^\d{8}T\d{6}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
REQUIRED_KEYS = {"schema", "race_id", "version", "strategy", "created_at", "computed_from", "pinned", "post_at",
                 "action", "tickets", "unit_yen", "stake", "p_hit", "used_for_prediction",
                 "official_results_untouched", "note"}
OPTIONAL_KEYS = {"x", "reason", "proven_optimal"}


# ------------------------------------------------------------------ 計算（make と audit で同じ）

def _git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=120)


def _rev_sha(rev: str, repo: Path) -> str:
    out = _git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], repo)
    sha = out.stdout.decode().strip()
    if out.returncode != 0 or not sha:
        raise ValueError(f"コミットが見つからない: {rev}")
    return sha


@contextlib.contextmanager
def _worktree(repo: Path, sha: str) -> Iterator[Path]:
    """sha を HEAD にした一時の作業ツリー。発走前の版の探し方（git log）が、その時点までの履歴だけを見る。"""
    parent = Path(tempfile.mkdtemp(prefix="chat-tickets-"))
    path = parent / "wt"
    try:
        out = _git(["worktree", "add", "-q", "--detach", str(path), sha], repo)
        if out.returncode != 0:
            raise RuntimeError("一時の作業ツリーを作れない")
        yield path
    finally:
        _git(["worktree", "remove", "--force", str(path)], repo)
        _git(["worktree", "prune"], repo)
        shutil.rmtree(parent, ignore_errors=True)


def compute(race_id: str, rev: str, repo: Path, reference: dict[str, Any] | None = None) -> dict[str, Any]:
    """rev の時点の main で、本線の券を portfolio-shadow-v1 と同じ engine・契約で計算する。"""
    reg = ps.registration(repo)
    if reg["status"] != "ok":
        raise RuntimeError(f"portfolio-shadow-v1 の登録が使えない: {reg['status']}")
    sha = _rev_sha(rev, repo)
    with _worktree(repo, sha) as wt:
        ref = reference or ps.load_reference(wt, reg["commit"])
        base = ref["pf"].portfolio_race(race_id, None, ref["portfolio"], ref["baseline"], wt)
    return {"rev": sha, "engine_commit": reg["commit"], **decide(base)}


def decide(base: dict[str, Any]) -> dict[str, Any]:
    """portfolio_race の出力から、チャットに出す券（または見送り）を決める。"""
    out: dict[str, Any] = {"pinned": base.get("pinned"), "post_at": base.get("post_at")}
    if base.get("status") == "excluded":
        return {**out, "action": "pass", "reason": f"excluded:{base.get('reason')}", "tickets": [], "stake": 0,
                "p_hit": None}
    s = (base.get("strategies") or {}).get(STRATEGY) or {}
    tickets = sorted(sorted(int(h) for h in t["horses"]) for t in s.get("tickets") or [])
    if s.get("status") != "chosen" or not tickets:
        return {**out, "action": "pass", "reason": f"{STRATEGY}:{s.get('status')}", "tickets": [], "stake": 0,
                "p_hit": None}
    p_hit = s.get("p_hit")
    return {**out, "action": "bet", "x": base.get("x"), "tickets": tickets, "stake": UNIT_YEN * len(tickets),
            "p_hit": round(p_hit, 6) if isinstance(p_hit, (int, float)) else None,
            "proven_optimal": s.get("proven_optimal")}


def _stamp(when: datetime.datetime) -> str:
    return when.astimezone(common.JST).strftime("%Y%m%dT%H%M%S")


def record_rel(race_id: str, created_at: datetime.datetime) -> str:
    return f"{RECORDS_REL}/{race_id}/{_stamp(created_at)}.json"


def make(race_id: str, repo: Path | None = None, rev: str = "origin/main", now: datetime.datetime | None = None,
         reference: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """記録を作って書く。発走時刻が分からないか、もう発走していれば作らない。"""
    repo = repo or common.ROOT
    now = (now or datetime.datetime.now(common.JST)).astimezone(common.JST).replace(microsecond=0)
    c = compute(race_id, rev, repo, reference)
    post = common.parse_dt(c.get("post_at"))
    if post is None:
        raise ValueError(f"{race_id}: 発走時刻が分からない（発走前の版が無い）")
    if now >= post:
        raise ValueError(f"{race_id}: もう発走している（{c['post_at']}）")
    record = {"schema": RECORD_SCHEMA, "race_id": race_id, "version": MAIN_VERSION, "strategy": STRATEGY,
              "created_at": now.isoformat(timespec="seconds"),
              "computed_from": {"rev": c["rev"], "engine": ps.VERSION, "engine_commit": c["engine_commit"]},
              **{k: c[k] for k in ("pinned", "post_at", "action", "tickets", "stake", "p_hit") if k in c},
              **{k: c[k] for k in OPTIONAL_KEYS if k in c},
              "unit_yen": UNIT_YEN, "used_for_prediction": False, "official_results_untouched": True, "note": NOTE}
    rel = record_rel(race_id, now)
    problems = validate(record, rel)
    if problems:
        raise ValueError(f"{race_id}: 記録が規約に合わない: {problems}")
    common.write_json(repo / rel, record)
    return rel, record


# ------------------------------------------------------------------ 形と規約（CI）

def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def validate(record: Any, rel: str | None = None) -> list[str]:
    """記録の形と規約。問題の短い名前の一覧（空なら OK）。"""
    if not isinstance(record, dict):
        return ["not_an_object"]
    problems = []
    keys = set(record)
    if REQUIRED_KEYS - keys:
        problems.append("missing:" + ",".join(sorted(REQUIRED_KEYS - keys)))
    if keys - REQUIRED_KEYS - OPTIONAL_KEYS:
        problems.append("unknown:" + ",".join(sorted(keys - REQUIRED_KEYS - OPTIONAL_KEYS)))
    if problems:
        return problems
    if record["schema"] != RECORD_SCHEMA:
        problems.append("schema")
    race_id = record["race_id"]
    if not isinstance(race_id, str) or not RACE_ID_RE.match(race_id):
        problems.append("race_id")
    if record["version"] != MAIN_VERSION or record["strategy"] != STRATEGY:
        problems.append("version_or_strategy")
    created = common.parse_dt(record["created_at"])
    if created is None or created.tzinfo is None:
        problems.append("created_at")
    post = common.parse_dt(record["post_at"])
    if post is None:
        problems.append("post_at")
    elif created is not None and created >= post:
        problems.append("created_after_post")
    cf = record["computed_from"]
    if not (isinstance(cf, dict) and set(cf) == {"rev", "engine", "engine_commit"}
            and isinstance(cf.get("rev"), str) and SHA_RE.match(cf["rev"])
            and isinstance(cf.get("engine_commit"), str) and SHA_RE.match(cf["engine_commit"])
            and cf.get("engine") == ps.VERSION):
        problems.append("computed_from")
    pinned = record["pinned"]
    if not (isinstance(pinned, dict) and isinstance(pinned.get("commit"), str) and SHA_RE.match(pinned["commit"])):
        problems.append("pinned")
    tickets = record["tickets"]
    ok_tickets = isinstance(tickets, list) and all(
        isinstance(t, list) and len(t) == 3 and all(isinstance(h, int) and not isinstance(h, bool) and h > 0 for h in t)
        and t == sorted(set(t)) for t in tickets)
    if not ok_tickets or len({tuple(t) for t in tickets}) != len(tickets) or len(tickets) > MAX_TICKETS \
            or tickets != sorted(tickets):
        problems.append("tickets")
    if record["unit_yen"] != UNIT_YEN or not isinstance(tickets, list) or record["stake"] != UNIT_YEN * len(tickets):
        problems.append("stake")
    action = record["action"]
    if action == "bet":
        if not tickets:
            problems.append("bet_without_tickets")
        p = record["p_hit"]
        if not isinstance(p, (int, float)) or isinstance(p, bool) or not 0 <= p <= 1:
            problems.append("p_hit")
    elif action == "pass":
        if tickets or not isinstance(record.get("reason"), str) or not record["reason"]:
            problems.append("pass_needs_reason_and_no_tickets")
    else:
        problems.append("action")
    if record["used_for_prediction"] is not False or record["official_results_untouched"] is not True:
        problems.append("flags")
    if any(URL_RE.search(s) for s in _strings(record)):
        problems.append("url")
    if rel is not None:
        parts = rel.split("/")
        expected_dir = f"{RECORDS_REL}/{race_id}"
        if "/".join(parts[:-1]) != expected_dir or not parts[-1].endswith(".json") \
                or not STAMP_RE.match(parts[-1][:-5]) \
                or (created is not None and parts[-1][:-5] != _stamp(created)):
            problems.append("path")
    return problems


def record_paths(repo: Path) -> list[Path]:
    return sorted((repo / RECORDS_REL).glob("*/*.json"))


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def validate_all(repo: Path | None = None) -> int:
    repo = repo or common.ROOT
    failed = 0
    paths = record_paths(repo)
    for path in paths:
        rel = path.relative_to(repo).as_posix()
        problems = validate(_load(path), rel)
        print(("NG " if problems else "OK ") + rel + (f"  {problems}" if problems else ""))
        failed += bool(problems)
    print(f"chat tickets: {len(paths)} checked, {failed} failed")
    return failed


# ------------------------------------------------------------------ 全履歴での確認（run_results）

def _history(rel: str, repo: Path) -> list[tuple[str, datetime.datetime]]:
    out = _git(["log", "--format=%H %cI", "--", rel], repo)
    rows = []
    for line in out.stdout.decode().splitlines() if out.returncode == 0 else []:
        sha, _, stamp = line.partition(" ")
        when = common.parse_dt(stamp)
        if sha and when:
            rows.append((sha, when))
    return sorted(rows, key=lambda r: r[1])


def _is_ancestor(a: str, b: str, repo: Path) -> bool:
    return _git(["merge-base", "--is-ancestor", a, b], repo).returncode == 0


def check_record(path: Path, repo: Path, verified: dict[str, Any], engine_commit: str,
                 reference: dict[str, Any] | None, as_of: datetime.date) -> dict[str, Any]:
    rel = path.relative_to(repo).as_posix()
    rec = _load(path)
    problems = validate(rec, rel)
    info: dict[str, Any] = {"record": rel}
    if problems:
        return {**info, "valid": False, "problems": problems}
    hist = _history(rel, repo)
    if not hist:
        return {**info, "valid": False, "problems": ["not_committed"]}
    first_sha, first_at = hist[0]
    info.update(commit=first_sha, committed_at=ma._iso(first_at), created_at=rec["created_at"],
                action=rec["action"], tickets=rec["tickets"], pinned_commit=rec["pinned"]["commit"])
    if ps._blob(first_sha, rel, repo) != ps._blob("HEAD", rel, repo):
        problems.append("modified_after_commit")
    found = ma.prerace_versions(rec["race_id"], repo)
    post = found["post"]
    if post is None:
        problems.append("post_time_unknown")
    else:
        if first_at >= post:
            problems.append("committed_after_post")
        if rec["post_at"] != ma._iso(post):
            problems.append("post_at_differs")
    if rec["pinned"]["commit"] not in {v["sha"] for v in found["versions"]}:
        problems.append("pinned_not_prerace_version")
    rev = rec["computed_from"]["rev"]
    if not _is_ancestor(rev, first_sha, repo):
        problems.append("computed_from_not_before_commit")
    if rec["computed_from"]["engine_commit"] != engine_commit:
        problems.append("engine_commit_differs")
    # 決め方どおりか：記録が見た版から計算し直す。記録と見た版は変わらないので、一度確かめた結果は台帳から使う
    blob = ps._blob("HEAD", rel, repo)
    cached = verified.get(rel)
    if not (isinstance(cached, dict) and cached.get("blob") == blob and cached.get("engine_commit") == engine_commit):
        try:
            c = compute(rec["race_id"], rev, repo, reference)
        except Exception as exc:  # 計算の失敗は台帳に入れない（直れば次の実行で確かめる）
            logger.exception("chat ticket の計算し直しに失敗: %s", rel)
            problems.append(f"recompute_error:{type(exc).__name__}")
            cached = None
        else:
            recomputed = {"action": c["action"], "tickets": c["tickets"],
                          "pinned_commit": (c.get("pinned") or {}).get("commit")}
            recorded = {"action": rec["action"], "tickets": rec["tickets"], "pinned_commit": rec["pinned"]["commit"]}
            cached = {"blob": blob, "engine_commit": engine_commit, "rev": rev, "recomputed": recomputed,
                      "rule_match": recomputed == recorded, "verified_on": as_of.isoformat()}
            verified[rel] = cached
    if cached is not None:
        info["rule_match"] = cached["rule_match"]
        if not cached["rule_match"]:
            problems.append("rule_mismatch")
    return {**info, "valid": not problems, "problems": problems}


def _realized(tickets: list[list[int]], result: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    finish = (result or {}).get("finish") or []
    if len(finish) < 3:
        return "pending_result", None
    top3 = sorted(int(h) for h in finish[:3])
    divs = ((result or {}).get("dividends") or {}).get(TRIO) or []
    if len(divs) >= 2:
        return "excluded:dead_heat_top3", {"top3": top3}
    hit = top3 in tickets
    div = next((d for d in divs if sorted(int(h) for h in d.get("horses") or []) == top3), None)
    payout = int(div["pay"]) * UNIT_YEN // 100 if (hit and div is not None) else 0
    return "evaluated", {"top3": top3, "hit": hit, "payout": payout, "dividend_found": div is not None}


def _main(race_id: str, repo: Path) -> dict[str, Any] | None:
    rec = ps._head_json(f"{SHADOW_RACES_REL}/{race_id}.json", repo)
    if not isinstance(rec, dict):
        return None
    s = (rec.get("strategies") or {}).get(STRATEGY) or {}
    return {"status": rec.get("status"), "pinned_commit": (rec.get("pinned") or {}).get("commit"),
            "tickets": sorted(sorted(t) for t in s.get("tickets") or []), "strategy_status": s.get("status")}


def audit(repo: Path | None = None, out_dir: Path | None = None, reference: dict[str, Any] | None = None,
          as_of: datetime.date | None = None) -> dict[str, Any]:
    repo = repo or common.ROOT
    out_dir = out_dir or OUT_DIR
    as_of = as_of or datetime.datetime.now(common.JST).date()
    base = {"schema": SUMMARY_SCHEMA, "generated_at": datetime.datetime.now(common.JST).isoformat(timespec="seconds"),
            "as_of": as_of.isoformat(), "version": MAIN_VERSION, "strategy": STRATEGY,
            "used_for_prediction": False, "official_results_untouched": True}
    reg = ps.registration(repo)
    if reg["status"] != "ok":
        summary = {**base, "status": reg["status"], "warnings": [f"engine_registration:{reg['status']}"]}
        common.write_json(out_dir / "summary.json", summary)
        return summary
    verified_path = out_dir / "verified.json"
    verified = common.read_json(verified_path) or {}
    results = ps._head_json(RESULTS_REL, repo) or {}
    by_race: dict[str, list[dict[str, Any]]] = {}
    checked = []
    for path in record_paths(repo):
        info = check_record(path, repo, verified, reg["commit"], reference, as_of)
        checked.append(info)
        by_race.setdefault(path.parent.name, []).append(info)
    common.write_json(verified_path, dict(sorted(verified.items())))

    warnings = []
    rows = []
    for race_id, infos in sorted(by_race.items()):
        valid = sorted((i for i in infos if i["valid"]), key=lambda i: i["committed_at"])
        row: dict[str, Any] = {"schema": RACE_SCHEMA, "race_id": race_id, "race_day": race_id[:8],
                               "records": infos, "used_for_prediction": False, "official_results_untouched": True}
        if not valid:
            rows.append({**row, "status": "no_valid_record"})
            continue
        shown = valid[-1]   # 発走前に最後に出した券
        row.update(shown=shown["record"], action=shown["action"], tickets=shown["tickets"],
                   stake=UNIT_YEN * len(shown["tickets"]))
        main = _main(race_id, repo)
        if main is not None:
            main["same_pinned"] = main["pinned_commit"] == shown["pinned_commit"]
            main["same_tickets"] = main["tickets"] == shown["tickets"]
            if main["same_pinned"] and not main["same_tickets"] and main["strategy_status"] == "chosen":
                warnings.append("differs_from_main_with_same_pinned")
        row["main"] = main
        status, realized = _realized(shown["tickets"], results.get(race_id))
        row["status"] = status
        if realized is not None:
            row["realized"] = realized
        rows.append(row)
    for r in rows:
        common.write_json(out_dir / "races" / f"{r['race_id']}.json", r)

    invalid = [{"record": i["record"], "problems": i["problems"]} for i in checked if not i["valid"]]
    if invalid:
        warnings.append("invalid_records")
    evaluated = [r for r in rows if r["status"] == "evaluated"]
    known = [r for r in evaluated if (not r["realized"]["hit"]) or r["realized"]["dividend_found"]]
    with_main = [r for r in rows if r.get("main")]
    summary = {
        **base,
        "status": "collecting",
        "warnings": sorted(set(warnings)),
        "engine_registration": {k: reg.get(k) for k in ("status", "commit", "registered_at")},
        "records": len(checked),
        "valid_records": sum(1 for i in checked if i["valid"]),
        "invalid_records": invalid,
        "races": len(rows),
        "bet_races": sum(1 for r in rows if r.get("action") == "bet"),
        "pass_races": sum(1 for r in rows if r.get("action") == "pass"),
        "pending_result": sorted(r["race_id"] for r in rows if r["status"] == "pending_result"),
        "excluded": sorted(r["race_id"] for r in rows if r["status"].startswith("excluded")),
        "evaluated": len(evaluated),
        "hits": sum(1 for r in evaluated if r["realized"]["hit"]),
        "stake": sum(r["stake"] for r in known),
        "payout": sum(r["realized"]["payout"] for r in known),
        "payout_unknown": sorted(r["race_id"] for r in evaluated if r not in known),
        "compared_with_main": len(with_main),
        "same_tickets_as_main": sum(1 for r in with_main if r["main"]["same_tickets"]),
        "same_pinned_as_main": sum(1 for r in with_main if r["main"]["same_pinned"]),
        "note": ("チャットで出した本線の参考券の記録の確認（observe-only）。的中・払戻は shadow で、正式成績・ROI・"
                 "portfolio-shadow-v1・edge-v1 の判定には入れない。本線の正式な前向きの記録は portfolio-shadow-v1 の x_out_2。"),
    }
    common.write_json(out_dir / "summary.json", summary)
    return summary


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="チャットで出した本線の参考券の記録（observe-only）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_make = sub.add_parser("make", help="本線の券を計算して記録を書く（コミットは別に行う）")
    p_make.add_argument("--race-id", nargs="+", required=True)
    p_make.add_argument("--rev", default="origin/main", help="計算に使う main の版（先に git fetch origin main する）")
    p_make.add_argument("--repo", type=Path, default=None)
    p_val = sub.add_parser("validate", help="記録の形と規約（履歴は見ない）")
    p_val.add_argument("--repo", type=Path, default=None)
    p_aud = sub.add_parser("audit", help="全履歴での確認と、本線の記録との比較")
    p_aud.add_argument("--repo", type=Path, default=None)
    p_aud.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if args.cmd == "make":
        failed = 0
        for race_id in args.race_id:
            try:
                rel, rec = make(race_id, args.repo, args.rev)
            except Exception as exc:
                logger.error("%s: 記録を作れなかった: %s", race_id, exc)
                failed += 1
                continue
            print(json.dumps({"record": rel, "action": rec["action"], "tickets": rec["tickets"],
                              "p_hit": rec["p_hit"], "reason": rec.get("reason"),
                              "pinned": rec["pinned"], "post_at": rec["post_at"]}, ensure_ascii=False))
        return 1 if failed else 0
    if args.cmd == "validate":
        return 1 if validate_all(args.repo) else 0
    summary = audit(args.repo, args.out)
    print(json.dumps({k: summary.get(k) for k in ("status", "warnings", "records", "valid_records", "evaluated",
                                                  "hits", "invalid_records")}, ensure_ascii=False))
    if summary.get("warnings"):
        for w in summary["warnings"]:
            logger.warning("chat tickets: %s", w)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
