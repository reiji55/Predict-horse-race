"""PR-B：過去走の棚卸し（history-feature-inventory-v1、observe-only）のテスト。

予想・買い目・snapshot・config_hash を一切変えないこと、cutoff・長期履歴の失敗・
「取得済みだが未使用」の明示・オッズを使わない根拠タグを確かめる。
"""
from __future__ import annotations

import copy
import datetime
import json
import re
from pathlib import Path

from logic import build_predictions, model_registry, snapshots
from research import history_inventory as hi
from scraper.fetchers import c_horse_history

ROOT = Path(__file__).resolve().parent.parent
CONFIG = hi.load_config()
JST = hi.JST
FIXTURE = ROOT / "tests" / "fixtures" / "history_inventory" / "20261004_tokyo11_three_horses.json"

AFTER = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=JST)
BEFORE = datetime.datetime(2026, 10, 18, 9, 0, tzinfo=JST)


def _run(date, venue="東京", surface="芝", dist=1800, going="良", klass="op",
         finish=5, heads=16, margin=0.5, race_name=None):
    run = {"date": date, "venue": venue, "surface": surface, "dist": dist, "going": going,
           "class": klass, "heads": heads, "finish": finish, "time_sec": 107.5,
           "last3f": 34.0, "margin_sec": margin, "impost": 57.0, "jockey_name": "J", "note": None}
    if race_name is not None:
        run["race_name"] = race_name
    return run


def _race(entries, race_id="20261018-tokyo-11", name="テストS", going="良"):
    return {"id": race_id, "post_time": "15:45", "venue": "東京", "name": name, "going": going,
            "course": {"surface": "芝", "dist": 1800}, "entries": entries}


def _entry(num, past_runs, ref="H1", odds=10.0):
    return {"num": num, "name": f"テスト{num}", "horse_ref": {"netkeiba": ref},
            "win_odds": odds, "past_runs": past_runs}


CURRENT5 = [
    _run("2026-09-01", venue="新潟", finish=8),
    _run("2026-07-01", venue="中山", finish=10),
    _run("2026-05-01", venue="京都", finish=12),
    _run("2026-03-01", venue="阪神", finish=9),
    _run("2026-01-10", venue="中京", finish=11),
]
OLDER_TOKYO = _run("2025-10-12", finish=2, margin=0.1, race_name="テストS(GII)")


# ---- 1. current 5 / long 15 の差分 ---------------------------------------------------

def test_long_history_adds_older_same_course_distance_run():
    long_runs = [dict(r) for r in CURRENT5] + [OLDER_TOKYO, _run("2025-05-01", venue="京都", dist=2000)]
    race = _race([_entry(1, CURRENT5)])
    inv = hi.build_inventory(race, CONFIG, BEFORE,
                             long_fetcher=lambda ref, n: {"status": "ok", "runs": long_runs})
    horse = inv["horses"][0]
    assert inv["retrieval_timing"] == "pre_race"
    assert horse["current"]["conditions"]["same_course_distance_runs"] == 0
    assert horse["long"]["conditions"]["same_course_distance_runs"] == 1
    assert horse["coverage_delta"]["added_runs"] == 2
    assert horse["coverage_delta"]["added_same_course_distance_runs"] == 1
    assert horse["coverage_delta"]["added_same_distance_runs"] == 1
    tags = {t["type"]: t for t in horse["expert_evidence"]}
    assert tags["older_form_not_visible_in_current_window"]["source"] == "long_only"
    assert "course_distance_proven" in tags["older_form_not_visible_in_current_window"]["underlying_types"]
    # レース名が取れた長期履歴だけ、同じレース名の実績を出す
    assert horse["same_named_race"]["status"] == "ok"
    assert horse["same_named_race"]["same_named_race_runs"] == 1
    assert horse["same_named_race"]["same_named_race_top3"] == 1
    assert inv["source_status"]["long_history_success_rate"] == 1.0


# ---- 2. current が4走なら4走のまま ----------------------------------------------------

def test_current_with_four_runs_is_not_filled():
    inv = hi.build_inventory(_race([_entry(1, CURRENT5[:4])]), CONFIG, BEFORE)
    current = inv["horses"][0]["current_history"]
    assert current["requested_runs"] == 5
    assert current["available_runs"] == 4
    assert len(current["dates"]) == 4


# ---- 3. class / margin は取得済みだが現行の着順系評価で未使用 ------------------------------

