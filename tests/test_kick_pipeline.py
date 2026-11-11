"""scripts/kick_pipeline.py の検証（開催日の予想パイプラインを起動し、発走前の凍結版ができたかを生成物で確かめる）。

ChatGPT レビュー PR #58 の場面：定時の実行が作られただけで待ちのまま・失敗・実行中に失敗・生成物がある・
最後までできない・区切りで次に任せる・gh の失敗。
"""
from __future__ import annotations

import datetime
import subprocess

from scripts import kick_pipeline as kp

DATE = "2026-11-14"


def _t(hhmm: str) -> datetime.datetime:
    return kp.at(DATE, hhmm)


def _snap(hhmm: str, pre_race: bool = True) -> dict:
    return {"path": "data/snapshots/20261114-tokyo-11.json", "frozen_at": _t(hhmm).isoformat(), "pre_race": pre_race}


def _run(hhmm: str, status: str, conclusion: str | None = None) -> dict:
    return {"createdAt": _t(hhmm).astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": status, "conclusion": conclusion}


# ------------------------------------------------------------------ 判断

def test_decide_uses_the_snapshot_not_the_run() -> None:
    cutoff = _t("14:45")
    assert kp.decide(_t("09:50"), _t("09:40"), cutoff, [], [], 0) == "wait"
    assert kp.decide(_t("09:50"), _t("09:51"), cutoff, [_snap("09:33")], [], 0) == "done"
    assert kp.decide(_t("09:50"), _t("09:51"), cutoff, [_snap("09:29")], [], 0) == "kick"         # 古い凍結版は数えない
    assert kp.decide(_t("09:50"), _t("09:51"), cutoff, [_snap("09:55", pre_race=False)], [], 0) == "kick"
    # 定時の実行が作られただけ（待ち）：止まっていなければ見張る。作られてから 25 分たてば起動する
    assert kp.decide(_t("09:50"), _t("09:51"), cutoff, [], [_run("09:47", "queued")], 0) == "watch"
    assert kp.decide(_t("09:50"), _t("10:13"), cutoff, [], [_run("09:47", "queued")], 0) == "kick"
    # 定時の実行が失敗した（終わったのに生成物が無い）：起動する
    assert kp.decide(_t("09:50"), _t("09:51"), cutoff, [], [_run("09:47", "completed", "failure")], 0) == "kick"
    # 成功したと言っていても、生成物が無ければ起動する
    assert kp.decide(_t("09:50"), _t("09:55"), cutoff, [], [_run("09:47", "completed", "success")], 0) == "kick"
    assert kp.decide(_t("09:50"), _t("10:30"), cutoff, [], [], kp.MAX_KICKS) == "watch"           # 起動は回数まで
    assert kp.decide(_t("12:50"), _t("14:45"), cutoff, [], [], 0) == "missed"


# ------------------------------------------------------------------ 1日分の通し

class _World:
    """時計・main の凍結版・予想パイプラインの実行をまとめて動かす。起動した実行は、ok なら build 分後に凍結版を作る。"""

    def __init__(self, start: str, outcomes: list[str], build: int = 4) -> None:
        self.now = _t(start)
        self.snaps: list[dict] = []
        self.runs: list[dict] = []
        self.outcomes = list(outcomes)
        self.build = datetime.timedelta(minutes=build)
        self.pending: list[tuple[datetime.datetime, dict, str]] = []
        self.kicked: list[datetime.datetime] = []

    def sleep(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)
        for when, run, outcome in list(self.pending):
            if self.now >= when:
                run["status"], run["conclusion"] = "completed", ("success" if outcome == "ok" else "failure")
                if outcome == "ok":
                    self.snaps.append({"frozen_at": when.isoformat(), "pre_race": True})
                self.pending.remove((when, run, outcome))

    def kick(self, repo: str) -> None:
        self.kicked.append(self.now)
        run = {"createdAt": self.now.isoformat(), "status": "in_progress", "conclusion": None}
        self.runs.append(run)
        self.pending.append((self.now + self.build, run, self.outcomes.pop(0) if self.outcomes else "ok"))

    def go(self, deadline: str = "18:00"):
        return kp.run(DATE, _t(deadline), "o/r", sleep=self.sleep, now_fn=lambda: self.now,
                      snapshots_fn=lambda d: list(self.snaps), runs_fn=lambda r: list(self.runs), kick_fn=self.kick)


def test_kicks_each_target_once_when_the_pipeline_works() -> None:
    w = _World("06:45", ["ok", "ok"])
    log, code = w.go()
    assert code == 0 and [e["result"] for e in log] == ["done", "done"]
    assert w.kicked == [_t("09:50"), _t("12:50")]


def test_a_queued_scheduled_run_is_watched_then_kicked_when_it_stalls() -> None:
    w = _World("09:00", ["ok", "ok"])
    w.runs.append(_run("09:47", "queued"))          # 定時の実行が作られたまま動かない
    log, code = w.go()
    assert code == 0 and log[0]["result"] == "done" and log[0]["kicks"] == 1
    assert w.kicked[0] >= _t("09:47") + kp.STALL     # 止まったと見てから起動した


def test_a_failed_run_is_retried_until_the_snapshot_exists() -> None:
    w = _World("09:00", ["fail", "ok", "ok"])
    log, code = w.go()
    assert code == 0 and log[0] == {"target": "09:50", "kicks": 2, "errors": [], "result": "done"}


def test_no_kick_when_the_scheduled_run_already_froze() -> None:
    w = _World("09:00", [])
    w.snaps.append(_snap("09:52"))
    w.snaps.append(_snap("12:48"))
    log, code = w.go()
    assert code == 0 and w.kicked == [] and [e["result"] for e in log] == ["done", "done"]


def test_missed_when_the_pipeline_never_freezes() -> None:
    w = _World("12:00", ["fail"] * 10)
    w.snaps.append(_snap("09:52"))
    log, code = w.go()
    assert code == 1 and log[0]["result"] == "done"
    assert log[1]["result"] == "missed" and log[1]["kicks"] == kp.MAX_KICKS


def test_deadline_leaves_the_rest_to_the_next_run() -> None:
    w = _World("06:45", ["ok"])
    log, code = w.go(deadline="11:00")
    assert code == 4 and [e["result"] for e in log] == ["done", "left_to_next_run"]


def test_gh_failures_are_retried() -> None:
    w = _World("09:50", ["ok", "ok"])
    calls = {"n": 0}

    def flaky_runs(repo: str):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise subprocess.CalledProcessError(1, "gh")
        return list(w.runs)

    log, code = kp.run(DATE, _t("18:00"), "o/r", sleep=w.sleep, now_fn=lambda: w.now,
                       snapshots_fn=lambda d: list(w.snaps), runs_fn=flaky_runs, kick_fn=w.kick)
    assert code == 0 and log[0]["result"] == "done" and log[0]["errors"] == ["CalledProcessError"] * 2
