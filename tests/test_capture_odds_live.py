"""全レースのオッズ記録（scraper/capture_odds_live.py）のテスト。時計と取得は差し替え、ネットワークには出ない。"""
from __future__ import annotations

import datetime
import gzip
import json

from scraper import capture_odds_live as col
from scraper.fetchers.a_race_list import RaceListEntry

JST = col.JST


def _t(hh: int, mm: int, ss: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 11, 1, hh, mm, ss, tzinfo=JST)


def _entry(post: str, race_no: int = 11, venue: str = "東京", surface: str = "芝",
           ref: str = "202605050211") -> RaceListEntry:
    return RaceListEntry(date="2026-11-01", day="日", venue=venue, race_no=race_no, name="（テスト用の名前）",
                         grade=None, post_time=post, source_ref=ref, surface=surface, dist=2000, heads=16)


def _full_day(venue: str = "東京") -> list[RaceListEntry]:
    """1〜12R がそろった一覧（欠けていない一覧の検査を通す）。"""
    return [_entry(f"{9 + n // 2:02d}:{(n % 2) * 30:02d}", race_no=n, venue=venue, ref=f"2026050502{n:02d}")
            for n in range(1, 13)]


class Clock:
    def __init__(self, t: datetime.datetime):
        self.t = t
        self.slept: list[float] = []

    def __call__(self) -> datetime.datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += datetime.timedelta(seconds=seconds)


def _body(type_code: str, source: str | None, bump: float = 0.0) -> dict:
    odds = {
        "1": {"1": {"01": [f"{2.5 + bump}", 0, 1], "02": ["5.0", 0, 2], "03": ["9.9", 0, 3]},
              "2": {"01": ["1.1", "1.3", 1], "02": ["1.5", "2.0", 2], "03": ["2.0", "3.1", 3]}},
        "7": {"7": {"010203": [f"{12.3 + bump}", 0, 1]}},
        "5": {"5": {"0102": ["1.5", "1.8", 1]}},
        "4": {"4": {"0102": ["3.4", 0, 1]}},
    }[type_code]
    return {"official_datetime": source, "odds": odds}


def _fetcher(clock: Clock, step: float = 4.0, calls: list | None = None, lag_min: float = 5.0,
             source: object = "lag", bump: float = 0.0):
    """取得元の時刻は、既定では取得の lag_min 分前（現実の形）。source に文字列か None を渡すとそれを返す。"""
    def fetch(source_ref: str, type_code: str) -> dict:
        clock.t += datetime.timedelta(seconds=step)
        if calls is not None:
            calls.append((source_ref, type_code))
        if source == "lag":
            st = (clock.t - datetime.timedelta(minutes=lag_min)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            st = source
        return _body(type_code, st, bump)
    return fetch


def _read(path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()))


DEADLINE = _t(23, 0)
DAY = "20261101"
RACE = "20261101-tokyo-11"


def _run(tmp_path, clock, fetch, entries):
    return col.run_once("2026-11-01", tmp_path, DEADLINE, clock=clock, sleep=clock.sleep,
                        fetch_body=fetch, fetch_list=lambda d: entries)


def test_race_rows_drop_jump_races_and_names():
    rows = col.race_rows([_entry("15:40"), _entry("14:20", race_no=8, surface="障", ref="202605050208")])
    assert [r["race_key"] for r in rows] == [RACE]
    assert "name" not in rows[0]
    assert rows[0]["post_time"] == "15:40" and rows[0]["source_ref"] == "202605050211"


def test_waits_for_pre_and_writes_one_verified_record(tmp_path):
    clock = Clock(_t(15, 0))
    assert _run(tmp_path, clock, _fetcher(clock), [_entry("15:40")]) == col.EXIT_MORE
    assert clock.slept == [600.0]  # 15:10（発走30分前）まで待つ
    rec = _read(tmp_path / DAY / RACE / "pre.json.gz")
    assert rec["status"] == "ok" and rec["phase"] == "pre" and rec["postrace"] is False
    assert rec["markets"]["win"]["odds"] == {"1": 2.5, "2": 5.0, "3": 9.9}
    assert rec["markets"]["place"]["odds"]["2"] == [1.5, 2.0]
    assert rec["markets"]["sanrenpuku"]["odds"] == {"1-2-3": 12.3}
    assert rec["markets"]["wide"]["odds"] == {"1-2": [1.5, 1.8]}
    assert rec["markets"]["umaren"]["odds"] == {"1-2": 3.4}
    for m in rec["markets"].values():  # 取得元の時刻は取得を終えた時刻より前で、発走より前
        assert m["source_time_jst"] < m["observed_at"] < rec["post_at"]
    assert b"http" not in gzip.decompress((tmp_path / DAY / RACE / "pre.json.gz").read_bytes())
    versions = list((tmp_path / DAY / "races").glob("*.json"))
    assert len(versions) == 1 and json.loads(versions[0].read_text())["races"][0]["race_key"] == RACE


