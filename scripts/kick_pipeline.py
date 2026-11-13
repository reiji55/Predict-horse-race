"""開催日の予想パイプラインを決めた時刻に起動し、**対象の全レース**の発走前の凍結版ができたことを確かめる。

GitHub の定時実行（schedule）は大きく遅れたり動かなかったりする（2026-11-07 は 9:47 の分が動かず、
11/8 は 9:47・12:47 の分が 15:47・19:49 に動いて、メインの発走前の凍結版が1つも無かった）。
このスクリプトは、専用のワークフロー（run_pipeline_kick.yml）の中で動き、TARGETS の時刻ごとに：

1. その時刻まで待つ。
2. **対象のレース**を決める：odds-live のその日のレース一覧（odds-live ブランチの {日付}/races/ の最新の版で、
   一覧の検査が ok のもの）から、予想パイプラインと同じ規則（config/scraper.json の main_race：11R、JRA の場、
   1日の上限）で選ぶ。一覧が無い・検査が ok でないときは「対象が分からない」とし、できたとはみなさない。
3. **できたかどうかは、対象の全レースの生成物で決める**：main の履歴にある data/snapshots/{race_id}.json の版のうち、
   `pre_race: true` で、`frozen_at` がその時刻の DONE_WINDOW 前から CUTOFF までの間にあり、発走時刻より前のもの
   が、対象のどのレースにもあれば、その時刻の分はできた（done）。1レースでも足りなければできていない。
   CUTOFF より後に凍結した版（11/7 京都の 15:42 のような版）は、この時刻の分には数えない。
4. できていなければ：
   - 予想パイプライン（main）の実行が待ち・実行中なら、待って見直す（watch）。run_pipeline.yml は
     concurrency で1つずつしか動かず、1回の実行は30分で打ち切られるので、ここでは重ねて起動しない。
   - 実行が無ければ（終わったのに足りない、を含む）、対象日と週を指定して workflow_dispatch で起動する（kick）。
     起動は、この監視の実行1回の中で、1つの時刻につき MAX_KICKS 回まで（区切りで次の実行に引き継ぐと数え直す）。
   - POLL ごとに見直す。次の時刻（最後の時刻は CUTOFF）までに足りなければ、その時刻の分は missed
     （足りないレースを残し、警告・終了コード 1）。前の時刻の分が、後の時刻を待たせることはない。
5. 実行の区切り（--deadline）が来たら、残りを次の実行に任せて終わる（終了コード 4。ワークフローが次を起動する）。

時刻の判断は Git の記録と生成物の frozen_at による。公開の時刻を暗号的に保証するものではない。
予想・カード・成績には触れない。起動と確認だけ。標準ライブラリだけで動く。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

JST = datetime.timezone(datetime.timedelta(hours=9))
TARGETS = ("09:50", "12:50")
CUTOFF = "14:45"
DONE_WINDOW = datetime.timedelta(minutes=20)
POLL = datetime.timedelta(minutes=3)
KICK_GRACE = datetime.timedelta(minutes=5)   # 起動した直後は gh run list に出ないことがあるので、この間は動いているとみなす
MAX_KICKS = 3
WORKFLOW = "run_pipeline.yml"
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
JRA_VENUES = {"札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"}
ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("kick_pipeline")


def at(date: str, hhmm: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(f"{date}T{hhmm}:00").replace(tzinfo=JST)


def iso_week(date: str) -> str:
    y, w, _ = datetime.date.fromisoformat(date).isocalendar()
    return f"{y}-W{w:02d}"


def _dt(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


# ------------------------------------------------------------------ 判断（純粋な関数）

def expected_races(race_list: dict[str, Any] | None, main_race: dict[str, Any]) -> list[str] | None:
    """odds-live の一覧から、予想パイプラインと同じ規則で対象のレースを選ぶ。一覧が使えなければ None（分からない）。"""
    if not isinstance(race_list, dict) or (race_list.get("check") or {}).get("status") != "ok":
        return None
    if main_race.get("mode", "race_no") != "race_no":
        return None          # 今は race_no だけに対応する（他の規則では対象が分からないとする）
    rows = [r for r in race_list.get("races") or []
            if r.get("race_no") == main_race.get("race_no", 11) and r.get("venue") in JRA_VENUES]
    rows.sort(key=lambda r: (r.get("venue"), r.get("race_no")))
    cap = main_race.get("max_per_day")
    if cap is not None:
        rows = rows[:cap]
    return [r["race_key"] for r in rows] or None


def race_done(versions: list[dict[str, Any]], date: str, target: datetime.datetime, cutoff: datetime.datetime) -> bool:
    """そのレースに、その時刻の分の発走前の凍結版（main の履歴のどれかの版）があるか。"""
    for v in versions:
        frozen = _dt(v.get("frozen_at"))
        post = at(date, v["post_time"]) if isinstance(v.get("post_time"), str) and len(v["post_time"]) == 5 else None
        if v.get("pre_race") is True and frozen is not None and target - DONE_WINDOW <= frozen <= cutoff \
                and (post is None or frozen < post):
            return True
    return False


def missing_races(expected: list[str] | None, versions: dict[str, list[dict[str, Any]]], date: str,
                  target: datetime.datetime, cutoff: datetime.datetime) -> list[str] | None:
    if expected is None:
        return None
    return [rid for rid in expected if not race_done(versions.get(rid) or [], date, target, cutoff)]


def active_run(runs: list[dict[str, Any]]) -> bool:
    return any(r.get("status") in ACTIVE for r in runs)


def decide(target: datetime.datetime, now: datetime.datetime, cutoff: datetime.datetime,
           missing: list[str] | None, runs: list[dict[str, Any]], kicks: int) -> str:
    """wait・done・missed・watch・kick。missing が None は対象が分からない（できたとはみなさない）。"""
    if now < target:
        return "wait"
    if missing == []:
        return "done"
    if now >= cutoff:
        return "missed"
    if active_run(runs) or kicks >= MAX_KICKS:
        return "watch"
    return "kick"


# ------------------------------------------------------------------ GitHub と Git

def _git(args: list[str], repo_dir: str) -> str:
    return subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True, check=True, timeout=120).stdout


def load_state(date: str, repo_dir: str = ".") -> dict[str, Any]:
    """odds-live の一覧と、main の履歴の snapshot の版。"""
    day = date.replace("-", "")
    _git(["fetch", "-q", "origin", "main"], repo_dir)
    try:   # odds-live がまだ無い・その日の一覧が無いときは「対象が分からない」（起動はする）
        _git(["fetch", "-q", "origin", "+refs/heads/odds-live:refs/remotes/origin/odds-live"], repo_dir)
        lists = sorted(n for n in _git(["ls-tree", "--name-only", "origin/odds-live", f"{day}/races/"], repo_dir).split()
                       if n.endswith(".json"))
    except subprocess.CalledProcessError:
        lists = []
    race_list = json.loads(_git(["show", f"origin/odds-live:{lists[-1]}"], repo_dir)) if lists else None
    main_race = json.loads((Path(repo_dir) / "config" / "scraper.json").read_text(encoding="utf-8"))["main_race"]
    expected = expected_races(race_list, main_race)
    versions: dict[str, list[dict[str, Any]]] = {}
    for rid in expected or []:
        rel = f"data/snapshots/{rid}.json"
        rows = []
        for sha in _git(["log", "--first-parent", "--format=%H", "origin/main", "--", rel], repo_dir).split():
            try:
                snap = json.loads(_git(["show", f"{sha}:{rel}"], repo_dir))
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                continue
            rows.append({"commit": sha, "frozen_at": snap.get("frozen_at"), "pre_race": snap.get("pre_race"),
                         "post_time": snap.get("post_time")})
        versions[rid] = rows
    return {"expected": expected, "list_status": ((race_list or {}).get("check") or {}).get("status"),
            "versions": versions}


def pipeline_runs(repo: str) -> list[dict[str, Any]]:
    out = subprocess.run(["gh", "run", "list", "--repo", repo, "--workflow", WORKFLOW, "--branch", "main",
                          "--limit", "30", "--json", "databaseId,status,conclusion,createdAt,event"],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def dispatch(repo: str, date: str) -> None:
    subprocess.run(["gh", "workflow", "run", WORKFLOW, "--repo", repo, "--ref", "main",
                    "-f", f"dates={date}", "-f", f"week_id={iso_week(date)}"], check=True, timeout=60)


# ------------------------------------------------------------------ 1日分

def run(date: str, deadline: datetime.datetime, repo: str, *,
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(JST),
        state_fn: Callable[[str], dict[str, Any]] = load_state,
        runs_fn: Callable[[str], list] = pipeline_runs,
        kick_fn: Callable[[str, str], None] = dispatch) -> tuple[list[dict[str, Any]], int]:
    """戻り値：(時刻ごとの記録, 終了コード 0＝すべて done／1＝missed あり／4＝区切りで残りを次に任せた)"""
    cutoff = at(date, CUTOFF)
    log: list[dict[str, Any]] = []
    for i, hhmm in enumerate(TARGETS):
        target = at(date, hhmm)
        # この時刻の分を見るのは次の時刻まで（最後の時刻は CUTOFF まで）。前の時刻の分がそろわないまま、後の時刻を待たせない
        window_end = min(at(date, TARGETS[i + 1]), cutoff) if i + 1 < len(TARGETS) else cutoff
        entry: dict[str, Any] = {"target": hhmm, "kicks": 0, "errors": [], "missing": None, "expected": None}
        my_kicks: list[datetime.datetime] = []
        while True:
            now = now_fn()
            if now >= deadline:
                entry["result"] = "left_to_next_run"
                log.append(entry)
                return log, 4
            if now < target:
                sleep(min((target - now).total_seconds(), max(1.0, (deadline - now).total_seconds())))
                continue
            try:
                state = state_fn(date)
                runs = runs_fn(repo)
            except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError) as exc:
                entry["errors"].append(type(exc).__name__)
                if now_fn() >= window_end:
                    entry["result"] = "missed"
                    break
                sleep(POLL.total_seconds())
                continue
            now = now_fn()                      # Git・gh を待った分だけ時計を進めてから判断する
            entry["expected"] = state["expected"]
            missing = missing_races(state["expected"], state["versions"], date, target, cutoff)
            entry["missing"] = missing
            runs = runs + [{"createdAt": t.isoformat(), "status": "queued"} for t in my_kicks if now - t < KICK_GRACE]
            action = decide(target, now, window_end, missing, runs, entry["kicks"])
            if action in ("done", "missed"):
                entry["result"] = action if missing is not None else "missed_unknown_races"
                break
            if action == "kick":
                now = now_fn()
                if now < window_end and now < deadline:   # 起動の直前にもう一度時計を見る
                    try:
                        kick_fn(repo, date)
                        entry["kicks"] += 1
                        my_kicks.append(now)
                    except (subprocess.SubprocessError, OSError) as exc:
                        entry["errors"].append(type(exc).__name__)
            sleep(POLL.total_seconds())
        logger.info("予想パイプライン %s の分：%s（起動 %d 回、足りないレース %s）",
                    hhmm, entry["result"], entry["kicks"], entry["missing"])
        log.append(entry)
    return log, (1 if any(e["result"] != "done" for e in log) else 0)


def _summary(log: list[dict[str, Any]], date: str) -> None:
    lines = [f"### 予想パイプラインの起動と確認（{date}）", "",
             "| 時刻 | 結果 | 起動 | 対象 | 足りないレース | 失敗 |", "|---|---|---|---|---|---|"]
    for e in log:
        exp = "分からない" if e.get("expected") is None else ", ".join(e["expected"])
        miss = "—" if e.get("missing") in (None, []) else ", ".join(e["missing"])
        lines.append(f"| {e['target']} | {e.get('result')} | {e['kicks']} | {exp} | {miss} | {', '.join(e['errors']) or '—'} |")
        if e.get("result", "").startswith("missed"):
            print(f"::warning title=予想パイプライン::{date} {e['target']} の分の発走前の凍結版が {CUTOFF} までにそろわなかった"
                  f"（足りない：{miss}、対象：{exp}）")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="開催日の予想パイプラインを起動し、対象の全レースの凍結版ができたか確かめる")
    p.add_argument("--date", required=True, help="対象日 YYYY-MM-DD（JST）")
    p.add_argument("--deadline", required=True, help="この実行の区切りの時刻（ISO 8601）")
    p.add_argument("--repo", required=True)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    deadline = _dt(args.deadline)
    if deadline is None:
        p.error("--deadline が読めない")
    try:
        datetime.date.fromisoformat(args.date)
    except ValueError:
        p.error("--date が読めない")
    log, code = run(args.date, deadline, args.repo)
    print(json.dumps(log, ensure_ascii=False))
    _summary(log, args.date)
    return code


if __name__ == "__main__":
    sys.exit(main())
