"""
build_predictions のエンドツーエンドテスト。

`docs/samples/raw.sample.json`（共通内部フォーマット §2 の形）を入力に、
predictions.json がデータスキーマ仕様 v1.2 §1 の契約どおりに出てくることを検証する。
build_raw（スクレイパー側のオーケストレーター）が未実装でも logic を回せるようにするための入口。

基準タイム表は `tests/fixtures/base_times.sample.json` を使う
（本番の config/base_times.json は scripts/build_base_times.py の生成物で、現在は空）。

実行： python3 tests/test_build_predictions.py もしくは python -m pytest tests/test_build_predictions.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import build_predictions
from logic.cards import BET_SIZE, BET_TYPES

ROOT = Path(__file__).resolve().parent.parent
RAW_SAMPLE = ROOT / "docs" / "samples" / "raw.sample.json"
BASE_TIMES = ROOT / "tests" / "fixtures" / "base_times.sample.json"

REQUIRED_RACE_KEYS = {
    "id", "source_refs", "day", "venue", "race_no", "name", "grade",
    "post_time", "course", "myomi", "myomi_parts", "legendary", "marks", "cards",
}


def _build():
    raw = json.loads(RAW_SAMPLE.read_text(encoding="utf-8"))
    base_times = json.loads(BASE_TIMES.read_text(encoding="utf-8"))
    return build_predictions.build_predictions(raw, build_predictions.load_configs(), base_times)


def test_top_level_shape():
    """トップレベル：generated_at / week_id / myomi_threshold / races（スキーマ§1）。"""
    predictions = _build()
    assert set(predictions) == {"generated_at", "week_id", "myomi_threshold", "races"}
    assert predictions["week_id"] == "2026-W27"
    assert predictions["myomi_threshold"] == 80
    assert predictions["generated_at"].endswith("+09:00")
    assert len(predictions["races"]) == 1
    print("test_top_level_shape: OK")


def test_race_shape_and_marks():
    """races[]：必須キーが揃い、marks が base_score 降順で印が付いていること。"""
    race = _build()["races"][0]
    assert REQUIRED_RACE_KEYS <= set(race)
    assert race["id"] == "20260705-kokura-11"

    marks = race["marks"]
    assert len(marks) == 10                      # 全出走馬が載る（OPEN_QUESTIONS B-5）
    assert [m["mk"] for m in marks[:6]] == ["◎", "○", "▲", "△", "△", "✕"]
    assert all(m["mk"] == "" for m in marks[6:])  # 7位以下は無印
    assert marks[0]["hon"] is True

    scores = [m["score"] for m in marks]
    assert scores == sorted(scores, reverse=True)  # スコア降順
    assert all(m["odds"] is not None for m in marks)
    print("test_race_shape_and_marks: OK")


def test_myomi_and_parts():
    """myomi は0〜100、myomi_parts は umami×conf で復元でき、legendary が閾値と整合すること。"""
    race = _build()["races"][0]
    parts = race["myomi_parts"]

    assert 0 <= race["myomi"] <= 100
    assert abs(100 * parts["umami"] * parts["conf"] - race["myomi"]) < 0.1
    assert race["legendary"] == (race["myomi"] > 80)
    print("test_myomi_and_parts: OK (myomi %.1f / umami %.3f / conf %.3f)"
          % (race["myomi"], parts["umami"], parts["conf"]))


def test_cards_and_invariants():
    """cards[]：キャラ構成と、買い目生成仕様§5.5の不変条件を満たすこと。"""
    race = _build()["races"][0]
    chars = [c["char"] for c in race["cards"]]

    if race["legendary"]:
        assert chars == ["otori", "kei", "tetsu", "gen"]  # 降臨時は鳳が先頭
    else:
        assert chars == ["kei", "tetsu", "gen"]           # 通常は3枚

    mark_nums = {m["num"] for m in race["marks"]}
    for card in race["cards"]:
        assert sum(b["amt"] for b in card["bets"]) == card["total"]
        assert card["total"] == 500
        for bet in card["bets"]:
            assert bet["type"] in BET_TYPES
            assert len(bet["horses"]) == BET_SIZE[bet["type"]]
            assert set(bet["horses"]) <= mark_nums
            assert bet["amt"] % 100 == 0
        low, high = card["payout_range"]
        assert 0 < low <= high
        assert 0 <= card["hit_pct"] <= 100
    print("test_cards_and_invariants: OK (%s)" % ", ".join(chars))


def test_character_risk_ordering():
    """キャラのリスク位置づけ：ケイは的中率が最も高く、源さんは払戻上限が最も大きい。"""
    race = _build()["races"][0]
    by_char = {c["char"]: c for c in race["cards"]}

    assert by_char["kei"]["hit_pct"] > by_char["gen"]["hit_pct"]
    assert by_char["gen"]["payout_range"][1] > by_char["kei"]["payout_range"][1]
    print("test_character_risk_ordering: OK (ケイ %d%% / 哲 %d%% / 源 %d%%)" % (
        by_char["kei"]["hit_pct"], by_char["tetsu"]["hit_pct"], by_char["gen"]["hit_pct"]))


def test_speed_index_actually_contributes():
    """基準タイム表を渡しているので①が効いていること（空なら全馬speed=Noneで劣化する）。"""
    raw = json.loads(RAW_SAMPLE.read_text(encoding="utf-8"))
    base_times = json.loads(BASE_TIMES.read_text(encoding="utf-8"))
    configs = build_predictions.load_configs()

    with_times = build_predictions.build_predictions(raw, configs, base_times)
    without = build_predictions.build_predictions(raw, configs, {})

    assert [m["score"] for m in with_times["races"][0]["marks"]] != \
        [m["score"] for m in without["races"][0]["marks"]]
    # 基準タイム表が無い場合は信頼度が下限まで落ちる（data_cov=0）
    assert without["races"][0]["myomi_parts"]["conf"] == 0.5
    print("test_speed_index_actually_contributes: OK")


def test_failed_race_does_not_stop_the_week():
    """1レースが壊れていても週全体は落ちない（マナー設計と同じ思想）。"""
    raw = json.loads(RAW_SAMPLE.read_text(encoding="utf-8"))
    broken = json.loads(json.dumps(raw))
    broken["races"][0]["entries"] = []            # 出走馬ゼロ＝生成不能
    broken["races"].append(raw["races"][0])       # 正常なレースを後ろに追加

    predictions = build_predictions.build_predictions(
        broken, build_predictions.load_configs(),
        json.loads(BASE_TIMES.read_text(encoding="utf-8")))

    assert len(predictions["races"]) == 2
    assert predictions["races"][0]["marks"] == []   # 壊れたほうは空で通過
    assert len(predictions["races"][1]["marks"]) == 10
    print("test_failed_race_does_not_stop_the_week: OK")


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
    print(f"\nすべてのテストが通りました（{len(ALL_TESTS)}件）。")
