"""
C2（出馬表の「過去5走」）のパーサーのテスト。

実サンプル `tests/samples/shutuba_past_hanshin11.html`
（2026-09-19 阪神11R 大阪スポーツ杯）が必要。無ければスキップする。

このサンプルの検証には**独立した裏取り**がある：同じ馬（テイエムヒショウ）の
`db.netkeiba.com/horse/2020101216` を `c_horse_history` で解析した結果と、
全項目が一致することを `test_matches_the_horse_page_for_the_same_runs` で確認している。
別ページ・別パーサーで同じ値が出るので、どちらかの読み違いなら気づける。
"""
import json
from pathlib import Path

import pytest

from scraper.fetchers import c2_shutuba_past, c_horse_history

SAMPLES = Path(__file__).parent / "samples"
PAST5_SAMPLE = SAMPLES / "shutuba_past_hanshin11.html"
HORSE_SAMPLE = SAMPLES / "horse_teiem.html"

TEIEM = "2020101216"   # テイエムヒショウ（1番）


def _read(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"実サンプルが無いのでスキップします: {path.name}"
                    "（tests/samples/README.md の入手方法を参照）")
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def parsed():
    return c2_shutuba_past.parse_shutuba_past_html(_read(PAST5_SAMPLE))


def test_every_horse_in_the_race_is_found(parsed):
    assert len(parsed) == 16                      # 16頭立て
    assert TEIEM in parsed
    assert all(ref.isdigit() and len(ref) == 10 for ref in parsed)   # B出馬表と同じ10桁


def test_past_runs_are_five_or_four_when_the_horse_had_a_layoff(parsed):
    """休養マーカー（td.Rest）が5走目の枠を潰すので、休養明けの馬は4走になる。"""
    counts = sorted(len(v["past_runs"]) for v in parsed.values())
    assert set(counts) <= {4, 5}
    assert counts.count(5) == 12 and counts.count(4) == 4
    assert len(parsed[TEIEM]["past_runs"]) == 4   # テイエムヒショウは4ヵ月半休養


def test_odds_and_popularity_come_from_the_same_page(parsed):
    """このページには単勝オッズと人気も載っている（B2が取れなかったときの保険）。"""
    assert parsed[TEIEM]["win_odds"] == pytest.approx(40.5)
    assert parsed[TEIEM]["popularity"] == 12
    assert all(v["win_odds"] is not None for v in parsed.values())

    populars = sorted(v["popularity"] for v in parsed.values())
    assert populars == list(range(1, 17))         # 1〜16人気が重複なく揃う


def test_run_fields_match_the_spec(parsed):
    latest = parsed[TEIEM]["past_runs"][0]
    assert latest == {
        "date": "2026-05-03", "venue": "京都", "surface": "ダ", "dist": 1400,
        "going": "稍重", "class": "3win", "heads": 16, "finish": 14,
        "time_sec": pytest.approx(85.2), "last3f": pytest.approx(39.1),
        "margin_sec": pytest.approx(2.5), "impost": pytest.approx(54.0),
        "jockey_name": "川須栄彦", "note": None,
    }


def test_winning_run_has_margin_zero(parsed):
    """自身が1着なら margin_sec=0（共通内部フォーマット仕様§2.5）。生値は2着馬との負の着差。"""
    win = [r for r in parsed[TEIEM]["past_runs"] if r["finish"] == 1]
    assert len(win) == 1
    assert win[0]["margin_sec"] == 0.0
    assert win[0]["date"] == "2026-04-04"


def test_matches_the_horse_page_for_the_same_runs(parsed):
    """★ 別ページ（db.netkeiba.com の馬戦績）を別パーサーで読んだ結果と一致すること。"""
    from_horse_page = c_horse_history.parse_horse_history_html(_read(HORSE_SAMPLE))
    from_past5 = parsed[TEIEM]["past_runs"]

    assert len(from_horse_page) == 5 and len(from_past5) == 4   # 過去5走ページは休養ぶん1走少ない
    for a, b in zip(from_past5, from_horse_page):
        assert a == b, f"同じ走なのに値が違う:\n past5={json.dumps(a, ensure_ascii=False)}\n horse={json.dumps(b, ensure_ascii=False)}"


