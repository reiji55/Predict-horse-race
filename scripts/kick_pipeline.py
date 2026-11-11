"""開催日の予想パイプラインを、決めた時刻に起動する（odds-live の実行の中で動かす）。

GitHub の定時実行（schedule）は大きく遅れたり動かなかったりする（2026-11-07 は 9:47 の分が動かず、
11/8 は 9:47・12:47 の分が 15:47・19:49 に動いて、メインの発走前の凍結版が1つも無かった）。
odds-live は開催日の朝から夕方まで、自分で次の実行を起動しながら動き続けるので、その中から
workflow_dispatch で予想パイプラインを起動する（workflow_dispatch は遅れない）。

- 起動する時刻（JST）：TARGETS。CUTOFF を過ぎたら起動しない（メインの発走に間に合わないため）。
- 同じ時刻の分を二重に起動しない：その時刻の DEDUP_WINDOW 前より後に作られた予想パイプラインの実行が
  あれば（定時でも手動でも）起動しない。odds-live の実行がつながって何回動いても、1回だけになる。
- --deadline（odds-live の区切りの時刻）より後の時刻の分は、次の odds-live の実行に任せる。

予想・カード・成績には触れない。起動するだけ。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import subprocess
import sys
import time

JST = datetime.timezone(datetime.timedelta(hours=9))
TARGETS = ("09:50", "12:50")
CUTOFF = "14:45"
DEDUP_WINDOW = datetime.timedelta(minutes=20)
WORKFLOW = "run_pipeline.yml"

logger = logging.getLogger("kick_pipeline")


def at(date: str, hhmm: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(f"{date}T{hhmm}:00").replace(tzinfo=JST)


def decide(target: datetime.datetime, now: datetime.datetime, cutoff: datetime.datetime,
           run_created: list[datetime.datetime]) -> str:
    """その時刻の分をどうするか：wait（まだ早い）・skip_late・skip_recent_run・kick。"""
    if now < target:
        return "wait"
    if now >= cutoff:
        return "skip_late"
    if any(t >= target - DEDUP_WINDOW for t in run_created):
        return "skip_recent_run"
    return "kick"


def recent_runs(repo: str) -> list[datetime.datetime]:
    out = subprocess.run(["gh", "run", "list", "--repo", repo, "--workflow", WORKFLOW, "--limit", "30",
                          "--json", "createdAt"], capture_output=True, text=True, timeout=60, check=True)
    return [datetime.datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) for r in json.loads(out.stdout)]


def kick(repo: str) -> None:
    subprocess.run(["gh", "workflow", "run", WORKFLOW, "--repo", repo, "--ref", "main"],
                   check=True, timeout=60)


def run(date: str, deadline: datetime.datetime, repo: str, sleep=time.sleep,
        now_fn=lambda: datetime.datetime.now(JST), runs_fn=recent_runs, kick_fn=kick) -> list[dict]:
    cutoff = at(date, CUTOFF)
    log = []
    kicked: list[datetime.datetime] = []   # 起動した直後は gh run list に出ないことがあるので、自分で起動した分も数える
    for hhmm in TARGETS:
        target = at(date, hhmm)
        if target > deadline:
            log.append({"target": hhmm, "action": "left_to_next_run"})
            continue
        wait = (target - now_fn()).total_seconds()
        if wait > 0:
            sleep(wait)
        try:
            action = decide(target, now_fn(), cutoff, runs_fn(repo) + kicked)
            if action == "kick":
                kick_fn(repo)
                kicked.append(now_fn())
        except (subprocess.SubprocessError, OSError, ValueError, KeyError) as exc:
            # 起動に失敗しても odds-live は止めない。理由は型名だけ残す
            action = f"error:{type(exc).__name__}"
        logger.info("予想パイプライン %s の分：%s", hhmm, action)
        log.append({"target": hhmm, "action": action})
    return log


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="開催日の予想パイプラインを決めた時刻に起動する")
    p.add_argument("--date", required=True, help="対象日 YYYY-MM-DD（JST）")
    p.add_argument("--deadline", required=True, help="この実行の区切りの時刻（ISO 8601）")
    p.add_argument("--repo", required=True)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    deadline = datetime.datetime.fromisoformat(args.deadline.replace("Z", "+00:00"))
    print(json.dumps(run(args.date, deadline, args.repo), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