def test_class_and_margin_are_available_but_unused():
    inv = hi.build_inventory(_race([_entry(1, CURRENT5)]), CONFIG, BEFORE)
    usage = inv["feature_usage"]
    for field in ("class", "margin_sec", "last3f"):
        assert usage[field]["available"] is True
        assert usage[field]["used_by_form_evaluation"] is False
        assert usage[field]["available_but_unused_by_form_evaluation"] == 5
    assert usage["finish"]["used_by_form_evaluation"] is True
    assert usage["finish"]["available_but_unused_by_form_evaluation"] == 0
    view = inv["horses"][0]["current_recent_profile_view"]
    assert view["uses"] == ["finish", "heads"]
    assert view["present_but_unused"]["class"] == 3
    assert view["present_but_unused"]["margin_sec"] == 3


def test_declared_feature_usage_matches_the_prediction_code():
    """config の used_by 宣言が、予想経路のコードの実態とずれていないこと。"""
    prediction_modules = ["aptitude", "base_score", "build_predictions", "cards", "chappy",
                          "human_score", "myomi", "prob_model", "race_regime", "speed_index",
                          "top3_score"]
    readers: dict[str, set[str]] = {}
    for module in prediction_modules:
        text = (ROOT / "logic" / f"{module}.py").read_text(encoding="utf-8")
        for field in ("class", "margin_sec", "time_sec", "last3f", "impost"):
            if re.search(rf'run\.get\("{field}"\)|run\["{field}"\]|r\.get\("{field}"\)', text):
                readers.setdefault(field, set()).add(module)
    assert readers.get("margin_sec", set()) == set()
    assert readers.get("last3f", set()) == set()
    assert readers.get("class", set()) == {"speed_index"}
    assert readers.get("time_sec", set()) == {"speed_index"}
    assert CONFIG["feature_usage"]["margin_sec"]["used_by"] == []
    assert CONFIG["feature_usage"]["last3f"]["used_by"] == []


# ---- 4. cutoff ----------------------------------------------------------------------

def test_cutoff_drops_same_day_and_future_rows_from_long_history():
    long_runs = [_run("2026-10-18", finish=1, margin=0.0, klass="g2"),   # 当日（このレースの結果）
                 _run("2026-11-01", finish=1),                            # 未来
                 _run("not-a-date")] + [dict(r) for r in CURRENT5]
    race = _race([_entry(1, CURRENT5)])
    inv = hi.build_inventory(race, CONFIG, AFTER, allow_retrospective=True,
                             long_fetcher=lambda ref, n: {"status": "ok", "runs": long_runs})
    horse = inv["horses"][0]
    assert inv["cutoff_rule"] == "run.date < race.date"
    excluded = horse["long_history"]["excluded_by_cutoff"]
    assert excluded == {"count": 3, "same_day": 1, "future": 1, "undated": 1,
                        "dates": ["2026-10-18", "2026-11-01"]}
    assert "2026-10-18" not in horse["long_history"]["dates"]
    assert horse["coverage_delta"]["added_runs"] == 0
    assert all(t["type"] != "high_class_close_finish" for t in horse["expert_evidence"])


# ---- 5. 長期履歴の取得失敗 -------------------------------------------------------------

def test_long_fetch_failure_still_writes_artifact(tmp_path: Path):
    def broken(ref, n):
        raise RuntimeError("blocked by bot detection")

    statuses = iter(["blocked", "fetch_failed"])
    race = _race([_entry(1, CURRENT5, ref="A"), _entry(2, CURRENT5, ref="B"),
                  _entry(3, CURRENT5, ref=None)])
    race["entries"][2]["horse_ref"] = {}
    fetchers = {"A": broken, "B": lambda ref, n: {"status": next(statuses), "runs": []}}
    report = hi.capture({"races": [race]}, CONFIG, BEFORE, directory=tmp_path,
                        long_fetcher=lambda ref, n: fetchers[ref](ref, n))
    [path] = report["written"]
    inv = json.loads(Path(path).read_text(encoding="utf-8"))
    statuses_by_num = {h["num"]: h["long_history"]["status"] for h in inv["horses"]}
    assert statuses_by_num == {1: "fetch_failed", 2: "blocked", 3: "unavailable"}
    for horse in inv["horses"]:
        assert horse["long_history"]["available_runs"] == 0
        assert horse["coverage_delta"] is None
        assert horse["current_history"]["available_runs"] == 5
    assert inv["source_status"]["long_history_success_rate"] == 0.0


def test_research_fetcher_reports_blocked_page_without_guessing(monkeypatch):
    class _Resp:
        text = "<html><title>Access Denied</title><body></body></html>"
    monkeypatch.setattr(c_horse_history, "http_get", lambda url: _Resp())
    assert c_horse_history.fetch_horse_history_for_research("X", 15)["status"] == "blocked"
    monkeypatch.setattr(c_horse_history, "http_get", lambda url: None)
    assert c_horse_history.fetch_horse_history_for_research("X", 15)["status"] == "fetch_failed"


