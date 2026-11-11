"""開催日の予想パイプラインを、決めた時刻に起動し、発走前の凍結版ができたことを確かめる。

GitHub の定時実行（schedule）は大きく遅れたり動かなかったりする（2026-11-07 は 9:47 の分が動かず、
11/8 は 9:47・12:47 の分が 15:47・19:49 に動いて、メインの発走前の凍結版が1つも無かった）。
このスクリプトは、専用のワークフロー（run_pipeline_kick.yml）の中で動き、TARGETS の時刻ごとに：

1. その時刻まで待つ。
2. **できたかどうかは、生成物で決める**：main の data/snapshots/{その日}-*.json に、`pre_race: true` で
   `frozen_at` がその時刻の DONE_WINDOW 前より後のものがあれば、その時刻の分はできた（done）。
   予想パイプラインの実行が「作られた」「成功した」だけでは、できたとみなさない（ChatGPT レビュー PR #58）。
3. できていなければ：
   - 予想パイプラインの実行が待ち・実行中で、作られてから STALL 未満なら、待って見直す（watch）。
   - そうでなければ（実行が無い・終わったのに生成物が無い・止まっている）、workflow_dispatch で起動する（kick）。
     起動は1つの時刻につき MAX_KICKS 回まで。
   - POLL ごとに見直す。CUTOFF を過ぎてもできていなければ、その時刻の分は missed（警告を出し、終了コード 1）。
4. 実行の区切り（--deadline）が来たら、残りを次の実行に任せて終わる（終了コード 4。ワークフローが次を起動する）。

予想・カード・成績には触れない。起動と確認だけ。
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
from typing import Any, Callable

JST = datetime.timezone(datetime.timedelta(hours=9))
TARGETS = ("09:50", "12:50")
CUTOFF = "14:45"
DONE_WINDOW = datetime.timedelta(minutes=20)
STALL = datetime.timedelta(minutes=25)
POLL = datetime.timedelta(minutes=3)
MAX_KICKS = 3
WORKFLOW = "run_pipeline.yml"
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}

logger = logging.getLogger("kick_pipeline")


def at(date: str, hhmm: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(f"{date}T{hhmm}:00").replace(tzinfo=JST)


def _dt(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def done(target: datetime.datetime, snapshots: list[dict[str, Any]]) -> bool:
    """その時刻の分の発走前の凍結版が main にあるか。"""
    for s in snapshots:
        frozen = _dt(s.get("frozen_at"))
        if s.get("pre_race") is True and frozen is not None and frozen >= target - DONE_WINDOW:
            return True
    return False


def active_run(target: datetime.datetime, now: datetime.datetime, runs: list[dict[str, Any]]) -> bool:
    """その時刻の分を作っているとみなせる実行（待ち・実行中で、止まっていない）があるか。"""
    for r in runs:
        created = _dt(r.get("createdAt"))
        if created is None or created < target - DONE_WINDOW or r.get("status") not in ACTIVE:
            continue
        if now - created < STALL:
            return True
    return False


def decide(target: datetime.datetime, now: datetime.datetime, cutoff: datetime.datetime,
           snapshots: list[dict[str, Any]], runs: list[dict[str, Any]], kicks: int) -> str:
    """その時刻の分をどうするか：wait・done・missed・watch・kick。"""
    if now < target:
        return "wait"
    if done(target, snapshots):
        return "done"
    if now >= cutoff:
        return "missed"
    if active_run(target, now, runs):
        return "watch"
    if kicks >= MAX_KICKS:
        return "watch"          # 起動はもうしない。CUTOFF までは生成物を見続ける
    return "kick"


# ------------------------------------------------------------------ GitHub と Git

def snapshots_on_main(date: str, repo_dir: str = ".") -> list[dict[str, Any]]:
    subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=repo_dir, check=True, timeout=120)
    prefix = "data/snapshots/" + date.replace("-", "")
    names = subprocess.run(["git", "ls-tree", "--name-only", "origin/main", "data/snapshots/"], cwd=repo_dir,
                           capture_output=True, text=True, check=True, timeout=60).stdout.split()
    out = []
    for name in names:
        if not name.startswith(prefix + "-"):
            continue
        shown = subprocess.run(["git", "show", f"origin/main:{name}"], cwd=repo_dir, capture_output=True,
                               text=True, timeout=60)
        try:
            snap = json.loads(shown.stdout) if shown.returncode == 0 else {}
        except json.JSONDecodeError:
            snap = {}
        out.append({"path": name, "frozen_at": snap.get("frozen_at"), "pre_race": snap.get("pre_race")})
    return out


def pipeline_runs(repo: str) -> list[dict[str, Any]]:
    out = subprocess.run(["gh", "run", "list", "--repo", repo, "--workflow", WORKFLOW, "--limit", "30",
                          "--json", "databaseId,status,conclusion,createdAt,event"],
                         capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def dispatch(repo: str) -> None:
    subprocess.run(["gh", "workflow", "run", WORKFLOW, "--repo", repo, "--ref", "main"], check=True, timeout=60)


# ------------------------------------------------------------------ 1日分

def run(date: str, deadline: datetime.datetime, repo: str, *,
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(JST),
        snapshots_fn: Callable[[str], list] = snapshots_on_main,
        runs_fn: Callable[[str], list] = pipeline_runs,
        kick_fn: Callable[[str], None] = dispatch) -> tuple[list[dict[str, Any]], int]:
    """戻り値：(時刻ごとの記録, 終了コード 0＝すべて done／1＝missed あり／4＝区切りで残りを次に任せた)"""
    cutoff = at(date, CUTOFF)
    log: list[dict[str, Any]] = []
    for hhmm in TARGETS:
        target = at(date, hhmm)
        entry: dict[str, Any] = {"target": hhmm, "kicks": 0, "errors": []}
        my_kicks: list[datetime.datetime] = []   # 起動した直後は gh run list に出ないことがあるので、自分の起動も数える
        while True:
            now = now_fn()
            if now >= deadline:
                entry["result"] = "left_to_next_run"
                log.append(entry)
                return log, 4
            try:
                snaps = snapshots_fn(date) if now >= target else []
                runs = runs_fn(repo) if now >= target else []
            except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
                entry["errors"].append(type(exc).__name__)
                snaps, runs = [], []
                if now >= cutoff:
                    entry["result"] = "missed"
                    break
                sleep(POLL.total_seconds())
                continue
            runs = runs + [{"createdAt": t.isoformat(), "status": "queued"} for t in my_kicks]
            action = decide(target, now, cutoff, snaps, runs, entry["kicks"])
            if action == "wait":
                sleep(min((target - now).total_seconds(), max(1.0, (deadline - now).total_seconds())))
                continue
            if action in ("done", "missed"):
                entry["result"] = action
                break
            if action == "kick":
                try:
                    kick_fn(repo)
                    entry["kicks"] += 1
                    my_kicks.append(now)
                except (subprocess.SubprocessError, OSError) as exc:
                    entry["errors"].append(type(exc).__name__)
            sleep(POLL.total_seconds())
        logger.info("予想パイプライン %s の分：%s（起動 %d 回）", hhmm, entry["result"], entry["kicks"])
        log.append(entry)
    return log, (1 if any(e["result"] == "missed" for e in log) else 0)


def _summary(log: list[dict[str, Any]], date: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    lines = [f"### 予想パイプラインの起動と確認（{date}）", "", "| 時刻 | 結果 | 起動 | 失敗 |", "|---|---|---|---|"]
    lines += [f"| {e['target']} | {e.get('result')} | {e['kicks']} | {', '.join(e['errors']) or '—'} |" for e in log]
    for e in log:
        if e.get("result") == "missed":
            print(f"::warning title=予想パイプライン::{date} {e['target']} の分の発走前の凍結版が {CUTOFF} までにできなかった")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="開催日の予想パイプラインを決めた時刻に起動し、凍結版ができたか確かめる")
    p.add_argument("--date", required=True, help="対象日 YYYY-MM-DD（JST）")
    p.add_argument("--deadline", required=True, help="この実行の区切りの時刻（ISO 8601）")
    p.add_argument("--repo", required=True)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    deadline = _dt(args.deadline)
    if deadline is None:
        p.error("--deadline が読めない")
    log, code = run(args.date, deadline, args.repo)
    print(json.dumps(log, ensure_ascii=False))
    _summary(log, args.date)
    return code


if __name__ == "__main__":
    sys.exit(main())
