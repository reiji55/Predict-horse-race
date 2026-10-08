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
    assert view["value_fields"] == ["finish", "heads"]
    assert view["selection_fields"] == ["surface", "recency_order"]
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
    """想定内の取得失敗は fetcher が status で返す。artifact は作れて、処理は続く。"""
    statuses = {"A": "fetch_failed", "B": "blocked"}
    race = _race([_entry(1, CURRENT5, ref="A"), _entry(2, CURRENT5, ref="B"),
                  _entry(3, CURRENT5, ref=None)])
    race["entries"][2]["horse_ref"] = {}
    report = hi.capture({"races": [race]}, CONFIG, BEFORE, directory=tmp_path,
                        long_fetcher=lambda ref, n: {"status": statuses[ref], "runs": []})
    [path] = report["written"]
    inv = json.loads(Path(path).read_text(encoding="utf-8"))
    statuses_by_num = {h["num"]: h["long_history"]["status"] for h in inv["horses"]}
    assert statuses_by_num == {1: "fetch_failed", 2: "blocked", 3: "unavailable"}
    for horse in inv["horses"]:
        assert horse["long_history"]["available_runs"] == 0
        assert horse["coverage_delta"] is None
        assert horse["current_history"]["available_runs"] == 5
    assert inv["source_status"]["long_history_success_rate"] == 0.0


def test_program_errors_in_long_fetcher_are_not_swallowed(tmp_path: Path):
    """パーサや schema の不具合などは fetch_failed に変えず、そのまま投げる（手動 workflow を失敗させる）。"""
    import pytest
    race = _race([_entry(1, CURRENT5, ref="A")])
    for error in (KeyError("odds"), AssertionError("schema"), IndexError("row")):
        def broken(ref, n, _error=error):
            raise _error
        with pytest.raises(type(error)):
            hi.capture({"races": [race]}, CONFIG, BEFORE, directory=tmp_path, long_fetcher=broken)
    assert not (tmp_path / race["id"]).exists()


