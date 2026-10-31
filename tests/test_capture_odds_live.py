"""全レースのオッズ記録（scraper/capture_odds_live.py）のテスト。時計と取得は差し替え、ネットワークには出ない。"""
from __future__ import annotations

import datetime
import gzip
import json

from scraper import capture_odds_live as col
from scraper.fetchers.a_race_list import RaceListEntry

JST = col.JST


def _entry(post: str, race_no: int = 11, venue: str = "東京", surface: str = "芝",
           ref: str = "202605050211") -> RaceListEntry:
    return RaceListEntry(date="2026-11-01", day="日", venue=venue, race_no=race_no, name="（テスト用の名前）",
                         grade=None, post_time=post, source_ref=ref, surface=surface, dist=2000, heads=16)


class Clock:
    def __init__(self, hh: int, mm: int, ss: int = 0):
        self.t = datetime.datetime(2026, 11, 1, hh, mm, ss, tzinfo=JST)
        self.slept: list[float] = []

    def __call__(self) -> datetime.datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += datetime.timedelta(seconds=seconds)


BODIES = {
    "1": {"official_datetime": "2026-11-01 15:35:00",
          "odds": {"1": {"01": ["2.5", 0, 1], "02": ["5.0", 0, 2], "03": ["9.9", 0, 3]},
                   "2": {"01": ["1.1", "1.3", 1], "02": ["1.5", "2.0", 2], "03": ["2.0", "3.1", 3]}}},
    "7": {"official_datetime": "2026-11-01 15:35:00", "odds": {"7": {"010203": ["12.3", 0, 1]}}},
    "5": {"official_datetime": "2026-11-01 15:35:00", "odds": {"5": {"0102": ["1.5", "1.8", 1]}}},
    "4": {"official_datetime": "2026-11-01 15:35:00", "odds": {"4": {"0102": ["3.4", 0, 1]}}},
}


def _fetcher(clock: Clock, step: float = 4.0, calls: list | None = None):
    def fetch(source_ref: str, type_code: str) -> dict:
        clock.t += datetime.timedelta(seconds=step)
        if calls is not None:
            calls.append((source_ref, type_code))
        return BODIES[type_code]
    return fetch


def _read(path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()))


def _deadline(hh: int = 23) -> datetime.datetime:
    return datetime.datetime(2026, 11, 1, hh, 0, tzinfo=JST)


def test_race_rows_drop_jump_races_and_names():
    rows = col.race_rows([_entry("15:40"), _entry("14:20", race_no=8, surface="障", ref="202605050208")])
    assert [r["race_key"] for r in rows] == ["20261101-tokyo-11"]
    assert "name" not in rows[0]
    assert rows[0]["post_time"] == "15:40" and rows[0]["source_ref"] == "202605050211"


def test_waits_for_pre_and_writes_one_record(tmp_path):
    clock = Clock(15, 0)
    rc = col.run_once("2026-11-01", tmp_path, _deadline(), clock=clock, sleep=clock.sleep,
                      fetch_body=_fetcher(clock), fetch_list=lambda d: [_entry("15:40")])
    assert rc == col.EXIT_MORE
    assert clock.slept == [600.0]  # 15:10（発走30分前）まで待つ
    rec = _read(tmp_path / "20261101" / "20261101-tokyo-11" / "pre.json.gz")
    assert rec["status"] == "ok" and rec["phase"] == "pre"
    assert rec["markets"]["win"]["odds"] == {"1": 2.5, "2": 5.0, "3": 9.9}
    assert rec["markets"]["place"]["odds"]["2"] == [1.5, 2.0]
    assert rec["markets"]["sanrenpuku"]["odds"] == {"1-2-3": 12.3}
    assert rec["markets"]["wide"]["odds"] == {"1-2": [1.5, 1.8]}
    assert rec["markets"]["umaren"]["odds"] == {"1-2": 3.4}
    assert all(m["source_time"] == "2026-11-01 15:35:00" for m in rec["markets"].values())
    raw = (tmp_path / "20261101" / "20261101-tokyo-11" / "pre.json.gz").read_bytes()
    assert b"http" not in gzip.decompress(raw)
    assert json.loads((tmp_path / "20261101" / "races.json").read_text())["races"][0]["race_key"] == "20261101-tokyo-11"


