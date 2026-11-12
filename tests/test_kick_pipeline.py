"""scripts/kick_pipeline.py の検証（開催日の予想パイプラインを起動し、対象の全レースの発走前の凍結版を確かめる）。

ChatGPT レビュー PR #58 の場面：作られただけの実行・失敗・3場中1場だけ・一覧が分からない／欠け・
CUTOFF 後の凍結・実行を重ねない・対象日の指定・区切り・gh の失敗。
"""
from __future__ import annotations

import datetime
import subprocess

from scripts import kick_pipeline as kp

DATE = "2026-11-14"
RACES = ["20261114-kyoto-11", "20261114-tokyo-11", "20261114-fukushima-11"]   # 予想パイプラインと同じ並び（場名の順）
MAIN = {"mode": "race_no", "race_no": 11, "max_per_day": 6}


def _t(hhmm: str) -> datetime.datetime:
    return kp.at(DATE, hhmm)


def _list(status: str = "ok", venues=("福島", "京都", "東京")) -> dict:
    roma = {"福島": "fukushima", "京都": "kyoto", "東京": "tokyo"}
    races = [{"race_key": f"20261114-{roma[v]}-{n:02d}", "venue": v, "race_no": n}
             for v in venues for n in (10, 11, 12)]
    return {"check": {"status": status}, "races": races}


def _v(hhmm: str, pre_race: bool = True, post: str = "15:40") -> dict:
    return {"frozen_at": _t(hhmm).isoformat(), "pre_race": pre_race, "post_time": post}


# ------------------------------------------------------------------ 判断

def test_expected_races_follow_the_pipeline_rule_and_fail_closed() -> None:
    assert kp.expected_races(_list(), MAIN) == RACES
    assert kp.expected_races(_list(status="partial_unverified"), MAIN) is None     # 一覧の検査が ok でない
    assert kp.expected_races(None, MAIN) is None                                   # 一覧が無い
    assert kp.expected_races(_list(), {**MAIN, "mode": "grade"}) is None            # 対応していない規則
    assert kp.expected_races(_list(), {**MAIN, "max_per_day": 2}) == RACES[:2]
    assert kp.iso_week(DATE) == "2026-W46"


def test_race_done_checks_window_cutoff_and_post() -> None:
    cut = _t("14:45")
    assert kp.race_done([_v("09:52")], DATE, _t("09:50"), cut)
    assert not kp.race_done([_v("09:29")], DATE, _t("09:50"), cut)                 # 古い
    assert not kp.race_done([_v("15:42", post="15:45")], DATE, _t("12:50"), cut)   # CUTOFF より後（11/7 京都の 15:42）
    assert not kp.race_done([_v("13:00", post="12:55")], DATE, _t("12:50"), cut)   # 発走後
    assert not kp.race_done([_v("13:00", pre_race=False)], DATE, _t("12:50"), cut)
    assert kp.race_done([_v("15:42", post="15:45"), _v("13:28")], DATE, _t("12:50"), cut)   # 履歴のどれかの版でよい


def test_decide_needs_every_race_and_never_stacks_runs() -> None:
    cut = _t("14:45")
    assert kp.decide(_t("09:50"), _t("09:51"), cut, [], [], 0) == "done"
    assert kp.decide(_t("09:50"), _t("09:51"), cut, ["x"], [], 0) == "kick"
    assert kp.decide(_t("09:50"), _t("09:51"), cut, None, [], 0) == "kick"          # 対象が分からなくても起動はする
    assert kp.decide(_t("09:50"), _t("10:40"), cut, ["x"], [{"status": "in_progress"}], 0) == "watch"   # 重ねない
    assert kp.decide(_t("09:50"), _t("09:51"), cut, ["x"], [{"status": "completed"}], 0) == "kick"
    assert kp.decide(_t("09:50"), _t("09:51"), cut, ["x"], [], kp.MAX_KICKS) == "watch"
    assert kp.decide(_t("12:50"), _t("14:45"), cut, ["x"], [], 0) == "missed"


# ------------------------------------------------------------------ 1日分の通し

