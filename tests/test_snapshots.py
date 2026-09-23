"""
発走前予想の凍結（logic/snapshots.py）のテスト。

守りたい性質はひとつだけ：**発走後のビルドは、発走前に記録した予想を書き換えられない**。
2026-09-19 に発走後の再生成分が採点され、妙味も買い目も後付けで変わっていた事故の再発防止。
"""
import datetime
import json

import pytest

from logic import snapshots

JST = snapshots.JST


def _race(race_id="20260919-hanshin-11", post_time="15:30", myomi=78.2,
          legendary=False, first_horse=4):
    return {
        "id": race_id, "day": "土", "venue": "阪神", "race_no": 11,
        "name": "大阪スポーツ杯", "grade": None, "post_time": post_time,
        "course": {"surface": "ダ", "dist": 1800},
        "myomi": myomi, "myomi_parts": {"umami": 0.5, "conf": 0.8}, "legendary": legendary,
        "marks": [{"mk": "◎", "hon": True, "num": first_horse, "waku": 2,
                   "name": "テスト馬", "odds": 4.3, "score": 81.2}],
        "cards": [{"char": "kei", "total": 500,
                   "bets": [{"type": "ワイド", "horses": [first_horse, 9], "amt": 500}]}],
    }


def _predictions(races, generated_at="2026-09-19T14:14:00+09:00"):
    return {"generated_at": generated_at, "week_id": "2026-W38",
            "myomi_threshold": 80, "races": races}


def _at(hour, minute=0):
    return datetime.datetime(2026, 9, 19, hour, minute, tzinfo=JST)


def test_post_datetime_comes_from_the_race_id_not_the_weekday():
    """`day` は "土" という曜日なので日付に使えない。日付は race_id の先頭8桁から取る。"""
    assert snapshots.post_datetime(_race()) == _at(15, 30)
    assert snapshots.post_datetime({"id": "2026-bad", "post_time": "15:30"}) is None
    assert snapshots.post_datetime({"id": "20260919-hanshin-11", "post_time": None}) is None


def test_pre_race_build_writes_the_snapshot(tmp_path):
    report = snapshots.freeze(_predictions([_race()]), now=_at(14, 14), directory=tmp_path)

    assert report == [{"race_id": "20260919-hanshin-11", "action": "frozen", "pre_race": True}]
    saved = json.loads((tmp_path / "20260919-hanshin-11.json").read_text(encoding="utf-8"))
    assert saved["pre_race"] is True
    assert saved["frozen_at"] == "2026-09-19T14:14:00+09:00"   # ＝オッズの観測時刻
    assert saved["myomi"] == 78.2
    assert saved["cards"][0]["bets"][0]["horses"] == [4, 9]



def test_pre_race_regime_is_frozen_for_later_result_analysis(tmp_path):
    race = _race()
    race["race_regime"] = {
        "version": "race-regime-v1",
        "label": "solid",
        "metrics": {"market_top3_share": 0.7},
    }
    race["race_regime_policy_active"] = True

    snapshots.freeze(_predictions([race]), now=_at(14, 14), directory=tmp_path)
    saved = json.loads((tmp_path / "20260919-hanshin-11.json").read_text(encoding="utf-8"))

    assert saved["race_regime"]["label"] == "solid"
    assert saved["race_regime_policy_active"] is True

def test_a_later_pre_race_build_overwrites_it(tmp_path):
    """発走前ならオッズが新しいほど良いので上書きしてよい。"""
    snapshots.freeze(_predictions([_race(myomi=70.0)]), now=_at(7, 14), directory=tmp_path)
    snapshots.freeze(_predictions([_race(myomi=78.2)], generated_at="2026-09-19T14:14:00+09:00"),
                     now=_at(14, 14), directory=tmp_path)

    saved = json.loads((tmp_path / "20260919-hanshin-11.json").read_text(encoding="utf-8"))
    assert saved["myomi"] == 78.2


def test_post_race_build_cannot_touch_an_existing_snapshot(tmp_path):
    """★ 本丸。レース後に作り直した予想で過去を上書きさせない。"""
    snapshots.freeze(_predictions([_race(myomi=78.2, legendary=False, first_horse=4)]),
                     now=_at(14, 14), directory=tmp_path)

    # 17:42 の再生成（確定オッズ入り・妙味が跳ね上がり鳳が降臨し、買い目まで変わった）
    after = _predictions([_race(myomi=87.5, legendary=True, first_horse=13)],
                         generated_at="2026-09-19T17:42:29+09:00")
    report = snapshots.freeze(after, now=_at(17, 42), directory=tmp_path)

    assert report[0]["action"] == "kept"
    saved = json.loads((tmp_path / "20260919-hanshin-11.json").read_text(encoding="utf-8"))
    assert saved["myomi"] == 78.2 and saved["legendary"] is False
    assert saved["cards"][0]["bets"][0]["horses"] == [4, 9]


