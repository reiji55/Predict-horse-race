"""
build_raw（スクレイパーのオーケストレーター）のテスト。

**ネットワークには一切アクセスしない**。各フェッチャーを差し替えて、
オーケストレーターの責務だけを検証する：

- メインレースの絞り込み（config/scraper.json の main_race）
- 土日別実行で raw をマージすること（日曜の実行が土曜ぶんを消さない）
- 週内キャッシュ（既存 raw を読み直して同じ馬を取り直さない）
- D・E が取れなくてもパイプラインが止まらないこと

実行： python3 tests/test_build_raw.py  もしくは  python -m pytest tests/test_build_raw.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import build_raw
from scraper.fetchers import b2_odds, b_shutuba, c2_shutuba_past, c_horse_history
from scraper.fetchers.a_race_list import RaceListEntry

CONFIG = build_raw.load_config()


def _entry(venue: str, race_no: int, grade: str | None = None, **kwargs) -> RaceListEntry:
    defaults = dict(
        date="2026-07-05", day="日", venue=venue, race_no=race_no,
        name=f"{venue}{race_no}R", grade=grade, post_time="15:35",
        source_ref=f"2026100206{race_no:02d}", surface="芝", dist=1200, heads=16,
    )
    defaults.update(kwargs)
    return RaceListEntry(**defaults)


# --- メインレースの絞り込み（OPEN_QUESTIONS B-2） ----------------------

def test_select_main_races_by_race_no():
    """既定の mode="race_no"：各場の11Rだけを選ぶ（3場開催なら3レース）。"""
    entries = [_entry(v, r) for v in ("小倉", "福島", "函館") for r in (1, 10, 11, 12)]
    selected = build_raw.select_main_races(entries, CONFIG)

    assert len(selected) == 3
    assert {e.race_no for e in selected} == {11}
    assert {e.venue for e in selected} == {"小倉", "福島", "函館"}
    print("test_select_main_races_by_race_no: OK")


def test_select_main_races_by_grade():
    """mode="grade"：min_grade 以上だけを選ぶ（条件戦は落ちる）。"""
    entries = [
        _entry("小倉", 11, grade="g3"), _entry("福島", 11, grade="g1"),
        _entry("函館", 11, grade="op"), _entry("小倉", 9, grade="1win"),
        _entry("福島", 8, grade=None),
    ]
    config = json.loads(json.dumps(CONFIG))
    config["main_race"].update({"mode": "grade", "min_grade": "g3"})

    selected = build_raw.select_main_races(entries, config)
    assert {e.grade for e in selected} == {"g3", "g1"}
    print("test_select_main_races_by_grade: OK")


def test_select_main_races_excludes_non_jra_and_respects_cap():
    """JRA中央10場以外は除外（race_id が作れないため）。max_per_day で上限もかかる。"""
    entries = [_entry("小倉", 11), _entry("大井", 11), _entry("福島", 11), _entry("函館", 11)]
    assert "大井" not in {e.venue for e in build_raw.select_main_races(entries, CONFIG)}

    config = json.loads(json.dumps(CONFIG))
    config["main_race"]["max_per_day"] = 2
    assert len(build_raw.select_main_races(entries, config)) == 2
    print("test_select_main_races_excludes_non_jra_and_respects_cap: OK")


def test_select_main_races_all_mode_keeps_everything():
    """mode="all" は絞り込まない（ページ数が跳ねるので検証用）。"""
    entries = [_entry("小倉", r) for r in range(1, 13)]
    config = json.loads(json.dumps(CONFIG))
    config["main_race"]["mode"] = "all"
    config["main_race"]["max_per_day"] = None
    assert len(build_raw.select_main_races(entries, config)) == 12
    print("test_select_main_races_all_mode_keeps_everything: OK")


# --- 土日別実行（マージと週内キャッシュ） ------------------------------

def test_merge_races_keeps_saturday_and_updates_same_id():
    """同じidは新しい方で置き換え、別のidは追記される（日曜の実行で土曜ぶんを消さない）。"""
    saturday = [{"id": "20260704-tokyo-11", "name": "土曜のレース"}]
    sunday = [{"id": "20260705-kokura-11", "name": "日曜のレース"}]
    merged = build_raw.merge_races(saturday, sunday)
    assert [r["id"] for r in merged] == ["20260704-tokyo-11", "20260705-kokura-11"]

    updated = build_raw.merge_races(merged, [{"id": "20260704-tokyo-11", "name": "再取得"}])
    assert len(updated) == 2
    assert updated[0]["name"] == "再取得"
    print("test_merge_races_keeps_saturday_and_updates_same_id: OK")


def test_load_week_cache_reads_existing_raw(tmp_dir: Path | None = None):
    """既存 raw から {horse_ref: past_runs} を復元する（土曜ぶんの再利用）。"""
    with tempfile.TemporaryDirectory() as tmp:
        original = build_raw.OUTPUT_DIR
        build_raw.OUTPUT_DIR = Path(tmp)
        try:
            (Path(tmp) / "2026-W27.json").write_text(json.dumps({
                "week_id": "2026-W27",
                "races": [{"id": "20260704-tokyo-11", "entries": [
                    {"horse_ref": {"netkeiba": "2022104764"}, "past_runs": [{"date": "2026-06-21"}]},
                    {"horse_ref": {"netkeiba": "2022104765"}, "past_runs": []},  # 空はキャッシュしない
                ]}],
            }, ensure_ascii=False), encoding="utf-8")

            cache = build_raw.load_week_cache("2026-W27")
            assert list(cache) == ["2022104764"]
            assert build_raw.load_week_cache("2026-W99") == {}  # 未作成の週は空
        finally:
            build_raw.OUTPUT_DIR = original
    print("test_load_week_cache_reads_existing_raw: OK")


# --- 1レース分の組み立て（フェッチャーを差し替え） --------------------

class _FakeFetchers:
    """B / B2 / C2 / C を差し替えて、ネットワーク無しで build_race を動かす。

    past5: C2（出馬表の過去5走）が返す {horse_ref: {...}}。
           既定は空 ＝ C2 が空振りして C（馬ごとの戦績ページ）に落ちる経路を通す。
    """

    def __init__(self, past5: dict | None = None):
        self.horse_fetch_count = 0
        self.past5_fetch_count = 0
        self._past5 = past5 if past5 is not None else {}
        self._original = {}

    def __enter__(self):
        self._original = {
            "shutuba": b_shutuba.fetch_shutuba,
            "odds": b2_odds.fetch_odds,
            "history": c_horse_history.fetch_horse_history,
            "past5": c2_shutuba_past.fetch_shutuba_past,
        }
        b_shutuba.fetch_shutuba = self._fake_shutuba
        b2_odds.fetch_odds = self._fake_odds
        c_horse_history.fetch_horse_history = self._fake_history
        c2_shutuba_past.fetch_shutuba_past = self._fake_past5
        return self

    def __exit__(self, *_):
        b_shutuba.fetch_shutuba = self._original["shutuba"]
        b2_odds.fetch_odds = self._original["odds"]
        c_horse_history.fetch_horse_history = self._original["history"]
        c2_shutuba_past.fetch_shutuba_past = self._original["past5"]

    def _fake_past5(self, *args, **kwargs):  # noqa: D401 - テスト用スタブ
        self.past5_fetch_count += 1
        return dict(self._past5)

    def _fake_shutuba(self, *args, **kwargs):  # noqa: D401 - テスト用スタブ
        return {
            "id": None, "source_refs": {"netkeiba": "202610020611", "jravan": None},
            "date": None, "day": None, "venue": None, "race_no": None,
            "name": None, "grade": None, "post_time": None,
            "course": {"surface": None, "dist": None, "heads": None, "note": None},
            "going": "良", "weather": "晴", "odds_updated_at": None,
            "entries": [
                {"num": 1, "name": "テスト1", "horse_ref": {"netkeiba": "H1"},
                 "jockey": {"name": "J1", "ref": {"netkeiba": "05001"}},
                 "trainer": {"name": "T1", "ref": {"netkeiba": "01001"}},
                 "win_odds": None, "past_runs": [], "jockey_stats": None, "trainer_stats": None},
                {"num": 2, "name": "テスト2", "horse_ref": {"netkeiba": "H2"},
                 "jockey": {"name": "J2", "ref": {"netkeiba": "05002"}},
                 "trainer": {"name": "T2", "ref": {"netkeiba": "01002"}},
                 "win_odds": None, "past_runs": [], "jockey_stats": None, "trainer_stats": None},
            ],
        }

    def _fake_odds(self, race_source_ref):
        return {"official_datetime": "2026-07-05 07:05:00",
                "by_num": {1: {"win_odds": 3.2, "popularity": 1},
                           2: {"win_odds": 9.9, "popularity": 4}}}

    def _fake_history(self, horse_ref, n_runs=5):
        self.horse_fetch_count += 1
        return [{"date": "2026-06-21", "venue": "東京", "surface": "芝", "dist": 1800,
                 "going": "良", "class": "op", "heads": 16, "finish": 3, "time_sec": 108.0,
                 "last3f": 34.2, "margin_sec": 0.3, "impost": 55.0,
                 "jockey_name": "J1", "note": None}][:n_runs]


def test_build_race_fills_missing_fields_and_odds():
    """B が取りこぼした欄を A の値で補い、id を生成し、B2 のオッズで埋めること。"""
    with _FakeFetchers():
        race = build_raw.build_race(_entry("小倉", 11, grade="g3"), {}, n_runs=5)

    assert race["id"] == "20260705-kokura-11"       # date/venue/race_no から生成
    assert race["venue"] == "小倉" and race["race_no"] == 11 and race["day"] == "日"
    assert race["grade"] == "g3"
    assert race["course"]["dist"] == 1200 and race["course"]["heads"] == 16
    assert race["entries"][0]["win_odds"] == 3.2     # B2で補完
    assert race["odds_updated_at"] == "2026-07-05 07:05:00"
    assert len(race["entries"][0]["past_runs"]) == 1  # Cで補完
    print("test_build_race_fills_missing_fields_and_odds: OK")


def test_week_cache_avoids_refetching_the_same_horse():
    """同じ馬が複数レース・複数日に出ても、馬戦績ページは1回しか取りに行かない。"""
    with _FakeFetchers() as fake:
        cache: dict = {}
        build_raw.build_race(_entry("小倉", 11), cache, n_runs=5)
        assert fake.horse_fetch_count == 2           # 2頭ぶん

        build_raw.build_race(_entry("福島", 11), cache, n_runs=5)
        assert fake.horse_fetch_count == 2           # キャッシュヒットで増えない
    print("test_week_cache_avoids_refetching_the_same_horse: OK")


def test_past5_page_is_the_primary_route_and_skips_the_horse_pages():
    """C2（1レース1ページ）が取れたら、馬ごとの戦績ページ（C）は1回も叩かないこと。"""
    past5 = {
        "H1": {"past_runs": [{"date": "2026-06-21", "finish": 1}], "win_odds": 7.7, "popularity": 3},
        "H2": {"past_runs": [{"date": "2026-06-14", "finish": 5}], "win_odds": 12.0, "popularity": 6},
    }
    with _FakeFetchers(past5=past5) as fake:
        race = build_raw.build_race(_entry("小倉", 11), {}, n_runs=5)

    assert fake.past5_fetch_count == 1      # 1レース1ページ
    assert fake.horse_fetch_count == 0      # 16頭ぶんのページを叩かない
    assert race["entries"][0]["past_runs"] == past5["H1"]["past_runs"]
    assert race["entries"][0]["win_odds"] == 3.2   # B2 APIの値が優先される
    print("test_past5_page_is_the_primary_route_and_skips_the_horse_pages: OK")


def test_past5_results_feed_the_week_cache():
    """C2で取れた past_runs は週内キャッシュにも入り、日曜の実行で再利用できること。"""
    past5 = {
        "H1": {"past_runs": [{"date": "2026-06-21"}], "win_odds": None, "popularity": None},
        "H2": {"past_runs": [], "win_odds": None, "popularity": None},   # 新馬など、過去走ゼロ
    }
    with _FakeFetchers(past5=past5) as fake:
        cache: dict = {}
        build_raw.build_race(_entry("小倉", 11), cache, n_runs=5)

    assert cache["H1"] == [{"date": "2026-06-21"}]
    # 過去走が空の馬はキャッシュに入れない（Cの退避路にもう一度チャンスを与えるため）
    assert cache.get("H2") != []
    assert fake.horse_fetch_count == 1   # H2 だけがCに落ちる
    print("test_past5_results_feed_the_week_cache: OK")


# --- D・E が取れなくても止まらないこと ---------------------------------

def test_leading_stats_tolerate_fetch_failure():
    """リーディングの取得に失敗しても例外を投げず、空マップで続行すること。"""
    from scraper.fetchers import d_jockey_leading, e_trainer_leading
    original = (d_jockey_leading.fetch_jockey_leading, e_trainer_leading.fetch_trainer_leading)

    def _boom(*args, **kwargs):
        raise RuntimeError("ネットワーク断を想定")

    d_jockey_leading.fetch_jockey_leading = _boom
    e_trainer_leading.fetch_trainer_leading = _boom
    try:
        jockey_stats, trainer_stats = build_raw.fetch_leading_stats(["小倉", "福島"], "2026")
        assert jockey_stats == {} and trainer_stats == {}
    finally:
        d_jockey_leading.fetch_jockey_leading, e_trainer_leading.fetch_trainer_leading = original
    print("test_leading_stats_tolerate_fetch_failure: OK")


def test_leading_stats_spread_overall_to_all_venues():
    """場別リーディングは存在しないので、全場に同じ全国成績が入ること（OPEN_QUESTIONS B-3）。"""
    from scraper.fetchers import d_jockey_leading, e_trainer_leading
    original = (d_jockey_leading.fetch_jockey_leading, e_trainer_leading.fetch_trainer_leading)

    overall = {"05339": {"scope": "overall", "period": "2026", "starts": 372,
                         "wins": 103, "seconds": 63, "thirds": None}}
    d_jockey_leading.fetch_jockey_leading = lambda *a, **k: overall
    e_trainer_leading.fetch_trainer_leading = lambda *a, **k: {}
    try:
        jockey_stats, _ = build_raw.fetch_leading_stats(["小倉", "福島"], "2026")
        assert set(jockey_stats) == {"小倉", "福島"}
        assert jockey_stats["小倉"] is jockey_stats["福島"] is overall
    finally:
        d_jockey_leading.fetch_jockey_leading, e_trainer_leading.fetch_trainer_leading = original
    print("test_leading_stats_spread_overall_to_all_venues: OK")


def test_attach_stats_matches_by_ref():
    """jockey_ref / trainer_ref で着度数を振り分ける（§2.6）。一致しない馬は null のまま。"""
    with _FakeFetchers():
        race = build_raw.build_race(_entry("小倉", 11), {}, n_runs=5)

    build_raw.attach_stats(
        race,
        {"小倉": {"05001": {"scope": "venue", "venue": "小倉", "period": "2026",
                            "starts": 42, "wins": 8, "seconds": 6, "thirds": 5}}},
        {"01002": {"scope": "overall", "period": "2026",
                   "starts": 180, "wins": 22, "seconds": 19, "thirds": 25}},
    )

    assert race["entries"][0]["jockey_stats"]["starts"] == 42
    assert race["entries"][0]["trainer_stats"] is None       # 01001 は表に無い
    assert race["entries"][1]["jockey_stats"] is None        # 05002 は表に無い
    assert race["entries"][1]["trainer_stats"]["starts"] == 180
    print("test_attach_stats_matches_by_ref: OK")


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
    print(f"\nすべてのテストが通りました（{len(ALL_TESTS)}件）。")
