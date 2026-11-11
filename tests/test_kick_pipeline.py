"""scripts/kick_pipeline.py の検証（開催日の予想パイプラインを決めた時刻に起動する）。"""
from __future__ import annotations

import datetime
import subprocess

from scripts import kick_pipeline as kp

DATE = "2026-11-14"


def _t(hhmm: str) -> datetime.datetime:
    return kp.at(DATE, hhmm)


def test_decide() -> None:
    cutoff = _t("14:45")
    assert kp.decide(_t("09:50"), _t("09:40"), cutoff, []) == "wait"
    assert kp.decide(_t("09:50"), _t("09:50"), cutoff, []) == "kick"
    assert kp.decide(_t("09:50"), _t("09:51"), cutoff, [_t("09:35")]) == "skip_recent_run"   # 20 分以内に動いた
    assert kp.decide(_t("09:50"), _t("09:51"), cutoff, [_t("09:25")]) == "kick"              # それより前は数えない
    assert kp.decide(_t("12:50"), _t("14:45"), cutoff, []) == "skip_late"


class _Clock:
    def __init__(self, start: datetime.datetime) -> None:
        self.now = start

    def sleep(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)


def test_run_kicks_each_target_once_and_waits_until_it() -> None:
    clock, kicked, runs = _Clock(_t("06:45")), [], []

    def kick_fn(repo: str) -> None:
        kicked.append(clock.now)
        runs.append(clock.now)          # 起動した実行は、次の確かめで「最近の実行」に入る

    log = kp.run(DATE, _t("18:00"), "o/r", sleep=clock.sleep, now_fn=lambda: clock.now,
                 runs_fn=lambda repo: list(runs), kick_fn=kick_fn)
    assert kicked == [_t("09:50"), _t("12:50")]
    assert [x["action"] for x in log] == ["kick", "kick"]


def test_run_skips_when_the_schedule_already_ran_and_leaves_late_targets_to_the_next_run() -> None:
    clock = _Clock(_t("09:00"))
    log = kp.run(DATE, _t("11:00"), "o/r", sleep=clock.sleep, now_fn=lambda: clock.now,
                 runs_fn=lambda repo: [_t("09:47")], kick_fn=lambda repo: (_ for _ in ()).throw(AssertionError))
    assert log == [{"target": "09:50", "action": "skip_recent_run"}, {"target": "12:50", "action": "left_to_next_run"}]


def test_run_does_not_stop_odds_live_when_gh_fails() -> None:
    clock = _Clock(_t("12:55"))

    def runs_fn(repo: str):
        raise subprocess.CalledProcessError(1, "gh")

    log = kp.run(DATE, _t("18:00"), "o/r", sleep=clock.sleep, now_fn=lambda: clock.now, runs_fn=runs_fn,
                 kick_fn=lambda repo: None)
    assert log == [{"target": "09:50", "action": "error:CalledProcessError"},
                   {"target": "12:50", "action": "error:CalledProcessError"}]


def test_run_started_late_kicks_only_once() -> None:
    clock, kicked = _Clock(_t("13:10")), []
    log = kp.run(DATE, _t("18:00"), "o/r", sleep=clock.sleep, now_fn=lambda: clock.now,
                 runs_fn=lambda repo: [], kick_fn=lambda repo: kicked.append(clock.now))   # gh に出るのが遅い
    assert kicked == [_t("13:10")]
    assert [x["action"] for x in log] == ["kick", "skip_recent_run"]