def _history_row(date, kaisai, race_name, heads, finish, dist, going, time, margin, last3f):
    cells = [""] * 28
    cells[0], cells[1], cells[4], cells[6] = date, kaisai, race_name, str(heads)
    cells[11], cells[12], cells[13], cells[14] = str(finish), "騎手", "57.0", dist
    cells[16], cells[18], cells[19], cells[27] = going, time, margin, last3f
    cells[23] = "480(+2)"
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


SYNTHETIC_HISTORY_HTML = (
    "<html><body><table class='db_h_race_results'><thead><tr><th>日付</th></tr></thead><tbody>"
    + _history_row("2026/06/07", "3東京2", "テストマイル(GI)", 17, 4, "芝1600", "良", "1:32.1", "0.0", "33.7")
    + _history_row("2026/04/05", "2阪神4", "テスト記念(GII)", 15, 5, "芝2000", "良", "1:58.4", "0.4", "35.3")
    + _history_row("2025/10/12", "4東京2", "テストS(GII)", 11, 2, "芝1800", "稍", "1:45.9", "0.1", "33.9")
    + "</tbody></table></body></html>"
)


def test_research_parser_keeps_production_past_runs_unchanged():
    html = SYNTHETIC_HISTORY_HTML
    research = c_horse_history.parse_horse_history_html_with_race_names(html, 15)
    production = c_horse_history.parse_horse_history_html(html, 15)
    assert [{k: v for k, v in r.items() if k != "race_name"} for r in research["runs"]] == production
    assert len(production) == 3
    assert [r["race_name"] for r in research["runs"]] == [
        "テストマイル(GI)", "テスト記念(GII)", "テストS(GII)"]
    assert production[0]["class"] == "g1" and production[0]["margin_sec"] == 0.0
    assert "race_name" not in production[0]


# ---- 6. high_class_close_finish は一般 fixture で付く ------------------------------------

def test_high_class_close_finish_tag_is_generic():
    runs = [_run("2026-06-01", klass="g1", finish=4, margin=0.0),
            _run("2026-05-01", klass="g3", finish=6, margin=0.3),
            _run("2026-04-01", klass="g2", finish=8, margin=0.4),     # 着差が閾値の外
            _run("2026-03-01", klass="op", finish=2, margin=0.1)]     # クラスが閾値の外
    inv = hi.build_inventory(_race([_entry(7, runs)], race_id="20261018-kyoto-11"), CONFIG, BEFORE)
    notable = inv["horses"][0]["performance_evidence"]["notable_runs"]
    assert [n["evidence"]["date"] for n in notable] == ["2026-06-01", "2026-05-01"]
    assert all(n["status"] == "hypothesis_candidate" for n in notable)
    source = (ROOT / "research" / "history_inventory.py").read_text(encoding="utf-8")
    for hardcoded in ("セイウンハーデス", "20261004", "毎日王冠"):
        assert hardcoded not in source


# ---- 7. 根拠タグはオッズを使わない ------------------------------------------------------

def test_expert_evidence_does_not_depend_on_odds():
    runs = [OLDER_TOKYO, _run("2026-06-01", klass="g1", finish=4, margin=0.0)]
    cheap = hi.build_inventory(_race([_entry(1, runs, odds=2.0)]), CONFIG, BEFORE)
    longshot = hi.build_inventory(_race([_entry(1, runs, odds=150.0)]), CONFIG, BEFORE)
    assert cheap["horses"][0]["expert_evidence"] == longshot["horses"][0]["expert_evidence"]
    assert CONFIG["expert_evidence"]["uses_odds"] is False
    import inspect
    assert "odds" not in inspect.signature(hi.expert_evidence).parameters


# ---- 8. 構造化データが無い根拠は作らない -------------------------------------------------

def test_unavailable_evidence_is_never_guessed():
    inv = hi.build_inventory(_race([_entry(1, CURRENT5)]), CONFIG, BEFORE)
    unavailable = inv["horses"][0]["expert_evidence_unavailable"]
    assert {u["type"] for u in unavailable} >= {"trouble_last_run", "slow_start", "workout_quality",
                                                "paddock_condition", "stable_intent"}
    assert all(u["status"] == "unavailable" for u in unavailable)
    produced = {t["type"] for t in inv["horses"][0]["expert_evidence"]}
    assert not produced & {u["type"] for u in unavailable}
    # current の過去走にはレース名が無いので、同じレース名の実績は作らない
    assert inv["horses"][0]["same_named_race"]["status"] == "unavailable"