def test_a_prediction_born_after_the_post_time_is_recorded_but_not_scored(tmp_path, caplog):
    """発走後に初めて作られた予想は、結果を見た後の予想なので採点対象にしない。"""
    report = snapshots.freeze(_predictions([_race()]), now=_at(17, 42), directory=tmp_path)

    assert report[0]["action"] == "late"
    saved = json.loads((tmp_path / "20260919-hanshin-11.json").read_text(encoding="utf-8"))
    assert saved["pre_race"] is False
    assert "成績集計には載せません" in caplog.text

    assert snapshots.as_predictions(tmp_path)["races"] == []


def test_as_predictions_keeps_only_pre_race_ones(tmp_path):
    snapshots.freeze(_predictions([_race()]), now=_at(14, 14), directory=tmp_path)
    snapshots.freeze(_predictions([_race(race_id="20260919-nakayama-11", post_time="15:45")]),
                     now=_at(17, 42), directory=tmp_path)

    races = snapshots.as_predictions(tmp_path)["races"]
    assert [r["id"] for r in races] == ["20260919-hanshin-11"]
    # results 側がそのまま食える形（predictions.json の races[] と同じキー）
    assert races[0]["cards"][0]["char"] == "kei"
    assert races[0]["frozen_at"] == "2026-09-19T14:14:00+09:00"


def test_unknown_post_time_does_not_destroy_an_existing_snapshot(tmp_path):
    snapshots.freeze(_predictions([_race(myomi=78.2)]), now=_at(14, 14), directory=tmp_path)

    broken = _race(myomi=99.9)
    broken["post_time"] = None
    report = snapshots.freeze(_predictions([broken]), now=_at(17, 42), directory=tmp_path)

    assert report[0]["action"] == "unknown"
    saved = json.loads((tmp_path / "20260919-hanshin-11.json").read_text(encoding="utf-8"))
    assert saved["myomi"] == 78.2


def test_unreadable_snapshot_is_skipped_with_a_warning(tmp_path, caplog):
    (tmp_path / "20260919-hanshin-11.json").write_text("{壊れている", encoding="utf-8")
    assert snapshots.load_all(tmp_path) == []
    assert "読めませんでした" in caplog.text


def test_finished_races_are_shown_as_they_were_frozen(tmp_path):
    """
    ★ 発走後に走ったビルドが、画面の「今日の予想」を書き換えないこと。

    採点はスナップショットを見るので記録は汚れないが、predictions.json は上書きされる。
    2026-09-19 の17:42の実行では、確定オッズで作り直した予想（妙味87.5・鳳あり・
    買い目も別物）がそのまま画面に出ていた。
    """
    snapshots.freeze(_predictions([_race(myomi=78.2, first_horse=4)]),
                     now=_at(14, 14), directory=tmp_path)

    after = _predictions([_race(myomi=87.5, legendary=True, first_horse=13)],
                         generated_at="2026-09-19T17:42:29+09:00")
    snapshots.freeze(after, now=_at(17, 42), directory=tmp_path)
    restored = snapshots.restore_finished_races(after, now=_at(17, 42), directory=tmp_path)

    assert restored == 1
    race = after["races"][0]
    assert race["myomi"] == 78.2 and race["legendary"] is False
    assert race["cards"][0]["bets"][0]["horses"] == [4, 9]
    assert race["frozen_at"] == "2026-09-19T14:14:00+09:00"


def test_races_that_have_not_started_keep_the_latest_odds(tmp_path):
    """発走前のレースは差し替えない（新しいオッズで作り直した方が良いので）。"""
    snapshots.freeze(_predictions([_race(myomi=70.0)]), now=_at(7, 14), directory=tmp_path)

    latest = _predictions([_race(myomi=78.2)], generated_at="2026-09-19T14:14:00+09:00")
    snapshots.freeze(latest, now=_at(14, 14), directory=tmp_path)
    restored = snapshots.restore_finished_races(latest, now=_at(14, 14), directory=tmp_path)

    assert restored == 0
    assert latest["races"][0]["myomi"] == 78.2


def test_a_race_without_a_pre_race_snapshot_is_left_alone(tmp_path):
    """発走後に初めて作られた予想（pre_race=false）は差し替え元にしない。"""
    after = _predictions([_race(myomi=87.5)], generated_at="2026-09-19T17:42:29+09:00")
    snapshots.freeze(after, now=_at(17, 42), directory=tmp_path)

    assert snapshots.restore_finished_races(after, now=_at(17, 42), directory=tmp_path) == 0
    assert after["races"][0]["myomi"] == 87.5