def test_source_time_is_parsed_and_checked():
    post = _t(15, 40)
    assert col.parse_source_time("2026-11-01 15:35:00") == _t(15, 35)
    assert col.parse_source_time("2026/11/01 15:35") is None and col.parse_source_time(None) is None
    ms = col.market_status
    assert ms("pre", _t(15, 10), _t(14, 35), post, 3) == "ok"
    assert ms("pre", _t(15, 10), None, post, 3) == "source_time_unknown"
    assert ms("pre", _t(15, 10), _t(15, 35), post, 3) == "source_time_future"  # 取ったときに無い時刻
    assert ms("pre", _t(15, 10), _t(15, 10, 50), post, 3) == "ok"  # 時計のずれは60秒まで
    assert ms("close", _t(15, 41), _t(15, 35), post, 3) == "after_post"
    assert ms("close", _t(15, 39), _t(15, 40), post, 3) == "source_after_post"
    assert ms("post1", _t(16, 5), _t(15, 41), post, 3) == "ok"
    assert ms("post1", _t(16, 5), _t(16, 30), post, 3) == "source_time_future"


def test_pre_with_future_or_missing_source_time_is_not_ok(tmp_path):
    race = col.race_rows([_entry("15:40")])
    event = next(e for e in col.schedule(race) if e.phase == "pre")
    clock = Clock(_t(15, 10))
    rec = col.observe(event, clock, _fetcher(clock, source="2026-11-01 15:35:00"))
    assert rec["status"] == "failed"
    assert {m["status"] for m in rec["markets"].values()} == {"source_time_future"}
    assert all("odds" not in m for m in rec["markets"].values())  # 発走前の値として使えない表は残さない
    rec = col.observe(event, clock, _fetcher(clock, source=None))
    assert rec["status"] == "failed" and {m["status"] for m in rec["markets"].values()} == {"source_time_unknown"}
    rec = col.observe(event, clock, _fetcher(clock, source="15:35"))  # 読めない書式
    assert {m["status"] for m in rec["markets"].values()} == {"source_time_unknown"}


def test_close_starts_on_time_and_drops_markets_finished_after_post():
    races = col.race_rows([_entry("15:40")])
    event = next(e for e in col.schedule(races) if e.phase == "close")
    assert not col._is_due(event, _t(15, 36, 59))
    assert col._is_due(event, _t(15, 37))
    clock = Clock(_t(15, 38, 30))  # 発走1分30秒前に始めて、1回30秒かかる
    rec = col.observe(event, clock, _fetcher(clock, step=30))
    assert rec["markets"]["win"]["status"] == "ok"  # 15:39:00 に取得完了
    assert rec["markets"]["sanrenpuku"]["status"] == "ok"  # 15:39:30
    assert rec["markets"]["wide"]["status"] == "after_post" and "odds" not in rec["markets"]["wide"]
    assert rec["markets"]["umaren"]["status"] == "after_post"
    assert rec["status"] == "partial"


def test_late_start_marks_missed_and_post2_verifies_final_odds(tmp_path):
    clock = Clock(_t(16, 10))
    calls: list = []
    fetch = _fetcher(clock, calls=calls, lag_min=3)
    assert _run(tmp_path, clock, fetch, [_entry("15:40")]) == col.EXIT_MORE
    day = tmp_path / DAY / RACE
    assert _read(day / "pre.json.gz")["status"] == "missed"
    assert _read(day / "close.json.gz")["reason"] == "started_too_late"
    post1 = _read(day / "post1.json.gz")
    assert post1["status"] == "ok" and post1["postrace"] is True and "final_check" not in post1
    assert len(calls) == 4
    assert _run(tmp_path, clock, fetch, [_entry("15:40")]) == col.EXIT_DONE  # 16:40 まで待って post2
    post2 = _read(day / "post2.json.gz")
    assert post2["final_check"]["verified"] is True


def test_stale_pre_close_version_is_never_promoted_to_final(tmp_path):
    clock = Clock(_t(16, 10))
    fetch = _fetcher(clock, source="2026-11-01 15:35:00")  # 発走後も締め切り前（15:35）の版を返し続ける
    _run(tmp_path, clock, fetch, [_entry("15:40")])
    _run(tmp_path, clock, fetch, [_entry("15:40")])
    check = _read(tmp_path / DAY / RACE / "post2.json.gz")["final_check"]
    assert check["verified"] is False
    assert {m["reason"] for m in check["markets"].values()} == {"source_before_close"}


def test_changed_odds_between_post1_and_post2_is_not_final(tmp_path):
    clock = Clock(_t(16, 10))
    _run(tmp_path, clock, _fetcher(clock, lag_min=3), [_entry("15:40")])
    _run(tmp_path, clock, _fetcher(clock, lag_min=3, bump=0.5), [_entry("15:40")])
    check = _read(tmp_path / DAY / RACE / "post2.json.gz")["final_check"]
    assert check["verified"] is False
    assert check["markets"]["win"]["reason"] == "changed_between_observations"
    assert check["markets"]["wide"]["verified"] is True  # 変わらなかった券種は確かめられる


