from __future__ import annotations

import json
from pathlib import Path

from results import anomaly_review


def _marks():
    return [
        {"num":1,"name":"人気1","score":90,"odds":2.0},
        {"num":2,"name":"人気2","score":85,"odds":3.5},
        {"num":3,"name":"人気3","score":80,"odds":5.0},
        {"num":4,"name":"中位","score":75,"odds":10.0},
        {"num":5,"name":"中位2","score":70,"odds":15.0},
        {"num":6,"name":"ダークホース","score":65,"odds":30.0},
        {"num":7,"name":"穴","score":60,"odds":50.0},
    ]


def _build(results, tmp_path: Path, context_dir: Path | None = None):
    return anomaly_review.build_report(
        results,
        context_snapshot_directory=context_dir or (tmp_path / "context"),
        audit_manifest_directory=tmp_path / "audit",
        prediction_snapshot_directory=tmp_path / "pred",
        odds_history_directory=tmp_path / "odds",
    )


def test_surprising_top3_is_queued_with_pre_race_context(tmp_path: Path):
    race_id = "20260926-nakayama-11"
    context_dir = tmp_path / "context"
    rd = context_dir / race_id
    rd.mkdir(parents=True)
    (rd / "20260926T151000.json").write_text(json.dumps({
        "race_id": race_id,
        "post_time": "15:45",
        "observed_at": "2026-09-26T15:10:00+09:00",
        "pre_race": True,
        "context_layers": {
            "version":"context-layers-v1",
            "layer2":{
                "track_metrics":{"cushion":{"value":9.3}},
                "odds_movement":{"horses":[
                    {"num":6,"first_odds":45.0,"last_odds":30.0,"support_shift":0.405}
                ]},
                "horses":[{
                    "num":6,
                    "body_weight":{"current":500,"median":480,"unusual":True},
                    "rest":{"days":70,"bucket":"layoff"},
                }],
            },
            "layer3":{"paddock":{"horses":[
                {"num":6,"gait_symmetry":.9,"confidence":.8}
            ]}},
        },
    }, ensure_ascii=False), encoding="utf-8")

    results = {"results":[{
        "race_id":race_id,
        "finish":[6,1,2],
        "meta":{"venue":"中山","race_no":11,"name":"テストS","marks":_marks()},
        "race_regime":{"label":"open"},
        "cards":[],
    }]}

    report = _build(results, tmp_path, context_dir)
    assert report["summary"]["anomalies"] == 1
    row = report["anomalies"][0]
    assert row["num"] == 6 and row["finish"] == 1
    assert set(row["reasons"]) == {"model_miss","market_miss"}
    assert row["context"]["body_weight"]["unusual"] is True
    assert row["context"]["rest"]["bucket"] == "layoff"
    assert row["context"]["odds_movement"]["last_odds"] == 30.0
    assert row["context"]["paddock"]["gait_symmetry"] == .9
    assert row["interpretation"] == "hypothesis_only"


def test_popular_expected_finish_is_not_an_anomaly(tmp_path: Path):
    results = {"results":[{
        "race_id":"r1",
        "finish":[1,2,3],
        "meta":{"marks":_marks()},
        "cards":[],
    }]}
    report = _build(results, tmp_path, tmp_path)
    assert report["summary"]["anomalies"] == 0


def test_one_of_model_or_market_miss_is_enough(tmp_path: Path):
    marks = _marks()
    # 6番はモデル6位だがオッズだけ3番人気へ変更
    for mark in marks:
        if mark["num"] == 6:
            mark["odds"] = 4.0
    results = {"results":[{
        "race_id":"r1","finish":[6,1,2],"meta":{"marks":marks},"cards":[]
    }]}
    row = _build(results, tmp_path, tmp_path)["anomalies"][0]
    assert row["reasons"] == ["model_miss"]


