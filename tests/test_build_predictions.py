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
    "post_time", "course", "model_id", "model_role", "git_commit", "config_hash",
    "speed_quality", "myomi", "myomi_parts", "legendary", "chappy_decision", "marks", "cards",
}


def _build():
    raw = json.loads(RAW_SAMPLE.read_text(encoding="utf-8"))
    base_times = json.loads(BASE_TIMES.read_text(encoding="utf-8"))
    return build_predictions.build_predictions(raw, build_predictions.load_configs(), base_times)


def test_top_level_shape():
    """トップレベル：generated_at / week_id / myomi_threshold / races（スキーマ§1）。"""
    predictions = _build()
    assert set(predictions) == {"generated_at", "week_id", "model", "myomi_threshold", "races"}
    assert predictions["model"]["model_id"] == "win-v1-speed-guard"
    assert predictions["model"]["model_role"] == "champion"
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

    quality = race["speed_quality"]
    assert quality["used"] is True
    assert quality["coverage"] >= quality["min_race_coverage"]
    print("test_race_shape_and_marks: OK")


def test_myomi_and_parts():
    """myomi は0〜100、myomi_parts は umami×conf で復元でき、legendary が閾値と整合すること。"""
    race = _build()["races"][0]
    parts = race["myomi_parts"]

    assert 0 <= race["myomi"] <= 100
    assert abs(100 * parts["umami"] * parts["conf"] - race["myomi"]) < 0.1
    # 鳳は今や「myomi>80」だけではなく、Chappy high-conviction gateで決まる。
    assert race["legendary"] == bool(
        (race.get("chappy_decision") or {}).get("otori_gate", {}).get("passed", False)
    )
    print("test_myomi_and_parts: OK (myomi %.1f / umami %.3f / conf %.3f)"
          % (race["myomi"], parts["umami"], parts["conf"]))


def test_cards_and_invariants():
    """cards[]：キャラ構成と、買い目生成仕様§5.5の不変条件を満たすこと。"""
    race = _build()["races"][0]
    chars = [c["char"] for c in race["cards"]]

    if race["legendary"]:
        assert chars == ["otori", "kei", "tetsu", "gen"]  # Chappy枠が鳳へ置換され先頭
        assert "chappy" not in chars
    else:
        assert chars == ["kei", "tetsu", "gen", "chappy"] # 通常3人+Chappy1000円
        assert "otori" not in chars

    mark_nums = {m["num"] for m in race["marks"]}
    for card in race["cards"]:
        assert sum(b["amt"] for b in card["bets"]) == card["total"]
        if card["char"] in ("chappy", "otori"):
            assert card["total"] == 1000
        else:
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
    # 基準タイム表が無い場合はspeed guardがレース全体をOFFにし、信頼度も下限まで落ちる。
    assert without["races"][0]["speed_quality"]["used"] is False
    assert without["races"][0]["speed_quality"]["coverage"] == 0.0
    assert without["races"][0]["myomi_parts"]["conf"] == 0.5
    print("test_speed_index_actually_contributes: OK")


def test_challenger_changes_place_partners_without_replacing_champion_default():
    """同じrawでChampionとTop3 Challengerを作り、役割と買い目差分を監査できること。"""
    from logic import model_registry

    raw = json.loads(RAW_SAMPLE.read_text(encoding="utf-8"))
    base_times = json.loads(BASE_TIMES.read_text(encoding="utf-8"))
    configs = build_predictions.load_configs()
    registry = model_registry.load_registry()
    challenger_spec = next(m for m in registry["challengers"] if m["id"] == "top3-partner-v1")

    champion = build_predictions.build_predictions(
        raw, configs, base_times, model_spec=registry["champion"],
        generated_at="2026-09-22T02:00:00+09:00",
    )
    challenger = build_predictions.build_predictions(
        raw, configs, base_times, model_spec=challenger_spec,
        generated_at="2026-09-22T02:00:00+09:00",
    )

    assert champion["model"]["model_role"] == "champion"
    assert challenger["model"]["model_role"] == "challenger"
    assert champion["races"][0]["model_id"] == "win-v1-speed-guard"
    assert challenger["races"][0]["model_id"] == "top3-partner-v1"

    champ_cards = {x["char"]: x for x in champion["races"][0]["cards"]}
    chall_cards = {x["char"]: x for x in challenger["races"][0]["cards"]}

    # Championは旧方式、Challengerだけplace partner modeを持つ。
    assert champ_cards["gen"]["place_partner_mode"] is None
    assert chall_cards["gen"]["place_partner_mode"] == "edge"

    champ_wide = [b["horses"] for b in champ_cards["gen"]["bets"] if b["type"] == "ワイド"]
    chall_wide = [b["horses"] for b in chall_cards["gen"]["bets"] if b["type"] == "ワイド"]
    assert champ_wide != chall_wide

    # 馬連はTop3モデルの対象外なので、哲さんの馬連は同じWin側選定を維持する。
    champ_umaren = [b["horses"] for b in champ_cards["tetsu"]["bets"] if b["type"] == "馬連"]
    chall_umaren = [b["horses"] for b in chall_cards["tetsu"]["bets"] if b["type"] == "馬連"]
    assert champ_umaren == chall_umaren


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



def test_race_regime_challenger_passes_gen_only_on_forced_solid():
    """影モデルではSOLID時だけ源さんが降り、ケイ/哲と自動Chappyは残る。"""
    raw = json.loads(RAW_SAMPLE.read_text(encoding="utf-8"))
    base_times = json.loads(BASE_TIMES.read_text(encoding="utf-8"))
    configs = json.loads(json.dumps(build_predictions.load_configs()))

    # 統合テストなので分類式そのものは別テストに任せ、ここではSOLIDを確実に作る。
    configs["race_regime"]["solid"] = {
        "min_market_top3_share": 0.0,
        "max_market_entropy": 1.0,
        "max_model_market_tv": 1.0,
        "min_top3_overlap": 0,
    }
    model_spec = {
        "id": "race-regime-abstain-v1",
        "role": "challenger",
        "use_top3_partner": False,
        "use_race_regime": True,
    }

    built = build_predictions.build_predictions(
        raw, configs, base_times, model_spec=model_spec,
        generated_at="2026-09-24T01:00:00+09:00",
    )
    race = built["races"][0]
    assert race["race_regime"]["label"] == "solid"
    assert race["race_regime_policy_active"] is True

    by_char = {card["char"]: card for card in race["cards"]}
    assert by_char["kei"].get("action", "bet") == "bet"
    assert by_char["tetsu"].get("action", "bet") == "bet"

    gen = by_char["gen"]
    assert gen["action"] == "pass"
    assert gen["total"] == 0 and gen["budget"] == 500 and gen["bets"] == []
    assert "見送" in gen["say"]

    chappy_card = by_char.get("chappy") or by_char.get("otori")
    assert chappy_card is not None
    assert chappy_card["decision_log"]["role_policy"]["long_edge_selector"] == "solid_depth"

ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
    print(f"\nすべてのテストが通りました（{len(ALL_TESTS)}件）。")