def test_close_starts_on_time_and_drops_markets_finished_after_post(tmp_path):
    clock = Clock(15, 30)
    races = col.race_rows([_entry("15:40")])
    event = next(e for e in col.schedule(races) if e.phase == "close")
    assert not col._is_due(event, datetime.datetime(2026, 11, 1, 15, 36, 59, tzinfo=JST))
    assert col._is_due(event, datetime.datetime(2026, 11, 1, 15, 37, 0, tzinfo=JST))
    clock.t = datetime.datetime(2026, 11, 1, 15, 38, 30, tzinfo=JST)  # 発走1分30秒前に始めて、1回30秒かかる
    rec = col.observe(event, clock, _fetcher(clock, step=30))
    assert rec["markets"]["win"]["status"] == "ok"  # 15:39:00 に取得完了
    assert rec["markets"]["sanrenpuku"]["status"] == "ok"  # 15:39:30
    assert rec["markets"]["wide"]["status"] == "after_post" and "odds" not in rec["markets"]["wide"]
    assert rec["markets"]["umaren"]["status"] == "after_post"
    assert rec["status"] == "partial"


def test_late_start_marks_missed_and_still_takes_final(tmp_path):
    clock = Clock(16, 10)
    calls: list = []
    rc = col.run_once("2026-11-01", tmp_path, _deadline(), clock=clock, sleep=clock.sleep,
                      fetch_body=_fetcher(clock, calls=calls), fetch_list=lambda d: [_entry("15:40")])
    assert rc == col.EXIT_DONE
    day = tmp_path / "20261101" / "20261101-tokyo-11"
    assert _read(day / "pre.json.gz")["status"] == "missed"
    assert _read(day / "close.json.gz")["reason"] == "started_too_late"
    assert _read(day / "final.json.gz")["status"] == "ok"
    assert len(calls) == 4  # final の4券種だけ取った


def test_existing_records_are_not_fetched_again(tmp_path):
    clock = Clock(16, 10)
    col.run_once("2026-11-01", tmp_path, _deadline(), clock=clock, sleep=clock.sleep,
                 fetch_body=_fetcher(clock), fetch_list=lambda d: [_entry("15:40")])
    calls: list = []
    rc = col.run_once("2026-11-01", tmp_path, _deadline(), clock=clock, sleep=clock.sleep,
                      fetch_body=_fetcher(clock, calls=calls), fetch_list=lambda d: [])
    assert rc == col.EXIT_DONE and calls == []


def test_deadline_before_next_event_returns_timeout_without_waiting(tmp_path):
    clock = Clock(9, 0)
    rc = col.run_once("2026-11-01", tmp_path, datetime.datetime(2026, 11, 1, 12, 0, tzinfo=JST),
                      clock=clock, sleep=clock.sleep, fetch_body=_fetcher(clock),
                      fetch_list=lambda d: [_entry("15:40")])
    assert rc == col.EXIT_TIMEOUT and clock.slept == []


def test_final_waits_for_imminent_close_of_another_race(tmp_path):
    # A は 15:00 発走（final は 15:25）、B は 15:30 発走（close は 15:27）。15:26 に起きたら B の close を先に取る
    entries = [_entry("15:00", race_no=10, ref="202605050210"), _entry("15:30", race_no=11, ref="202605050211")]
    clock = Clock(15, 26)
    calls: list = []
    run = lambda: col.run_once("2026-11-01", tmp_path, _deadline(), clock=clock, sleep=clock.sleep,  # noqa: E731
                               fetch_body=_fetcher(clock, calls=calls), fetch_list=lambda d: entries)
    assert run() == col.EXIT_MORE  # 1回目：過ぎた pre・close を取り逃しとして残すだけ。A の final は後に回す
    assert calls == [] and clock.slept == []
    assert run() == col.EXIT_MORE  # 2回目：15:27 まで待って、B の close を先に取ってから A の final
    assert clock.slept == [60.0]
    assert calls[0][0] == "202605050211" and calls[4][0] == "202605050210"
    assert _read(tmp_path / "20261101" / "20261101-tokyo-11" / "close.json.gz")["status"] == "ok"
    assert _read(tmp_path / "20261101" / "20261101-tokyo-10" / "final.json.gz")["status"] == "ok"


def test_unavailable_race_list_is_not_saved_so_the_next_run_retries(tmp_path):
    def no_tab(date_str):
        raise RuntimeError("指定日のタブが日付タブ一覧に見つかりませんでした")
    clock = Clock(9, 0)
    for fetch_list in (no_tab, lambda d: []):
        rc = col.run_once("2026-11-03", tmp_path, _deadline(), clock=clock, sleep=clock.sleep,
                          fetch_body=_fetcher(clock), fetch_list=fetch_list)
        assert rc == col.EXIT_ERROR
        assert not (tmp_path / "20261103" / "races.json").exists()


def test_records_are_byte_identical_for_identical_content(tmp_path):
    rec = {"b": 1, "a": [1, 2]}
    col.write_record(tmp_path / "x.json.gz", rec)
    col.write_record(tmp_path / "y.json.gz", rec)
    assert (tmp_path / "x.json.gz").read_bytes() == (tmp_path / "y.json.gz").read_bytes()
    col.write_record(tmp_path / "x.json.gz", {"changed": True})  # 上書きしない
    assert _read(tmp_path / "x.json.gz") == rec
