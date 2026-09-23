from __future__ import annotations

from logic import data_quality


def _entry(num: int) -> dict:
    return {
        "num": num,
        "win_odds": 3.0 + num,
        "past_runs": [{"finish": 2}],
        "jockey_stats": {"starts": 100},
        "trainer_stats": {"starts": 80},
    }


def _raw() -> dict:
    return {
        "week_id": "2026-W39",
        "collection_report": {
            "requested_dates": ["2026-09-26"],
            "selected": [{"date": "2026-09-26", "venue": "中山", "race_no": 11, "source_ref": "R1"}],
            "built": [{"date": "2026-09-26", "venue": "中山", "race_no": 11,
                       "source_ref": "R1", "race_id": "20260926-nakayama-11"}],
            "failed": [],
        },
        "races": [{
            "id": "20260926-nakayama-11",
            "venue": "中山",
            "race_no": 11,
            "name": "テストS",
            "entries": [_entry(i) for i in range(1, 6)],
            "combo_odds": {
                "ワイド": {"1-2": [3.0, 4.0]},
                "馬連": {"1-2": 8.0},
                "3連複": {"1-2-3": 20.0},
            },
        }],
    }


def _predictions() -> dict:
    return {
        "week_id": "2026-W39",
        "races": [{
            "id": "20260926-nakayama-11",
            "model_id": "win-v1-speed-guard",
            "marks": [{"num": i} for i in range(1, 6)],
            "cards": [{"char": "kei"}],
            "speed_quality": {"used": True, "coverage": 1.0},
        }],
    }


def test_healthy_input_is_ok():
    report = data_quality.build_report(_raw(), _predictions())

    assert report["status"] == "ok"
    assert report["summary"]["raw_races"] == 1
    assert report["summary"]["prediction_races"] == 1
    assert report["summary"]["entry_coverage"]["past_runs"] == 1.0
    assert data_quality.should_fail(report, "critical") is False


def test_missing_prediction_is_critical():
    report = data_quality.build_report(_raw(), {"races": []})

    assert report["status"] == "critical"
    codes = {issue["code"] for issue in report["races"][0]["issues"]}
    assert "missing_prediction" in codes
    assert data_quality.should_fail(report, "critical") is True


def test_scraper_collection_failure_is_critical_even_if_partial_output_exists():
    raw = _raw()
    raw["collection_report"]["selected"].append(
        {"date": "2026-09-26", "venue": "阪神", "race_no": 11, "source_ref": "R2"}
    )
    raw["collection_report"]["failed"].append(
        {"date": "2026-09-26", "stage": "race", "venue": "阪神",
         "race_no": 11, "source_ref": "R2"}
    )

    report = data_quality.build_report(raw, _predictions())

    assert report["status"] == "critical"
    codes = {issue["code"] for issue in report["global_issues"]}
    assert "scraper_collection_failed" in codes
    assert "scraper_race_missing" in codes


def test_safe_fallbacks_are_warnings_not_critical():
    raw = _raw()
    race = raw["races"][0]
    for entry in race["entries"]:
        entry["jockey_stats"] = None
        entry["trainer_stats"] = None
    race["combo_odds"] = {}

    predictions = _predictions()
    predictions["races"][0]["speed_quality"] = {"used": False, "coverage": 0.4}

    report = data_quality.build_report(raw, predictions)

    assert report["status"] == "warning"
    codes = {issue["code"] for issue in report["races"][0]["issues"]}
    assert "low_jockey_stats_coverage" in codes
    assert "low_trainer_stats_coverage" in codes
    assert "incomplete_combo_odds" in codes
    assert "speed_guard_disabled" in codes
    assert data_quality.should_fail(report, "critical") is False
    assert data_quality.should_fail(report, "warning") is True


def test_zero_odds_or_past_runs_is_critical():
    raw = _raw()
    for entry in raw["races"][0]["entries"]:
        entry["win_odds"] = None
        entry["past_runs"] = []

    report = data_quality.build_report(raw, _predictions())

    codes = {issue["code"] for issue in report["races"][0]["issues"]}
    assert report["status"] == "critical"
    assert {"no_win_odds", "no_past_runs"} <= codes
