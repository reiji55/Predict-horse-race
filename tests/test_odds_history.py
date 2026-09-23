from __future__ import annotations

import datetime
import json
from pathlib import Path

from logic import odds_history
from scraper import capture_late_odds


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


def test_append_observation_keeps_time_series_and_deduplicates_same_market_state(tmp_path: Path):
    race = _race()
    t1 = datetime.datetime(2026, 9, 26, 13, 0, tzinfo=odds_history.JST)

    assert odds_history.append_observation(race, "pipeline", t1, tmp_path) is True
    # phase/observed_at が違っても、API時刻とオッズが同じなら新情報ではない。
    assert odds_history.append_observation(race, "late", t1 + datetime.timedelta(minutes=1), tmp_path) is False

    race["odds_updated_at"] = "2026-09-26 15:10:02"
    race["entries"][0]["win_odds"] = 2.5
    assert odds_history.append_observation(
        race, "late", t1 + datetime.timedelta(hours=2, minutes=10), tmp_path
    ) is True

    payload = json.loads((tmp_path / f"{race['id']}.json").read_text(encoding="utf-8"))
    assert len(payload["observations"]) == 2
    assert payload["observations"][0]["phase"] == "pipeline"
    assert payload["observations"][1]["phase"] == "late"
    assert payload["observations"][1]["odds"][0]["win_odds"] == 2.5


def test_pipeline_history_uses_only_races_built_in_this_run(tmp_path: Path):
    today = _race()
    old = _race("20260925-nakayama-11")
    raw = {
        "collection_report": {"built": [{"race_id": today["id"]}]},
        "races": [old, today],
    }

    report = odds_history.append_current_run(raw, directory=tmp_path)

    assert report == {"added": 1, "skipped": 0}
    assert (tmp_path / f"{today['id']}.json").exists()
    assert not (tmp_path / f"{old['id']}.json").exists()


def test_late_capture_skips_started_races_and_fetches_only_pre_race(monkeypatch, tmp_path: Path):
    pre = _race("20260926-nakayama-11", "15:45")
    started = _race("20260926-hanshin-11", "15:00")
    raw = {"races": [pre, started]}
    called = []

    def fake_fetch(source_ref):
        called.append(source_ref)
        return {
            "official_datetime": "2026-09-26 15:10:03",
            "by_num": {
                1: {"win_odds": 2.6, "popularity": 1},
                2: {"win_odds": 5.0, "popularity": 2},
            },
        }

    monkeypatch.setattr(capture_late_odds.b2_odds, "fetch_win_odds", fake_fetch)
    now = datetime.datetime(2026, 9, 26, 15, 10, tzinfo=capture_late_odds.JST)

    report = capture_late_odds.capture(raw, "2026-09-26", now=now, directory=tmp_path)

    assert len(called) == 1
    assert [x["race_id"] for x in report["captured"]] == ["20260926-nakayama-11"]
    assert report["captured"][0]["minutes_to_post"] == 35.0
    assert report["skipped"] == [{"race_id": "20260926-hanshin-11", "reason": "already_posted"}]
    assert (tmp_path / "20260926-nakayama-11.json").exists()
    assert not (tmp_path / "20260926-hanshin-11.json").exists()


def test_pipeline_history_with_empty_built_set_does_not_relabel_old_races(tmp_path: Path):
    old = _race("20260925-nakayama-11")
    raw = {
        "collection_report": {"built": [], "failed": [{"stage": "race"}]},
        "races": [old],
    }

    report = odds_history.append_current_run(raw, directory=tmp_path)

    assert report == {"added": 0, "skipped": 0}
    assert list(tmp_path.iterdir()) == []