class _World:
    """時計・odds-live の一覧・main の snapshot の版・予想パイプラインの実行（1つずつ）をまとめて動かす。

    起動した実行は build 分後に終わり、ok_races のレースの凍結版を作る（指定が無ければ全レース）。
    """

    def __init__(self, start: str, plan: list[set[str] | None], build: int = 4, race_list: dict | None = None) -> None:
        self.now = _t(start)
        self.race_list = race_list if race_list is not None else _list()
        self.versions: dict[str, list[dict]] = {}
        self.runs: list[dict] = []
        self.plan = list(plan)
        self.build = datetime.timedelta(minutes=build)
        self.kicked: list[tuple[datetime.datetime, str]] = []

    def _advance(self) -> None:
        for run in self.runs:
            if run["status"] == "in_progress" and self.now >= run["_end"]:
                run["status"] = "completed"
                for rid in run["_ok"]:
                    self.versions.setdefault(rid, []).append(_v(run["_end"].strftime("%H:%M")))

    def sleep(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)
        self._advance()

    def state(self, date: str) -> dict:
        expected = kp.expected_races(self.race_list, MAIN)
        return {"expected": expected, "versions": {r: list(self.versions.get(r, [])) for r in expected or []}}

    def kick(self, repo: str, date: str) -> None:
        assert not any(r["status"] == "in_progress" for r in self.runs), "実行を重ねた"
        self.kicked.append((self.now, date))
        ok = self.plan.pop(0) if self.plan else None
        self.runs.append({"status": "in_progress", "_end": self.now + self.build, "_ok": set(RACES) if ok is None else ok})

    def go(self, deadline: str = "18:00"):
        return kp.run(DATE, _t(deadline), "o/r", sleep=self.sleep, now_fn=lambda: self.now,
                      state_fn=self.state, runs_fn=lambda r: [{"status": x["status"]} for x in self.runs],
                      kick_fn=self.kick)


def test_each_target_needs_all_races() -> None:
    w = _World("06:45", [None, None])
    log, code = w.go()
    assert code == 0 and [e["result"] for e in log] == ["done", "done"]
    assert [t for t, _ in w.kicked] == [_t("09:50"), _t("12:50")] and all(d == DATE for _, d in w.kicked)


def test_one_venue_only_is_not_done_and_is_retried() -> None:
    w = _World("09:00", [{RACES[2]}, None, None])       # 1回目は東京だけ
    log, code = w.go()
    assert code == 0 and log[0]["result"] == "done" and log[0]["kicks"] == 2


def test_missing_races_are_reported_at_cutoff() -> None:
    w = _World("12:00", [{RACES[2]}] * 10)
    w.versions = {r: [_v("09:52")] for r in RACES}       # 朝の分はそろっている
    log, code = w.go()
    assert code == 1 and log[0]["result"] == "done"
    assert log[1]["result"] == "missed" and log[1]["missing"] == RACES[:2] and log[1]["kicks"] == kp.MAX_KICKS


def test_unknown_or_partial_race_list_is_never_done() -> None:
    for race_list in ({"check": {"status": "partial_unverified"}, "races": []}, {}):
        w = _World("12:40", [None] * 5, race_list=race_list)
        log, code = w.go()
        assert code == 1 and log[1]["result"] == "missed_unknown_races" and log[1]["kicks"] >= 1   # 起動はする


def test_a_late_freeze_does_not_count_for_earlier_targets() -> None:
    w = _World("15:50", [])
    w.versions = {r: [_v("15:42", post="15:45")] for r in RACES}
    log, code = w.go()
    assert code == 1 and [e["result"] for e in log] == ["missed", "missed"] and w.kicked == []


def test_a_running_pipeline_is_waited_for_not_stacked() -> None:
    w = _World("09:50", [None], build=40)                 # 長い実行：終わるまで重ねない
    w.runs.append({"status": "in_progress", "_end": _t("10:20"), "_ok": set()})   # 9:47 の定時が動いている（失敗する）
    log, code = w.go(deadline="11:30")
    assert code == 4 and log[0]["result"] == "done"
    assert w.kicked and w.kicked[0][0] >= _t("10:20")    # 先の実行が終わってから起動した


def test_deadline_and_gh_failures() -> None:
    w = _World("06:45", [None])
    log, code = w.go(deadline="11:00")
    assert code == 4 and [e["result"] for e in log] == ["done", "left_to_next_run"]

    w = _World("09:50", [None, None])
    calls = {"n": 0}

    def flaky(repo: str):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise subprocess.CalledProcessError(1, "gh")
        return [{"status": x["status"]} for x in w.runs]

    log, code = kp.run(DATE, _t("18:00"), "o/r", sleep=w.sleep, now_fn=lambda: w.now, state_fn=w.state,
                       runs_fn=flaky, kick_fn=w.kick)
    assert code == 0 and log[0]["result"] == "done" and log[0]["errors"] == ["CalledProcessError"] * 2