# ---- 9. retrospective / pre_race の区別 -----------------------------------------------

def test_pre_race_and_retrospective_are_separated(tmp_path: Path):
    race = _race([_entry(1, CURRENT5)])
    assert hi.build_inventory(race, CONFIG, AFTER) is None          # 発走後は既定で作らない
    retro = hi.build_inventory(race, CONFIG, AFTER, allow_retrospective=True)
    pre = hi.build_inventory(race, CONFIG, BEFORE)
    assert retro["retrieval_timing"] == "retrospective"
    assert pre["retrieval_timing"] == "pre_race"
    report = hi.capture({"races": [race]}, CONFIG, AFTER, directory=tmp_path)
    assert report["written"] == []
    assert report["skipped"][0]["reason"] == "posted_or_unknown_post_time"
    p1 = hi.write_inventory(pre, tmp_path)
    p2 = hi.write_inventory(pre, tmp_path)              # 同じ時刻でも上書きしない
    assert p1 != p2 and p1.name.endswith("_pre_race.json")


# ---- 10. 予想・買い目・snapshot・config_hash は完全一致 -----------------------------------

def test_predictions_are_unchanged_by_inventory(tmp_path: Path):
    raw = json.loads((ROOT / "docs" / "samples" / "raw.sample.json").read_text(encoding="utf-8"))
    base_times = json.loads((ROOT / "tests" / "fixtures" / "base_times.sample.json")
                            .read_text(encoding="utf-8"))
    configs = build_predictions.load_configs()
    hash_before = model_registry.config_hash()
    before = build_predictions.build_predictions(copy.deepcopy(raw), configs, base_times,
                                                 generated_at="X")
    raw_copy = copy.deepcopy(raw)
    hi.capture(raw_copy, CONFIG, datetime.datetime(2000, 1, 1, tzinfo=JST), directory=tmp_path)
    assert raw_copy == raw                            # 棚卸しは raw を書き換えない
    after = build_predictions.build_predictions(copy.deepcopy(raw), configs, base_times,
                                                generated_at="X")
    assert after == before
    for b, a in zip(before["races"], after["races"]):
        assert snapshots.build_snapshot(b, "W", "t", True) == snapshots.build_snapshot(a, "W", "t", True)
    assert model_registry.config_hash() == hash_before
    assert "history_inventory.json" not in model_registry.HASH_CONFIGS
    assert json.loads((ROOT / "config" / "scraper.json").read_text(encoding="utf-8"))["past_runs"] == 5


# ---- 11. 10/4 東京（retrospective 監査） ------------------------------------------------

def test_tokyo_1004_retrospective_three_horses():
    race = json.loads(FIXTURE.read_text(encoding="utf-8"))
    inv = hi.build_inventory(race, CONFIG, datetime.datetime(2026, 10, 6, 12, 0, tzinfo=JST),
                             allow_retrospective=True)
    assert inv["retrieval_timing"] == "retrospective"
    by_num = {h["num"]: h for h in inv["horses"]}

    # #1：current raw に G1 4着・margin 0.0 があるのに、recent_profile は class / margin を見ていない
    h1 = by_num[1]
    g1_close = [r for r in h1["performance_evidence"]["recent_runs"]
                if r["class"] == "g1" and r["finish"] == 4 and r["margin_sec"] == 0.0]
    assert len(g1_close) == 1
    assert h1["current_recent_profile_view"]["uses"] == ["finish", "heads"]
    assert h1["current_recent_profile_view"]["present_but_unused"]["margin_sec"] >= 1
    assert inv["feature_usage"]["margin_sec"]["available_but_unused_by_form_evaluation"] > 0
    assert any(t["type"] == "high_class_close_finish" for t in h1["expert_evidence"])

    # #13：current window では近走が低調で、同コース同距離は0走。長期履歴は取っていないので推測しない
    h13 = by_num[13]
    assert h13["current"]["conditions"]["same_course_distance_runs"] == 0
    assert all(r["finish"] is not None and r["finish"] >= 10
               for r in h13["performance_evidence"]["recent_runs"])
    assert h13["long_history"]["status"] == "not_requested"
    assert h13["coverage_delta"] is None
    assert h13["expert_evidence"] == []

    # #3：G2 3着などの current evidence を独立に表示（#1・#13 と同じ原因と決め打ちしない）
    h3 = by_num[3]
    assert any(r["class"] == "g2" and r["finish"] == 3
               for r in h3["performance_evidence"]["recent_runs"])
    assert {t["type"] for t in h3["expert_evidence"]} != {t["type"] for t in h1["expert_evidence"]}
