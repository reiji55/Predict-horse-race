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
    assert report["publish_blocked"] is True
    assert data_quality.should_fail(report, "critical") is True


def test_partial_collection_failure_is_reported_but_does_not_block_publishing():
    """
    ★ 1レースの取得失敗で、正常に作れた他のレースの公開まで止めない。

    push を止めると、正常なレースの発走前スナップショットも Actions の作業領域ごと消える。
    次の実行が発走後なら、そのレースは採点対象から外れる（E-1 で守ってきた記録を失う）。
    失敗は必ず記録し、ERROR/WARNING で見える形にする。
    """
    raw = _raw()
    raw["collection_report"]["selected"].append(
        {"date": "2026-09-26", "venue": "阪神", "race_no": 11, "source_ref": "R2"}
    )
    raw["collection_report"]["failed"].append(
        {"date": "2026-09-26", "stage": "race", "venue": "阪神",
         "race_no": 11, "source_ref": "R2"}
    )

    report = data_quality.build_report(raw, _predictions())

    assert report["status"] == "warning"
    assert report["publish_blocked"] is False
    codes = {issue["code"] for issue in report["global_issues"]}
    assert {"scraper_collection_failed", "scraper_race_missing"} <= codes
    assert data_quality.should_fail(report, "critical") is False


def test_total_collection_failure_blocks_publishing():
    """この実行の対象レースが1件も作れなければ止める（push しても何も増えない＝異常を知らせる）。"""
    raw = _raw()
    raw["collection_report"]["built"] = []
    raw["collection_report"]["failed"] = [
        {"date": "2026-09-26", "stage": "race", "venue": "中山", "race_no": 11, "source_ref": "R1"}
    ]

    report = data_quality.build_report(raw, _predictions())

    assert report["status"] == "critical"
    assert report["publish_blocked"] is True
    assert data_quality.should_fail(report, "critical") is True


def test_race_list_failure_blocks_publishing():
    raw = _raw()
    raw["collection_report"].update(selected=[], built=[], failed=[
        {"date": "2026-09-26", "stage": "race_list", "venue": None,
         "race_no": None, "source_ref": None}
    ])

    report = data_quality.build_report(raw, _predictions())
    assert report["publish_blocked"] is True


def test_an_older_broken_race_does_not_block_todays_healthy_run():
    """
    ★ raw は週単位でマージされる。既に公開済みの前日のレースの欠損が、
    今日の正常な実行を止め続けてはいけない（止めると週末いっぱい何も出なくなる）。
    """
    raw = _raw()
    broken = {
        "id": "20260926-hanshin-11", "venue": "阪神", "race_no": 11, "name": "昨日のS",
        "entries": [dict(_entry(i), win_odds=None) for i in range(1, 6)],
        "combo_odds": raw["races"][0]["combo_odds"],
    }
    raw["races"].insert(0, broken)
    # 今日の実行で作ったのは中山11Rだけ
    predictions = _predictions()
    predictions["races"].append({
        "id": "20260926-hanshin-11", "model_id": "win-v1-speed-guard",
        "marks": [{"num": 1}], "cards": [{"char": "kei"}],
        "speed_quality": {"used": True, "coverage": 1.0},
    })

    report = data_quality.build_report(raw, predictions)

    assert report["summary"]["critical_races"] == 1          # 欠損は記録される
    assert report["summary"]["races_in_this_run"] == ["20260926-nakayama-11"]
    assert report["publish_blocked"] is False                 # でも今日の公開は止めない
    assert data_quality.should_fail(report, "critical") is False


def test_every_race_of_this_run_broken_blocks_publishing():
    """この実行で作ったレースが全部 critical なら、系統的な障害として止める。"""
    raw = _raw()
    for entry in raw["races"][0]["entries"]:
        entry["past_runs"] = []

    report = data_quality.build_report(raw, _predictions())

    assert report["status"] == "critical"
    assert report["publish_blocked"] is True
    assert any("すべて critical" in r for r in report["blocking_reasons"])


def test_old_raw_without_collection_report_is_still_checked():
    """collection_report が無い古い raw でも落ちない。全レースを対象に判定する。"""
    raw = _raw()
    del raw["collection_report"]

    report = data_quality.build_report(raw, _predictions())

    assert report["status"] == "ok"
    assert report["summary"]["races_in_this_run"] == ["20260926-nakayama-11"]
    assert report["collection_report"] == {}


def test_jockey_coverage_at_its_normal_level_does_not_warn():
    """
    騎手成績は D が上位約100人しか載せないため、平常時でも 56〜92%（W38 実測）。
    80% にすると毎回ほぼ全レースで鳴って警告が無視されるので、閾値を分けている。
    """
    raw = _raw()
    entries = raw["races"][0]["entries"]
    entries[0]["jockey_stats"] = None
    entries[1]["jockey_stats"] = None          # 3/5 = 60%：平常時の範囲

    report = data_quality.build_report(raw, _predictions())
    codes = {issue["code"] for issue in report["races"][0]["issues"]}
    assert "low_jockey_stats_coverage" not in codes

    for entry in entries:
        entry["jockey_stats"] = None           # 0%：D の取得そのものが失敗
    report = data_quality.build_report(raw, _predictions())
    codes = {issue["code"] for issue in report["races"][0]["issues"]}
    assert "low_jockey_stats_coverage" in codes


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
    assert report["races"][0]["status"] == "critical"
    assert {"no_win_odds", "no_past_runs"} <= codes
    # この実行で作った唯一のレースが壊れている＝全滅なので止める
    assert report["status"] == "critical" and report["publish_blocked"] is True