def test_late_start_takes_post1_then_post2_and_post2_alone_is_unverified(tmp_path):
    clock = Clock(_t(16, 45))  # post2 の時刻も過ぎてから始めると、post1 を先に取ってから post2 を取る
    _run(tmp_path, clock, _fetcher(clock, lag_min=3), [_entry("15:40")])
    day = tmp_path / DAY / RACE
    assert _read(day / "post1.json.gz")["status"] == "ok"
    assert _read(day / "post2.json.gz")["final_check"]["verified"] is True
    check = col.final_check(None, _read(day / "post2.json.gz"), _t(15, 40))
    assert check == {"verified": False, "reason": "single_observation", "markets": {}}


def test_existing_records_are_not_fetched_again(tmp_path):
    clock = Clock(_t(17, 0))
    _run(tmp_path, clock, _fetcher(clock, lag_min=3), [_entry("15:40")])
    calls: list = []
    rc = _run(tmp_path, clock, _fetcher(clock, calls=calls), [])
    assert rc == col.EXIT_DONE and calls == []


def test_deadline_before_next_event_returns_timeout_without_waiting(tmp_path):
    clock = Clock(_t(9, 0))
    rc = col.run_once("2026-11-01", tmp_path, _t(12, 0), clock=clock, sleep=clock.sleep,
                      fetch_body=_fetcher(clock), fetch_list=lambda d: [_entry("15:40")])
    assert rc == col.EXIT_TIMEOUT and clock.slept == []


def test_close_has_priority_and_other_captures_wait_for_it(tmp_path):
    # A は 15:00 発走（post1 は 15:25）、B は 15:30 発走（close は 15:27）。15:26 に起きたら B の close を先に取る
    entries = [_entry("15:00", race_no=10, ref="202605050210"), _entry("15:30", race_no=11, ref="202605050211")]
    clock = Clock(_t(15, 26))
    calls: list = []
    fetch = _fetcher(clock, calls=calls)
    assert _run(tmp_path, clock, fetch, entries) == col.EXIT_MORE  # 1回目：過ぎた close だけ取り逃しとして残す
    assert calls == [] and clock.slept == []
    assert _run(tmp_path, clock, fetch, entries) == col.EXIT_MORE  # 2回目：15:27 まで待って B の close を先に
    assert clock.slept == [60.0]
    assert calls[0][0] == "202605050211" and calls[4][0] == "202605050210"
    assert _read(tmp_path / DAY / "20261101-tokyo-11" / "close.json.gz")["status"] == "ok"
    assert _read(tmp_path / DAY / "20261101-tokyo-10" / "post1.json.gz")["status"] == "ok"


def test_race_list_is_refreshed_hourly_and_merged_with_the_latest_post_time(tmp_path):
    clock = Clock(_t(9, 0))
    first = _full_day()
    later = [e if e.race_no != 11 else _entry("15:45") for e in first]  # 11R の発走時刻が変わった
    later.append(_entry("16:30", race_no=12, venue="京都", ref="202608050212"))
    col.load_races(tmp_path, "2026-11-01", clock(), lambda d: first)
    rows = col.load_races(tmp_path, "2026-11-01", _t(9, 30), lambda d: [])  # 1時間たっていないので取り直さない
    assert len(list((tmp_path / DAY / "races").glob("*.json"))) == 1 and len(rows) == 12
    rows = col.load_races(tmp_path, "2026-11-01", _t(10, 1), lambda d: later)
    assert len(list((tmp_path / DAY / "races").glob("*.json"))) == 2
    by_key = {r["race_key"]: r for r in rows}
    assert by_key[RACE]["post_time"] == "15:45" and "20261101-kyoto-12" in by_key
    versions = [json.loads(p.read_text()) for p in sorted((tmp_path / DAY / "races").glob("*.json"))]
    assert versions[0]["check"]["status"] == "ok"
    assert versions[1]["check"]["status"] == "partial_unverified"  # 京都は 12R しか無い


def test_unavailable_race_list_is_not_saved_so_the_next_run_retries(tmp_path):
    def no_tab(date_str):
        raise RuntimeError("指定日のタブが日付タブ一覧に見つかりませんでした")
    clock = Clock(_t(9, 0))
    for fetch_list in (no_tab, lambda d: []):
        rc = col.run_once("2026-11-03", tmp_path, DEADLINE, clock=clock, sleep=clock.sleep,
                          fetch_body=_fetcher(clock), fetch_list=fetch_list)
        assert rc == col.EXIT_ERROR
        assert not (tmp_path / "20261103" / "races").exists()


def test_records_are_byte_identical_for_identical_content(tmp_path):
    rec = {"b": 1, "a": [1, 2]}
    col.write_record(tmp_path / "x.json.gz", rec)
    col.write_record(tmp_path / "y.json.gz", rec)
    assert (tmp_path / "x.json.gz").read_bytes() == (tmp_path / "y.json.gz").read_bytes()
    col.write_record(tmp_path / "x.json.gz", {"changed": True})  # 上書きしない
    assert _read(tmp_path / "x.json.gz") == rec
