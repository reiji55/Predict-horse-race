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


def test_surprising_top3_is_queued_with_pre_race_context(tmp_path: Path):
    race_id = "20260926-nakayama-11"
    context_dir = tmp_path / "context"
    rd = context_dir / race_id
    rd.mkdir(parents=True)
    (rd / "20260926T151000.json").write_text(json.dumps({
        "race_id": race_id,
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

    report = anomaly_review.build_report(results, context_snapshot_directory=context_dir)
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
    report = anomaly_review.build_report(results, context_snapshot_directory=tmp_path)
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
    row = anomaly_review.build_report(results, context_snapshot_directory=tmp_path)["anomalies"][0]
    assert row["reasons"] == ["model_miss"]