def test_artifact_records_implementation_revision(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    inv = hi.build_inventory(_race([_entry(1, CURRENT5)]), CONFIG, BEFORE)
    assert inv["implementation_revision"] == "abc123"


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


# 実ページの戦績表の見出し（thead の先頭28列。_parse_run_row が読む列はこの中にある）
REAL_HEADER_LABELS = ["日付", "開催", "天気", "R", "レース名", "映像", "頭数", "枠番", "馬番", "オッズ",
                      "人気", "着順", "騎手", "斤量", "距離", "水分量", "馬場", "馬場指数", "タイム", "着差",
                      "ﾀｲﾑ指数", "ﾀｲﾑ指数M", "ｽﾀｰﾄ指数", "追走指数", "上がり指数", "通過", "ペース", "上り"]
HISTORY_HEAD = "<thead><tr>" + "".join(f"<th>{label}</th>" for label in REAL_HEADER_LABELS) + "</tr></thead>"
HISTORY_ROWS = (
    _history_row("2026/06/07", "3東京2", "テストマイル(GI)", 17, 4, "芝1600", "良", "1:32.1", "0.0", "33.7")
    + _history_row("2026/04/05", "2阪神4", "テスト記念(GII)", 15, 5, "芝2000", "良", "1:58.4", "0.4", "35.3")
    + _history_row("2025/10/12", "4東京2", "テストS(GII)", 11, 2, "芝1800", "稍", "1:45.9", "0.1", "33.9")
)
SYNTHETIC_HISTORY_HTML = (
    "<html><body><table class='db_h_race_results'>" + HISTORY_HEAD + "<tbody>" + HISTORY_ROWS
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
    assert h1["current_recent_profile_view"]["value_fields"] == ["finish", "heads"]
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


# ---- レビュー対応：研究 config の再現性・window の打ち切り ------------------------------

def test_artifact_records_research_config_hash_and_registered_at():
    inv = hi.build_inventory(_race([_entry(1, CURRENT5)]), CONFIG, BEFORE)
    assert inv["history_inventory_config_sha256"] == hi.config_sha256(CONFIG)
    assert len(inv["history_inventory_config_sha256"]) == 64
    assert inv["registered_at"] == CONFIG["registered_at"]
    datetime.datetime.fromisoformat(inv["registered_at"])


def test_research_config_hash_changes_but_production_hash_does_not():
    production_before = model_registry.config_hash()
    changed = copy.deepcopy(CONFIG)
    changed["notable_runs"]["high_class_close_finish"]["max_margin_sec"] = 0.2
    assert hi.config_sha256(changed) != hi.config_sha256(CONFIG)
    # キーの並び順が違うだけなら同じ指紋（canonical JSON）
    reordered = json.loads(json.dumps(CONFIG), object_pairs_hook=lambda pairs: dict(reversed(pairs)))
    assert hi.config_sha256(reordered) == hi.config_sha256(CONFIG)
    inv = hi.build_inventory(_race([_entry(1, CURRENT5)]), changed, BEFORE)
    assert inv["history_inventory_config_sha256"] == hi.config_sha256(changed)
    assert model_registry.config_hash() == production_before


def _past_runs(n, start=datetime.date(2026, 9, 20)):
    """新しい順に n 走。最古の走だけ東京芝1800で2着（同コース同距離の実績）。"""
    runs = []
    for i in range(n):
        d = (start - datetime.timedelta(days=28 * i)).isoformat()
        if i == n - 1:
            runs.append(_run(d, finish=2, margin=0.1, race_name="テストS(GII)"))
        else:
            runs.append(_run(d, venue="中山", dist=2000, finish=9, margin=1.0, race_name=f"条件戦{i}"))
    return runs


def test_retrospective_long_window_keeps_fifteen_past_runs_after_dropping_race_day():
    """取得元 16 行＝先頭が当日のレース＋過去15走。cutoff のあとで 15 走に切るので、過去15走が全部残る。"""
    race_day = _run("2026-10-18", klass="g2", finish=1, margin=0.0, race_name="テストS(GII)")
    source = [race_day] + _past_runs(15)
    race = _race([_entry(1, CURRENT5)])
    inv = hi.build_inventory(race, CONFIG, AFTER, allow_retrospective=True,
                             long_fetcher=lambda ref, n: {"status": "ok", "runs": source,
                                                          "source_total_rows": len(source)})
    horse = inv["horses"][0]
    long_block = horse["long_history"]
    assert inv["retrieval_timing"] == "retrospective"
    assert long_block["source_total_rows"] == 16
    assert long_block["excluded_by_cutoff"]["same_day"] == 1
    assert long_block["eligible_runs"] == 15
    assert long_block["returned_runs"] == 15 == long_block["available_runs"]
    assert long_block["truncated"] is False
    assert "2026-10-18" not in long_block["dates"]
    oldest = source[-1]["date"]
    assert long_block["earliest_date"] == oldest
    # 15走目（最古）の同コース同距離の実績が、差分と根拠候補に使われる
    assert horse["coverage_delta"]["added_same_course_distance_runs"] == 1
    assert horse["long"]["conditions"]["same_course_distance_top3"] == 1
    tags = {t["type"]: t for t in horse["expert_evidence"]}
    older = tags["older_form_not_visible_in_current_window"]
    assert [e["date"] for e in older["evidence"]] == [oldest]
    # 当日の行はどの根拠にも入らない
    assert all(e.get("date") != "2026-10-18" for t in horse["expert_evidence"] for e in t["evidence"])
    assert horse["same_named_race"]["same_named_race_runs"] == 1
    assert horse["same_named_race"]["long_window_truncated"] is False


def test_long_window_truncation_is_explicit():
    """取得元 17 行＝当日＋過去16走なら、cutoff 後の 15 走を返して truncated=true。"""
    race_day = _run("2026-10-18", race_name="テストS(GII)")
    source = [race_day] + _past_runs(16)
    race = _race([_entry(1, CURRENT5)])
    inv = hi.build_inventory(race, CONFIG, AFTER, allow_retrospective=True,
                             long_fetcher=lambda ref, n: {"status": "ok", "runs": source,
                                                          "source_total_rows": len(source)})
    horse = inv["horses"][0]
    long_block = horse["long_history"]
    assert long_block["source_total_rows"] == 17
    assert long_block["eligible_runs"] == 16
    assert long_block["returned_runs"] == 15
    assert long_block["truncated"] is True
    assert source[-1]["date"] not in long_block["dates"]          # 16走目（最古）は window の外
    named = horse["same_named_race"]
    assert named["scope"] == "within_long_window"
    assert named["long_window"] == CONFIG["long_window"]
    assert named["long_window_truncated"] is True
    assert named["same_named_race_runs"] == 0                     # window の外にあるので数えない


def test_race_name_normalization_strips_grade_tokens_only():
    norm = hi.normalize_race_name
    assert norm("テストS(GII)") == norm("テストS")
    assert norm("天皇賞(春)(GI)") == norm("天皇賞(春)")
    assert norm("天皇賞(秋)(GI)") == norm("天皇賞(秋)")
    assert norm("天皇賞(春)") != norm("天皇賞(秋)")
    assert norm("天皇賞(春)(GI)") != norm("天皇賞(秋)(GI)")
    assert norm("東京大賞典(G1)") == norm("東京大賞典")
    assert norm("兵庫CS(JpnII)") == norm("兵庫CS")
    assert norm("オパールS(L)") == norm("オパールS")


def test_research_parser_counts_all_rows_beyond_the_window():
    parsed = c_horse_history.parse_horse_history_html_with_race_names(SYNTHETIC_HISTORY_HTML, 2)
    assert parsed["source_total_rows"] == 3
    assert len(parsed["runs"]) == 2
    # 既定（n_runs=None）は全行。研究側が cutoff のあとで long_window 走に切る
    everything = c_horse_history.parse_horse_history_html_with_race_names(SYNTHETIC_HISTORY_HTML)
    assert len(everything["runs"]) == 3


# ---- 取れなかったページの手がかり（ブロック原因の切り分け用） ----------------------------

class _FakeResp:
    def __init__(self, html, status=200, url="https://db.netkeiba.com/horse/2020100001/",
                 charset="utf-8", headers=None):
        self.content = html.encode(charset)
        self.text = html
        self.status_code = status
        self.url = url
        self.headers = headers or {}


BOT_PAGE = ("<html><head><title>Just a moment...</title></head><body>"
            "<div>Checking your browser. captcha required</div></body></html>")
LAYOUT_CHANGED_PAGE = (
    "<html><head><meta charset='EUC-JP'><title>テスト馬 | 競走馬データ</title></head><body>"
    "<table class='db_prof_table'><tr><td>生年月日</td></tr></table>"
    "<div id='horse_results_box'></div>"
    "<script>$.get('/horse/ajax_horse_results.html?id=2020100001')</script>"
    "</body></html>")


def test_blocked_page_records_diagnostics_not_the_page(monkeypatch):
    monkeypatch.setattr(c_horse_history, "http_get", lambda url: _FakeResp(BOT_PAGE))
    result = c_horse_history.fetch_horse_history_for_research("2020100001")
    assert result["status"] == "blocked" and result["runs"] == []
    diag = result["diagnostics"]
    assert diag["http_status"] == 200
    assert diag["title"] == "Just a moment..."
    assert diag["markers"]["captcha"] is True and diag["markers"]["cloudflare"] is True
    assert diag["markers"]["results_table_class_in_source"] is False
    assert diag["text_chars"] > 0
    for key in ("content", "html", "text", "text_excerpt"):    # ページ本体・本文の抜粋は残さない
        assert key not in diag


def test_layout_change_is_distinguishable_from_bot_page(monkeypatch):
    # ajax の読み込み先は取れなかったことにする（ここで見るのは馬のページの手がかり）
    monkeypatch.setattr(c_horse_history, "http_get",
                        _Router(_FakeResp(LAYOUT_CHANGED_PAGE, charset="euc_jp"), ajax=None))
    diag = c_horse_history.fetch_horse_history_for_research("2020100001")["diagnostics"]
    assert diag["title"] == "テスト馬 | 競走馬データ"          # EUC-JP でも文字化けしない
    assert diag["table_classes"] == ["db_prof_table"]
    assert diag["markers"]["captcha"] is False
    assert diag["markers"]["horse_results_word_in_source"] is True
    assert "/horse/ajax_horse_results.html" in diag["ajax_urls"]


def test_inventory_keeps_diagnostics_and_summarizes_them(tmp_path: Path):
    pages = {"A": _FakeResp(BOT_PAGE), "B": _FakeResp(LAYOUT_CHANGED_PAGE)}

    def fetcher(ref, n):
        return {"status": "blocked", "runs": [],
                "diagnostics": c_horse_history.describe_page_without_results_table(pages[ref])}

    race = _race([_entry(1, CURRENT5, ref="A"), _entry(2, CURRENT5, ref="B")])
    inv = hi.build_inventory(race, CONFIG, BEFORE, long_fetcher=fetcher)
    assert all(h["long_history"]["diagnostics"]["http_status"] == 200 for h in inv["horses"])
    summary = inv["source_status"]["blocked_page_summary"]
    assert summary["pages"] == 2
    assert summary["http_status"] == {"200": 2}
    assert summary["marker_hits"]["captcha"] == 1
    assert summary["marker_hits"]["horse_results_word_in_source"] == 1
    assert "/horse/ajax_horse_results.html" in summary["ajax_urls"]
    ok = hi.build_inventory(race, CONFIG, BEFORE)
    assert ok["source_status"]["blocked_page_summary"] is None


def test_diagnostics_never_persist_request_specific_text_or_url_tokens(tmp_path: Path):
    """public repo にコミットされる artifact に、外部が返した本文の一部や URL の一時トークンを残さない。"""
    leaky_page = ("<html><head><title>Just a moment...</title></head><body>"
                  "<p>Checking your browser. IP=203.0.113.1 token=SECRET Ray ID: 8f1e2d3c4b5a6978</p>"
                  "<form action='/?__cf_chl_tk=SECRET'><input type='hidden' value='203.0.113.1'></form>"
                  "</body></html>")
    pages = {
        # 同じ URL のまま challenge ページが返った（query と fragment に一時トークン）
        "A": _FakeResp(leaky_page, url="https://db.netkeiba.com/horse/2020100001/?token=SECRET#x"),
        # 別の URL へリダイレクトされた（userinfo と query にトークン）
        "B": _FakeResp(leaky_page,
                       url="https://user:SECRET@www.netkeiba.com/login/?pid=login&token=SECRET#frag"),
    }

    def fetcher(ref, n):
        diag = c_horse_history.describe_page_without_results_table(
            pages[ref], requested_url="https://db.netkeiba.com/horse/2020100001/")
        return {"status": "blocked", "runs": [], "diagnostics": diag}

    race = _race([_entry(1, CURRENT5, ref="A"), _entry(2, CURRENT5, ref="B")])
    report = hi.capture({"races": [race]}, CONFIG, BEFORE, directory=tmp_path, long_fetcher=fetcher)
    [path] = report["written"]
    persisted = Path(path).read_text(encoding="utf-8")
    for secret in ("203.0.113.1", "SECRET", "8f1e2d3c4b5a6978", "#x", "#frag", "token=", "user:"):
        assert secret not in persisted, secret

    inv = json.loads(persisted)
    diag_a = inv["horses"][0]["long_history"]["diagnostics"]
    diag_b = inv["horses"][1]["long_history"]["diagnostics"]
    assert diag_a["final_url"] == "https://db.netkeiba.com/horse/2020100001/"
    assert diag_a["redirected"] is False
    assert diag_b["final_url"] == "https://www.netkeiba.com/login/"
    assert diag_b["redirected"] is True
    assert diag_a["markers"]["cloudflare"] is True            # 判定に要る情報は真偽値で残る
    assert inv["source_status"]["blocked_page_summary"]["redirected_pages"] == 1


def test_url_without_query_keeps_only_scheme_host_path():
    strip = c_horse_history.url_without_query
    assert strip("https://db.netkeiba.com/horse/1/?a=1#b") == "https://db.netkeiba.com/horse/1/"
    assert strip("https://u:p@example.com:8443/x?y=z") == "https://example.com:8443/x"
    assert strip(None) is None and strip("not a url") is None


def test_long_titles_are_capped():
    page = "<html><head><title>" + "長" * 500 + "</title></head><body></body></html>"
    diag = c_horse_history.describe_page_without_results_table(_FakeResp(page))
    assert len(diag["title"]) == 120


# ---- 戦績表の ajax の読み込み先（研究用の取得だけ。本番の取得経路は変えない） ------------------

AJAX_URL = c_horse_history.HORSE_RESULTS_AJAX_URL
# ajax の data に入る HTML の断片（実ページでは #horse_results_box に差し込まれる部分）
AJAX_RESULTS_FRAGMENT = ("<div class='cate_bar'><h2>競走成績</h2></div>"
                         "<table class='db_h_race_results nk_tb_common'>" + HISTORY_HEAD
                         + "<tbody>" + HISTORY_ROWS + "</tbody></table>")


class _Router:
    """馬のページと ajax の読み込み先とで別の偽の応答を返す http_get。呼ばれ方も記録する。"""

    def __init__(self, page, ajax=None):
        self.page, self.ajax, self.calls = page, ajax, []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.ajax if url.startswith(AJAX_URL) else self.page


def _ajax_json(data, status="OK", url=AJAX_URL + "?input=UTF-8&output=json&id=2020100001"):
    return _FakeResp(json.dumps({"status": status, "data": data}, ensure_ascii=False), url=url,
                     headers={"Content-Type": "application/json; charset=UTF-8"})


def _without_race_name(runs):
    return [{k: v for k, v in r.items() if k != "race_name"} for r in runs]


def test_expected_columns_match_the_real_header_labels():
    """固定 index で読む列の見出しが、実ページの戦績表の見出しと一致している。"""
    assert {i: REAL_HEADER_LABELS[i] for i in c_horse_history.RESULT_COLUMNS} == c_horse_history.RESULT_COLUMNS


def test_research_fetcher_reads_results_from_the_ajax_endpoint(monkeypatch):
    router = _Router(_FakeResp(LAYOUT_CHANGED_PAGE, charset="euc_jp"), _ajax_json(AJAX_RESULTS_FRAGMENT))
    monkeypatch.setattr(c_horse_history, "http_get", router)
    result = c_horse_history.fetch_horse_history_for_research("2020100001")
    assert result["status"] == "ok" and result["retrieval"] == "ajax"
    assert result["source_total_rows"] == 3
    # 同じ表を本番のパーサで読んだ結果と、race_name 以外は完全に一致する
    assert _without_race_name(result["runs"]) == c_horse_history.parse_horse_history_html(SYNTHETIC_HISTORY_HTML, 15)
    assert [r["race_name"] for r in result["runs"]] == ["テストマイル(GI)", "テスト記念(GII)", "テストS(GII)"]
    # ページのスクリプトと同じ呼び方。1頭につき馬のページ1回＋ajax 1回だけ
    (page_url, _), (ajax_url, ajax_kwargs) = router.calls
    assert page_url == "https://db.netkeiba.com/horse/2020100001"
    assert ajax_url == AJAX_URL
    assert ajax_kwargs["params"] == {"input": "UTF-8", "output": "json", "id": "2020100001"}
    assert ajax_kwargs["headers"]["Referer"] == page_url
    assert "diagnostics" not in result and "ajax_diagnostics" not in result


def test_ajax_fragment_without_tbody_is_read_the_same(monkeypatch):
    fragment = AJAX_RESULTS_FRAGMENT.replace("<tbody>", "").replace("</tbody>", "")
    monkeypatch.setattr(c_horse_history, "http_get",
                        _Router(_FakeResp(LAYOUT_CHANGED_PAGE), _ajax_json(fragment)))
    result = c_horse_history.fetch_horse_history_for_research("2020100001")
    assert result["status"] == "ok" and result["source_total_rows"] == 3
    assert _without_race_name(result["runs"]) == c_horse_history.parse_horse_history_html(SYNTHETIC_HISTORY_HTML, 15)


def test_ajax_json_in_another_charset_is_read_like_a_browser(monkeypatch):
    """UTF-8 でない JSON（Content-Type の charset どおりの文字コード）でも、ブラウザと同じく読める。"""
    body = json.dumps({"status": "OK", "data": AJAX_RESULTS_FRAGMENT}, ensure_ascii=False)
    ajax = _FakeResp(body, url=AJAX_URL, charset="euc_jp",
                     headers={"Content-Type": "application/json; charset=EUC-JP"})
    monkeypatch.setattr(c_horse_history, "http_get", _Router(_FakeResp(LAYOUT_CHANGED_PAGE), ajax))
    result = c_horse_history.fetch_horse_history_for_research("2020100001")
    assert result["status"] == "ok" and result["source_total_rows"] == 3


def test_shifted_columns_are_not_read(monkeypatch):
    """列が1つ増えて位置がずれた表は、値を読まずに layout_mismatch にする（静かにズレた値を使わない）。"""
    labels = REAL_HEADER_LABELS[:2] + ["新しい列"] + REAL_HEADER_LABELS[2:]
    shifted_head = "<thead><tr>" + "".join(f"<th>{label}</th>" for label in labels) + "</tr></thead>"
    fragment = AJAX_RESULTS_FRAGMENT.replace(HISTORY_HEAD, shifted_head)
    monkeypatch.setattr(c_horse_history, "http_get",
                        _Router(_FakeResp(LAYOUT_CHANGED_PAGE), _ajax_json(fragment)))
    result = c_horse_history.fetch_horse_history_for_research("2020100001")
    assert result["status"] == "layout_mismatch" and result["runs"] == []
    assert result["header_positions"]["日付"] == 0 and result["header_positions"]["レース名"] == 5
    assert result["ajax_diagnostics"]["results_table_present"] is True
    assert result["ajax_diagnostics"]["header_matches"] is False
    # 馬のページに直接ある表でも同じ
    page = SYNTHETIC_HISTORY_HTML.replace(HISTORY_HEAD, shifted_head)
    monkeypatch.setattr(c_horse_history, "http_get", _Router(_FakeResp(page)))
    assert c_horse_history.fetch_horse_history_for_research("2020100001")["status"] == "layout_mismatch"


def test_ajax_failures_are_recorded_without_guessing(monkeypatch):
    maintenance = _FakeResp("<html><body>メンテナンス中</body></html>", url=AJAX_URL,
                            headers={"Content-Type": "text/html; charset=EUC-JP"})
    cases = {
        "http_failed": (None, "fetch_failed"),
        "status_ng": (_ajax_json("", status="NG"), "blocked"),
        "not_json_without_table": (maintenance, "blocked"),
    }
    results = {}
    for name, (ajax, expected) in cases.items():
        monkeypatch.setattr(c_horse_history, "http_get", _Router(_FakeResp(LAYOUT_CHANGED_PAGE), ajax))
        result = c_horse_history.fetch_horse_history_for_research("2020100001")
        assert result["status"] == expected, name
        assert result["runs"] == [] and result["retrieval"] == "ajax", name
        assert result["diagnostics"]["http_status"] == 200, name          # 馬のページの手がかりも残る
        results[name] = result["ajax_diagnostics"]
    assert results["http_failed"] == {"fetched": False}
    assert results["status_ng"]["json_parsed"] is True and results["status_ng"]["status_ok"] is False
    assert results["status_ng"]["results_table_present"] is False
    assert results["status_ng"]["header_matches"] is None
    assert results["not_json_without_table"]["json_parsed"] is False
    assert results["not_json_without_table"]["status_ok"] is None
    assert results["not_json_without_table"]["media_type"] == "text/html"


def test_page_without_the_ajax_reference_is_not_followed(monkeypatch):
    """ajax の読み込み先を参照していないページ（bot 判定の画面など）からは、ajax を取りに行かない。"""
    router = _Router(_FakeResp(BOT_PAGE), _ajax_json(AJAX_RESULTS_FRAGMENT))
    monkeypatch.setattr(c_horse_history, "http_get", router)
    result = c_horse_history.fetch_horse_history_for_research("2020100001")
    assert result["status"] == "blocked" and result["retrieval"] == "page"
    assert len(router.calls) == 1


def test_inline_table_on_the_page_is_used_without_ajax(monkeypatch):
    router = _Router(_FakeResp(SYNTHETIC_HISTORY_HTML))
    monkeypatch.setattr(c_horse_history, "http_get", router)
    result = c_horse_history.fetch_horse_history_for_research("2020100001")
    assert result["status"] == "ok" and result["retrieval"] == "page"
    assert len(router.calls) == 1


def test_inventory_records_retrieval_and_ajax_failure_summary(monkeypatch):
    shifted_head = "<thead><tr>" + "".join(
        f"<th>{label}</th>" for label in REAL_HEADER_LABELS[:2] + ["新しい列"] + REAL_HEADER_LABELS[2:]
    ) + "</tr></thead>"
    ajax_by_horse = {"A": _ajax_json(AJAX_RESULTS_FRAGMENT),
                     "B": _ajax_json(AJAX_RESULTS_FRAGMENT.replace(HISTORY_HEAD, shifted_head)),
                     "C": None}

    def fake_get(url, **kwargs):
        if url.startswith(AJAX_URL):
            return ajax_by_horse[kwargs["params"]["id"]]
        return _FakeResp(LAYOUT_CHANGED_PAGE, url=url)

    monkeypatch.setattr(c_horse_history, "http_get", fake_get)
    race = _race([_entry(1, CURRENT5, ref="A"), _entry(2, CURRENT5, ref="B"), _entry(3, CURRENT5, ref="C")])
    inv = hi.build_inventory(race, CONFIG, BEFORE, long_fetcher=c_horse_history.fetch_horse_history_for_research)
    by_num = {h["num"]: h["long_history"] for h in inv["horses"]}
    assert by_num[1]["status"] == "ok" and by_num[1]["retrieval"] == "ajax"
    assert by_num[1]["source_total_rows"] == 3 and by_num[1]["returned_runs"] == 3
    assert by_num[2]["status"] == "layout_mismatch" and by_num[2]["header_positions"]["レース名"] == 5
    assert by_num[3]["status"] == "fetch_failed" and by_num[3]["ajax_diagnostics"] == {"fetched": False}
    status = inv["source_status"]
    assert status["long_history_status_counts"] == {"ok": 1, "layout_mismatch": 1, "fetch_failed": 1}
    assert status["long_history_retrieval_counts"] == {"ajax": 1}
    summary = status["ajax_failure_summary"]
    assert summary["attempts"] == 2 and summary["http_failed"] == 1
    assert summary["results_table_present"] == 1 and summary["header_matches"] == 0
    assert status["blocked_page_summary"]["pages"] == 2      # 取れなかった2頭の馬のページの手がかり
    ok_only = hi.build_inventory(_race([_entry(1, CURRENT5, ref="A")]), CONFIG, BEFORE,
                                 long_fetcher=c_horse_history.fetch_horse_history_for_research)
    assert ok_only["source_status"]["ajax_failure_summary"] is None


def test_ajax_diagnostics_never_persist_response_text_or_url_tokens(monkeypatch, tmp_path: Path):
    """public repo にコミットされる artifact に、ajax の応答の本文や URL の一時トークンを残さない。"""
    leaky_html = _FakeResp(
        "<html><body><p>IP=203.0.113.1 token=SECRET Ray ID: 8f1e2d3c4b5a6978</p></body></html>",
        url=AJAX_URL + "?input=UTF-8&output=json&id=A&token=SECRET#x",
        headers={"Content-Type": "text/html; charset=EUC-JP; token=SECRET"})
    leaky_json = _ajax_json("<p>IP=203.0.113.1 token=SECRET</p>",
                            url="https://user:SECRET@db.netkeiba.com/horse/ajax_horse_results.html?token=SECRET")
    ajax_by_horse = {"A": leaky_html, "B": leaky_json}

    def fake_get(url, **kwargs):
        if url.startswith(AJAX_URL):
            return ajax_by_horse[kwargs["params"]["id"]]
        return _FakeResp(LAYOUT_CHANGED_PAGE, url=url)

    monkeypatch.setattr(c_horse_history, "http_get", fake_get)
    race = _race([_entry(1, CURRENT5, ref="A"), _entry(2, CURRENT5, ref="B")])
    report = hi.capture({"races": [race]}, CONFIG, BEFORE, directory=tmp_path,
                        long_fetcher=c_horse_history.fetch_horse_history_for_research)
    [path] = report["written"]
    persisted = Path(path).read_text(encoding="utf-8")
    for secret in ("203.0.113.1", "SECRET", "8f1e2d3c4b5a6978", "#x", "token=", "user:", "?input"):
        assert secret not in persisted, secret

    inv = json.loads(persisted)
    diag_a = inv["horses"][0]["long_history"]["ajax_diagnostics"]
    diag_b = inv["horses"][1]["long_history"]["ajax_diagnostics"]
    assert diag_a["final_url"] == AJAX_URL and diag_a["redirected"] is False
    assert diag_a["media_type"] == "text/html"
    assert diag_b["final_url"] == AJAX_URL and diag_b["json_parsed"] is True
    assert diag_b["status_ok"] is True and diag_b["results_table_present"] is False


def test_real_results_table_is_read_with_the_expected_columns():
    """手元に実サンプルがあるときだけ（git 管理外なので CI では skip）。

    実ページで ajax が差し込んだ戦績表（#horse_results_box の中身）を研究用のパーサで読み、
    見出しの確認が通ること・本番のパーサと race_name 以外が一致することを確かめる。
    """
    import pytest
    from bs4 import BeautifulSoup

    sample = ROOT / "tests" / "samples" / "horse_teiem.html"
    if not sample.exists():
        pytest.skip("tests/samples/horse_teiem.html が無い（git 管理外。tests/samples/README.md を参照）")
    html = sample.read_text(encoding="utf-8", errors="replace")
    fragment = BeautifulSoup(html, "lxml").select_one("#horse_results_box").decode_contents()
    parsed = c_horse_history.parse_horse_history_html_with_race_names(fragment)
    assert parsed["header_matches"] is True
    assert parsed["source_total_rows"] > 5
    assert _without_race_name(parsed["runs"]) == c_horse_history.parse_horse_history_html(html, n_runs=1000)