def test_missing_table_returns_empty_and_warns(caplog):
    """200が返っていても表が無ければ、黙って空を返さず警告を残す。"""
    html = "<html><head><title>アクセスが制限されています</title></head><body></body></html>"
    assert c2_shutuba_past.parse_shutuba_past_html(html) == {}
    assert "過去5走テーブル" in caplog.text


def test_merge_fills_past_runs_but_does_not_overwrite_api_odds():
    """オッズはB2 APIの方が新しいので上書きしない。空いているときだけ埋める。"""
    race = {"entries": [
        {"num": 1, "horse_ref": {"netkeiba": "111"}, "win_odds": 3.2, "popularity": 1},
        {"num": 2, "horse_ref": {"netkeiba": "222"}, "win_odds": None, "popularity": None},
        {"num": 3, "horse_ref": {"netkeiba": "999"}, "win_odds": None, "popularity": None},
    ]}
    by_horse = {
        "111": {"past_runs": [{"date": "2026-01-01"}], "win_odds": 9.9, "popularity": 9},
        "222": {"past_runs": [{"date": "2026-02-02"}], "win_odds": 5.5, "popularity": 4},
    }
    c2_shutuba_past.merge_into_race(race, by_horse)

    assert race["entries"][0]["win_odds"] == 3.2      # APIの値を守る
    assert race["entries"][0]["popularity"] == 1
    assert race["entries"][0]["past_runs"] == [{"date": "2026-01-01"}]
    assert race["entries"][1]["win_odds"] == 5.5      # 空いていたので埋める
    assert race["entries"][2].get("past_runs") is None  # 表に無い馬は触らない


def test_class_labels_cover_both_icon_and_sentence_forms():
    """過去5走ページはアイコンの "3勝"、出馬表は "3歳以上2勝クラス" と書き方が違う。"""
    from scraper.common import constants
    assert constants.normalize_class_label("東大路S 3勝") == "3win"
    assert constants.normalize_class_label("4歳以上2勝クラス") == "2win"
    assert constants.normalize_class_label("3歳未勝利") == "mi"
    assert constants.normalize_class_label("OP") == "op"
    assert constants.normalize_class_label("JpnII") == "g2"
    assert constants.normalize_class_label("GIII") == "g3"
    assert constants.normalize_class_label("") is None


def test_class_labels_from_the_past5_page_icons():
    """
    過去5走ページは省略レース名 + アイコン文字で出る（"兵庫チャン JpnII"）。
    完全一致だけ見ていた頃はここが全部 None になり、実データで46走が落ちていた。
    """
    from scraper.common import constants
    assert constants.normalize_class_label("兵庫チャン JpnII") == "g2"
    assert constants.normalize_class_label("バイオレッ OP") == "op"
    assert constants.normalize_class_label("カトレアS OP") == "op"
    assert constants.normalize_class_label("NHKマイルC(GI)") == "g1"
    assert constants.normalize_class_label("ホープフル(GIII)") == "g3"   # GI を誤って拾わない
    assert constants.normalize_class_label("リステッド (L)") == "op"
    # クラスの手がかりが無いものは正直に None（推測しない）
    assert constants.normalize_class_label("ブルートシ") is None


def test_no_grade_tag_is_matched_inside_a_race_name():
    """レース名に紛れた文字を拾わない（部分一致にしていない理由）。"""
    from scraper.common import constants
    assert constants.normalize_class_label("エルムステークス") is None   # "L" を拾わない
    assert constants.normalize_class_label("ジャパンC") is None


def test_past5_sample_has_no_unknown_class_except_nameless_rows(parsed):
    """実サンプルで、クラスが取れない走が大きく減っていること。"""
    runs = [r for v in parsed.values() for r in v["past_runs"]]
    unknown = [r for r in runs if r["class"] is None]
    assert len(unknown) <= 2, f"クラス不明が多すぎる: {len(unknown)}/{len(runs)}"
