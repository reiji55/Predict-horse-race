from __future__ import annotations

import datetime
from pathlib import Path

from logic import odds_history
from scraper import capture_late_odds

JST = odds_history.JST


def _race(race_id="20260926-nakayama-11", post_time="15:45"):
    return {
        "id": race_id,
        "post_time": post_time,
        "source_refs": {"netkeiba": "202606040911"},
        "odds_updated_at": "2026-09-26 13:00:01",
        "entries": [
            {"num": 1, "win_odds": 2.8, "popularity": 1},
            {"num": 2, "win_odds": 4.5, "popularity": 2},
            {"num": 3, "win_odds": 8.0, "popularity": 3},
        ],
    }


def _at(hour, minute, day=26):
    return datetime.datetime(2026, 9, day, hour, minute, tzinfo=JST)


def test_append_observation_keeps_time_series_and_deduplicates_same_market_state(tmp_path: Path):
    race = _race()
    t1 = _at(13, 0)

    assert odds_history.append_observation(race, "pipeline", t1, tmp_path) == odds_history.ADDED
    # phase/observed_at が違っても、API時刻とオッズが同じなら新情報ではない。
    assert odds_history.append_observation(
        race, "late", t1 + datetime.timedelta(minutes=1), tmp_path
    ) == odds_history.DUPLICATE

    race["odds_updated_at"] = "2026-09-26 15:10:02"
    race["entries"][0]["win_odds"] = 2.5
    assert odds_history.append_observation(
        race, "late", _at(15, 10), tmp_path
    ) == odds_history.ADDED

    rows = odds_history.load_observations(race["id"], tmp_path)
    assert [r["phase"] for r in rows] == ["pipeline", "late"]
    assert rows[1]["odds"][0]["win_odds"] == 2.5
    assert rows[1]["source_time"] == "2026-09-26 15:10:02"
    # 1観測=1ファイル。別workflowが同時に書いても同じファイルを奪い合わない。
    assert len(list((tmp_path / race["id"]).glob("*.json"))) == 2


def test_observation_at_or_after_post_time_is_never_saved(tmp_path: Path):
    race = _race(post_time="15:45")

    assert odds_history.append_observation(race, "pipeline", _at(15, 45), tmp_path) \
        == odds_history.AFTER_POST
    assert odds_history.append_observation(race, "pipeline", _at(17, 41), tmp_path) \
        == odds_history.AFTER_POST
    assert not (tmp_path / race["id"]).exists()


def test_source_time_after_post_is_treated_as_final_odds(tmp_path: Path):
    """取得開始が発走前でも、APIの時刻が発走後なら確定オッズなので保存しない。"""
    race = _race(post_time="15:45")
    race["odds_updated_at"] = "2026-09-26 15:46:10"

    assert odds_history.append_observation(race, "late", _at(15, 44), tmp_path) \
        == odds_history.AFTER_POST


def test_unknown_post_time_fails_closed(tmp_path: Path):
    race = _race()
    race["post_time"] = None

    assert odds_history.append_observation(race, "pipeline", _at(10, 0), tmp_path) \
        == odds_history.UNKNOWN_POST_TIME


def test_pipeline_history_uses_only_races_built_in_this_run(tmp_path: Path):
    today = _race()
    old = _race("20260925-nakayama-11")
    raw = {
        "fetched_at": "2026-09-26T14:13:00+09:00",
        "collection_report": {"built": [{"race_id": today["id"]}]},
        "races": [old, today],
    }

    report = odds_history.append_current_run(raw, directory=tmp_path, now=_at(14, 14))

    assert report["added"] == 1 and report["skipped"] == 0
    rows = odds_history.load_observations(today["id"], tmp_path)
    # observed_at はスクリプト実行時刻ではなく raw を取り終えた時刻
    assert rows[0]["observed_at"] == "2026-09-26T14:13:00+09:00"
    assert not (tmp_path / old["id"]).exists()


def test_pipeline_after_post_does_not_save_final_odds_as_pre_race(tmp_path: Path):
    """Actions遅延で13時枠が17時台に動いた場合、そのオッズは発走後なので保存しない。"""
    today = _race(post_time="15:45")
    raw = {
        "fetched_at": "2026-09-26T17:41:00+09:00",
        "collection_report": {"built": [{"race_id": today["id"]}]},
        "races": [today],
    }

    report = odds_history.append_current_run(raw, directory=tmp_path, now=_at(17, 42))

    assert report["added"] == 0
    assert report["reasons"] == {odds_history.AFTER_POST: 1}
    assert not (tmp_path / today["id"]).exists()


