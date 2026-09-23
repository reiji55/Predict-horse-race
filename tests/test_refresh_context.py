from __future__ import annotations

import datetime
import json
from pathlib import Path

from logic import condition_history, refresh_context

JST = refresh_context.JST


def _race():
    return {
        "id":"20260926-nakayama-11",
        "date":"2026-09-26",
        "post_time":"15:45",
        "source_refs":{"netkeiba":"x"},
        "entries":[{
            "num":4,"name":"H4","body_weight":None,
            "past_runs":[
                {"date":"2026-09-10","body_weight":{"value":480,"diff":0}},
                {"date":"2026-08-20","body_weight":{"value":482,"diff":2}},
                {"date":"2026-08-01","body_weight":{"value":478,"diff":-4}},
            ],
        }],
    }


def test_context_snapshot_uses_latest_measured_weight_without_touching_prediction_snapshot(tmp_path: Path):
    raw = {"races":[_race()]}
    cond_dir = tmp_path / "conditions"
    context_dir = tmp_path / "context"
    prediction_dir = tmp_path / "predictions"
    prediction_dir.mkdir()
    prediction_path = prediction_dir / "20260926-nakayama-11.json"
    prediction_path.write_text('{"cards":[{"char":"kei"}],"frozen_at":"13:00"}', encoding="utf-8")
    original = prediction_path.read_text(encoding="utf-8")

    observed = _race()
    observed["entries"][0]["body_weight"] = {"value":500,"diff":20}
    status = condition_history.append_body_weight_observation(
        observed, "late",
        datetime.datetime(2026,9,26,15,0,tzinfo=JST),
        cond_dir,
    )
    assert status == condition_history.ADDED

    report = refresh_context.capture(
        raw,
        now=datetime.datetime(2026,9,26,15,10,tzinfo=JST),
        output_directory=context_dir,
        condition_directory=cond_dir,
        odds_directory=tmp_path/"odds",
        paddock_directory=tmp_path/"paddock",
    )
    assert report["added"] == ["20260926-nakayama-11"]

    latest = refresh_context.latest_context_snapshot("20260926-nakayama-11", context_dir)
    profile = latest["context_layers"]["layer2"]["horses"][0]["body_weight"]
    assert profile["current"] == 500
    assert profile["median"] == 480
    assert profile["unusual"] is True

    # context収集は既存の予想snapshotを一切触らない。
    assert prediction_path.read_text(encoding="utf-8") == original


def test_context_snapshot_never_writes_after_post(tmp_path: Path):
    raw = {"races":[_race()]}
    report = refresh_context.capture(
        raw,
        now=datetime.datetime(2026,9,26,15,46,tzinfo=JST),
        output_directory=tmp_path/"context",
    )
    assert report["added"] == []
    assert report["skipped"][0]["reason"] == "already_posted"
