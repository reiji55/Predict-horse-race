from __future__ import annotations

import datetime
import json
from pathlib import Path

from logic import audit_manifest

JST = datetime.timezone(datetime.timedelta(hours=9))


def _at(h, m):
    return datetime.datetime(2026, 9, 26, h, m, tzinfo=JST)


def _race():
    return {
        "id": "20260926-hanshin-11",
        "post_time": "15:45",
        "entries": [{"num": 1}, {"num": 2}],
    }


def test_manifest_binds_prediction_context_and_odds_with_hashes(tmp_path: Path):
    pred_dir = tmp_path / "pred"
    context_dir = tmp_path / "context"
    odds_dir = tmp_path / "odds"
    out_dir = tmp_path / "manifests"
    pred_dir.mkdir()
    (context_dir / _race()["id"]).mkdir(parents=True)
    (odds_dir / _race()["id"]).mkdir(parents=True)

    (pred_dir / f"{_race()['id']}.json").write_text(json.dumps({
        "race_id": _race()["id"],
        "post_time": "15:45",
        "pre_race": True,
        "frozen_at": "2026-09-26T14:40:32+09:00",
        "cards": [],
    }), encoding="utf-8")

    (context_dir / _race()["id"] / "20260926T154000_late.json").write_text(
        json.dumps({
            "race_id": _race()["id"],
            "post_time": "15:45",
            "observed_at": "2026-09-26T15:40:00+09:00",
            "pre_race": True,
            "context_layers": {
                "layer2": {
                    "track_metrics": {"moisture": {"ダート": {"goal": 6.3}}},
                    "odds_movement": {"horses": [{"num": 1}, {"num": 2}]},
                    "horses": [
                        {"num": 1, "body_weight": {"current": 500}},
                        {"num": 2, "body_weight": {"current": 480}},
                    ],
                },
                "layer3": {"paddock": None},
            },
        }), encoding="utf-8",
    )

    (odds_dir / _race()["id"] / "20260926T154100_late.json").write_text(
        json.dumps({
            "race_id": _race()["id"],
            "post_time": "15:45",
            "observed_at": "2026-09-26T15:41:00+09:00",
            "source_time": "2026-09-26 15:40:00",
            "odds": [{"num": 1, "win_odds": 4.0}, {"num": 2, "win_odds": 8.0}],
        }), encoding="utf-8",
    )

    report = audit_manifest.capture(
        {"races": [_race()]},
        now=_at(15, 42),
        phase="late",
        output_directory=out_dir,
        prediction_directory=pred_dir,
        context_directory=context_dir,
        odds_directory=odds_dir,
        config={"audit": {
            "odds_target_min_minutes": 5,
            "odds_target_max_minutes": 15,
            "odds_stale_after_minutes": 30,
        }},
    )

    assert report["added"] == [_race()["id"]]
    manifest = audit_manifest.latest_manifest(_race()["id"], out_dir)
    assert manifest is not None
    assert manifest["artifacts"]["prediction"]["sha256"]
    assert manifest["artifacts"]["context"]["sha256"]
    assert manifest["artifacts"]["odds"]["sha256"]
    assert manifest["odds_freshness"]["status"] == "target_window"
    assert manifest["odds_freshness"]["minutes_to_post"] == 5.0
    assert manifest["context_completeness"]["body_weight_current_coverage"] == 1.0
    assert manifest["context_completeness"]["track_metrics_present"] is True
    assert manifest["proof_scope"] == "internal_hash_manifest"


def test_odds_freshness_distinguishes_target_very_late_and_stale():
    cfg = {"audit": {
        "odds_target_min_minutes": 5,
        "odds_target_max_minutes": 15,
        "odds_stale_after_minutes": 30,
    }}
    assert audit_manifest.classify_odds_freshness(10, cfg)["status"] == "target_window"
    assert audit_manifest.classify_odds_freshness(3, cfg)["status"] == "very_late"
    assert audit_manifest.classify_odds_freshness(20, cfg)["status"] == "acceptable"
    stale = audit_manifest.classify_odds_freshness(45, cfg)
    assert stale["status"] == "stale" and stale["fresh"] is False


def test_post_race_manifest_is_never_created(tmp_path: Path):
    report = audit_manifest.capture(
        {"races": [_race()]},
        now=_at(15, 46),
        output_directory=tmp_path,
    )
    assert report["added"] == []
    assert report["skipped"] == [{"race_id": _race()["id"], "reason": "not_pre_race"}]
