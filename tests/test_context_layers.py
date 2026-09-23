from __future__ import annotations

import datetime
import json
from pathlib import Path

from logic import context_layers, odds_history, paddock

JST = odds_history.JST
CONFIG = context_layers.load_config()


def _entry(current=482, weights=(480, 482, 478, 480), last_date="2026-09-10"):
    return {
        "num": 4,
        "name": "テストホース",
        "body_weight": {"value": current, "diff": current - weights[0]},
        "past_runs": [
            {"date": last_date, "body_weight": {"value": w, "diff": 0}}
            for w in weights
        ],
    }


def test_body_weight_profile_uses_each_horses_own_baseline():
    stable = context_layers.body_weight_profile(_entry(current=482), CONFIG["body_weight"])
    unusual = context_layers.body_weight_profile(_entry(current=500), CONFIG["body_weight"])

    assert stable["median"] == 480.0
    assert stable["unusual"] is False
    assert unusual["deviation_from_median"] == 20.0
    assert unusual["deviation_pct"] > 0.04
    assert unusual["unusual"] is True


def test_body_weight_does_not_flag_with_insufficient_history():
    entry = _entry(current=510, weights=(480, 482))
    profile = context_layers.body_weight_profile(entry, CONFIG["body_weight"])
    assert profile["quality"] == "insufficient"
    assert profile["unusual"] is False


def test_rest_profile_separates_quick_normal_and_layoff():
    cfg = CONFIG["rest"]
    quick = context_layers.rest_profile(_entry(last_date="2026-09-20"), "2026-09-26", cfg)
    normal = context_layers.rest_profile(_entry(last_date="2026-08-30"), "2026-09-26", cfg)
    layoff = context_layers.rest_profile(_entry(last_date="2026-06-01"), "2026-09-26", cfg)
    assert quick["bucket"] == "quick_return"
    assert normal["bucket"] == "normal"
    assert layoff["bucket"] == "layoff"


def test_odds_movement_is_compressed_to_first_last_support_shift(tmp_path: Path):
    race = {
        "id": "20260926-nakayama-11",
        "post_time": "15:45",
        "source_refs": {"netkeiba": "x"},
        "odds_updated_at": "2026-09-26 13:00:00",
        "entries": [{"num": 4, "win_odds": 10.0, "popularity": 6}],
    }
    assert odds_history.append_observation(
        race, "pipeline", datetime.datetime(2026,9,26,13,0,tzinfo=JST), tmp_path
    ) == odds_history.ADDED
    race["odds_updated_at"] = "2026-09-26 15:00:00"
    race["entries"][0]["win_odds"] = 5.0
    assert odds_history.append_observation(
        race, "late", datetime.datetime(2026,9,26,15,0,tzinfo=JST), tmp_path
    ) == odds_history.ADDED

    summary = context_layers.odds_movement_summary(race["id"], tmp_path)
    row = summary["horses"][0]
    assert summary["n_observations"] == 2
    assert row["first_odds"] == 10.0 and row["last_odds"] == 5.0
    assert row["odds_ratio"] == 0.5
    assert row["support_shift"] > 0


def test_paddock_accepts_only_recent_pre_race_observations(tmp_path: Path):
    race = {"id":"20260926-nakayama-11", "post_time":"15:45"}
    payload = {
        "race_id": race["id"],
        "observed_at":"2026-09-26T15:15:00+09:00",
        "source":"manual",
        "horses":[{"num":4,"gait_symmetry":.8,"sweat":.2,"confidence":.9}],
    }
    path = tmp_path / f"{race['id']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    loaded = paddock.load_verified(race, CONFIG["paddock"], tmp_path)
    assert loaded is not None and loaded["verified_pre_race"] is True

    payload["observed_at"] = "2026-09-26T15:46:00+09:00"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert paddock.load_verified(race, CONFIG["paddock"], tmp_path) is None


def test_context_layers_are_observe_only_and_do_not_emit_prediction_adjustment(tmp_path: Path):
    race = {
        "id":"20260926-nakayama-11", "date":"2026-09-26", "post_time":"15:45",
        "entries":[_entry()],
        "track_metrics":{"venue":"中山","moisture":{"芝":{"goal":12.3,"turn4":13.1}}},
    }
    built = context_layers.build_context(
        race, CONFIG, odds_directory=tmp_path/"odds", paddock_directory=tmp_path/"paddock"
    )
    assert built["mode"] == "observe_only"
    assert built["layer1"]["status"] == "active"
    assert built["layer2"]["status"] == "observe_only"
    assert built["layer3"]["status"] == "unobserved"
    assert "score_adjustment" not in built