def test_context_snapshot_after_post_is_never_joined(tmp_path: Path):
    """手置き・時計ずれで発走後の所見が紛れ込んでも、研究キューへ混ぜない。"""
    race_id = "20260926-nakayama-11"
    rd = tmp_path / race_id
    rd.mkdir(parents=True)
    for stamp, observed, pre in (
        ("20260926T151000", "2026-09-26T15:10:00+09:00", True),
        ("20260926T160000", "2026-09-26T16:00:00+09:00", True),   # 発走後
        ("20260926T152000", "2026-09-26T15:20:00+09:00", False),  # pre_race でない
    ):
        (rd / f"{stamp}.json").write_text(json.dumps({
            "race_id": race_id, "post_time": "15:45", "observed_at": observed,
            "pre_race": pre,
            "context_layers": {"version": stamp, "layer2": {"horses": []}},
        }), encoding="utf-8")

    results = {"results": [{
        "race_id": race_id, "finish": [6, 1, 2],
        "meta": {"marks": _marks()}, "cards": [],
    }]}
    row = _build(results, tmp_path, tmp_path)["anomalies"][0]
    assert row["context_observed_at"] == "2026-09-26T15:10:00+09:00"
    assert row["context"]["context_version"] == "20260926T151000"


def test_race_audit_reports_manifest_freshness_and_context_completeness(tmp_path: Path):
    race_id = "20260926-hanshin-11"
    context_dir = tmp_path / "context"
    rd = context_dir / race_id
    rd.mkdir(parents=True)
    (rd / "20260926T154000_late.json").write_text(json.dumps({
        "race_id": race_id,
        "post_time": "15:45",
        "observed_at": "2026-09-26T15:40:00+09:00",
        "pre_race": True,
        "context_layers": {
            "version": "context-layers-v1",
            "layer2": {
                "track_metrics": {"moisture": {"ダート": {"goal": 6.3}}},
                "odds_movement": {"horses": [{"num": 6}, {"num": 9}, {"num": 11}]},
                "horses": [
                    {"num": 6, "body_weight": {"current": 516}, "rest": {"days": 20}},
                    {"num": 9, "body_weight": {"current": 542}, "rest": {"days": 161}},
                    {"num": 11, "body_weight": {"current": 506}, "rest": {"days": 49}},
                ],
            },
            "layer3": {"paddock": None},
        },
    }), encoding="utf-8")

    audit_dir = tmp_path / "audit" / race_id
    audit_dir.mkdir(parents=True)
    (audit_dir / "20260926T154200_late.json").write_text(json.dumps({
        "version": "context-audit-manifest-v1",
        "race_id": race_id,
        "post_time": "15:45",
        "observed_at": "2026-09-26T15:42:00+09:00",
        "pre_race": True,
        "proof_scope": "internal_hash_manifest",
        "artifacts": {
            "prediction": {"sha256": "a"},
            "context": {"sha256": "b"},
            "odds": {"sha256": "c", "source_time": "2026-09-26 15:40:00"},
        },
        "odds_freshness": {
            "status": "target_window", "fresh": True,
            "target_window": True, "minutes_to_post": 5.0,
        },
        "context_completeness": {
            "prediction_snapshot_present": True,
            "context_snapshot_present": True,
            "odds_snapshot_present": True,
            "body_weight_current_coverage": 1.0,
        },
    }), encoding="utf-8")

    results = {"results": [{
        "race_id": race_id,
        "finish": [11, 9, 12],
        "meta": {
            "venue": "阪神", "race_no": 11, "name": "シリウスS",
            "post_time": "15:45", "marks": _marks(),
        },
        "cards": [],
    }]}
    report = _build(results, tmp_path, context_dir)
    audit = report["race_audits"][0]

    assert audit["evidence"]["status"] == "manifest"
    assert audit["evidence"]["odds_freshness"]["target_window"] is True
    assert audit["evidence"]["context_completeness"]["body_weight_current_coverage"] == 1.0
    assert report["summary"]["audit"]["with_manifest"] == 1
    assert report["summary"]["audit"]["odds_fresh"] == 1
    assert report["summary"]["audit"]["body_weight_full_coverage"] == 1
    assert len(audit["finishers"]) == 3
    assert audit["finishers"][0]["num"] == 11