def test_stale_or_unreported_raw_is_not_relabelled_as_new_observation(tmp_path: Path):
    today = _race()
    stale = {
        "fetched_at": "2026-09-26T10:05:00+09:00",
        "collection_report": {"built": [{"race_id": today["id"]}]},
        "races": [today],
    }
    unreported = {"fetched_at": "2026-09-26T14:13:00+09:00", "races": [today]}

    assert odds_history.append_current_run(stale, directory=tmp_path, now=_at(14, 14))["added"] == 0
    assert odds_history.append_current_run(unreported, directory=tmp_path, now=_at(14, 14))["added"] == 0
    assert list(tmp_path.iterdir()) == []


def test_pipeline_history_with_empty_built_set_does_not_relabel_old_races(tmp_path: Path):
    old = _race("20260925-nakayama-11")
    raw = {
        "fetched_at": "2026-09-26T14:13:00+09:00",
        "collection_report": {"built": [], "failed": [{"stage": "race"}]},
        "races": [old],
    }

    report = odds_history.append_current_run(raw, directory=tmp_path, now=_at(14, 14))

    assert report["added"] == 0 and report["skipped"] == 0
    assert list(tmp_path.iterdir()) == []


def _fake_fetch(called, official="2026-09-26 15:10:03"):
    def fetch(source_ref):
        called.append(source_ref)
        return {
            "official_datetime": official,
            "by_num": {
                1: {"win_odds": 2.6, "popularity": 1},
                2: {"win_odds": 5.0, "popularity": 2},
            },
        }
    return fetch


def test_late_capture_skips_started_races_and_fetches_only_pre_race(monkeypatch, tmp_path: Path):
    pre = _race("20260926-nakayama-11", "15:45")
    started = _race("20260926-hanshin-11", "15:00")
    raw = {"races": [pre, started]}
    called = []

    monkeypatch.setattr(capture_late_odds.b2_odds, "fetch_win_odds", _fake_fetch(called))

    report = capture_late_odds.capture(raw, "2026-09-26", now=_at(15, 10), directory=tmp_path)

    assert len(called) == 1
    assert [x["race_id"] for x in report["captured"]] == ["20260926-nakayama-11"]
    assert report["captured"][0]["minutes_to_post"] == 35.0
    assert report["captured"][0]["added"] is True
    assert report["skipped"] == [{"race_id": "20260926-hanshin-11", "reason": "already_posted"}]
    assert (tmp_path / "20260926-nakayama-11").is_dir()
    assert not (tmp_path / "20260926-hanshin-11").exists()


def test_late_capture_rechecks_post_time_after_slow_fetch(monkeypatch, tmp_path: Path):
    """取得前は発走前でも、取得完了時に発走を跨いだら保存しない。"""
    raw = {"races": [_race("20260926-nakayama-11", "15:45")]}
    ticks = iter([_at(15, 44), _at(15, 46)])
    called = []
    monkeypatch.setattr(capture_late_odds.b2_odds, "fetch_win_odds", _fake_fetch(called))

    report = capture_late_odds.capture(
        raw, "2026-09-26", directory=tmp_path, clock=lambda: next(ticks)
    )

    assert len(called) == 1
    assert report["captured"] == []
    assert report["skipped"] == [
        {"race_id": "20260926-nakayama-11", "reason": "posted_during_fetch"}
    ]
    assert not (tmp_path / "20260926-nakayama-11").exists()


def test_late_capture_counts_empty_odds_as_failure(monkeypatch, tmp_path: Path):
    raw = {"races": [_race("20260926-nakayama-11", "15:45")]}
    monkeypatch.setattr(
        capture_late_odds.b2_odds, "fetch_win_odds",
        lambda ref: {"official_datetime": None, "by_num": {1: {"win_odds": None}}},
    )

    report = capture_late_odds.capture(raw, "2026-09-26", now=_at(15, 10), directory=tmp_path)

    assert report["captured"] == []
    assert report["failed"] == [{"race_id": "20260926-nakayama-11", "reason": "no_win_odds"}]


def test_late_capture_without_raw_is_a_quiet_noop(monkeypatch, tmp_path: Path):
    """Actions遅延で当日pipelineより先に動いた場合、赤にせず何もしない。"""
    monkeypatch.setattr(capture_late_odds, "RAW_DIR", tmp_path)
    monkeypatch.setattr("sys.argv", ["capture_late_odds", "--week", "2026-W39",
                                     "--date", "2026-09-26"])

    capture_late_odds.main()     # SystemExit しない
