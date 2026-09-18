"""
基準タイム表の構築（scripts/build_base_times.py）のテスト。

ネットワークを使う経路3（netkeiba検索）は、パーサー（`parse_race_search_html`）だけを
合成HTMLで検証する。リクエスト側のパラメータは実サンプル未取得なので未検証のまま
（OPEN_QUESTIONS C-5）。
"""
import json

import pytest

from scripts import build_base_times as bbt

CLASS_OFFSET = {"mi": 2.5, "1win": 1.5, "2win": 1.0, "3win": 0.5,
                "op": 0.0, "g3": -0.3, "g2": -0.6, "g1": -1.0}


def _rec(**kwargs):
    base = {"venue": "京都", "surface": "芝", "dist": 1200, "going": "良",
            "class": "op", "win_time": 68.0}
    base.update(kwargs)
    return base


def test_normalize_subtracts_class_offset_scaled_by_distance():
    """§4：win_time − class_offset × dist/2000。未勝利の遅い勝ちタイムがOP水準に引き直される。"""
    # 未勝利1200mで69.5秒 → 69.5 − 2.5×0.6 = 68.0（OP水準）
    value = bbt.normalize_win_time(_rec(**{"class": "mi", "win_time": 69.5}), CLASS_OFFSET)
    assert value == pytest.approx(68.0)

    # G1は逆方向（期待が速いので正規化すると遅くなる）：68.0 − (−1.0)×0.6 = 68.6
    value = bbt.normalize_win_time(_rec(**{"class": "g1", "win_time": 68.0}), CLASS_OFFSET)
    assert value == pytest.approx(68.6)


def test_normalize_returns_none_for_unknown_class():
    assert bbt.normalize_win_time(_rec(**{"class": None}), CLASS_OFFSET) is None
    assert bbt.normalize_win_time(_rec(**{"class": "障害"}), CLASS_OFFSET) is None
    assert bbt.normalize_win_time(_rec(win_time=None), CLASS_OFFSET) is None


def test_build_table_takes_median_of_normalized_times():
    """外れ値1本に引っ張られないことを中央値で確認する（§4の「中央値を使う理由」）。"""
    times = [67.6, 67.8, 68.0, 68.2, 95.0]  # 最後は極端な外れ値
    records = [_rec(win_time=t) for t in times]
    table, report = bbt.build_table(records, CLASS_OFFSET, min_samples=5)

    assert table["京都"]["芝"]["1200"] == pytest.approx(68.0)  # 平均なら73.3になる
    assert report["counts"]["京都/芝/1200"] == 5


def test_build_table_mixes_classes_onto_one_scale():
    """全クラスを混ぜても、正規化後は同じ水準に揃う（§4「全クラスを使う理由」）。"""
    records = [
        _rec(**{"class": "mi", "win_time": 69.5}),   # → 68.0
        _rec(**{"class": "1win", "win_time": 68.9}),  # → 68.0
        _rec(**{"class": "op", "win_time": 68.0}),    # → 68.0
        _rec(**{"class": "g3", "win_time": 67.8}),    # → 67.98
        _rec(**{"class": "g1", "win_time": 67.4}),    # → 68.0
    ]
    table, _ = bbt.build_table(records, CLASS_OFFSET, min_samples=5)
    assert table["京都"]["芝"]["1200"] == pytest.approx(68.0, abs=0.05)


def test_build_table_drops_courses_below_min_samples():
    records = [_rec(win_time=68.0) for _ in range(3)]
    table, report = bbt.build_table(records, CLASS_OFFSET, min_samples=5)

    assert table == {}
    assert report["counts"]["京都/芝/1200"] == 3
    assert "京都/芝/1200" in report["courses_missing"]


def test_build_table_excludes_non_jra_and_jump_races():
    records = [
        _rec(venue="大井"),        # 地方（§2条件2）
        _rec(venue="ロンシャン"),   # 海外
        _rec(surface="障"),        # 障害（§2条件3）
    ]
    table, report = bbt.build_table(records, CLASS_OFFSET, min_samples=1)
    assert table == {}
    assert report["skipped"] == 3


def test_build_table_can_filter_by_going():
    records = [_rec(win_time=68.0, going="良") for _ in range(3)]
    records += [_rec(win_time=71.0, going="不良") for _ in range(3)]

    all_going, _ = bbt.build_table(records, CLASS_OFFSET, min_samples=1)
    firm_only, _ = bbt.build_table(records, CLASS_OFFSET, min_samples=1, goings=["良"])

    assert all_going["京都"]["芝"]["1200"] == pytest.approx(69.5)  # 6本の中央値
    assert firm_only["京都"]["芝"]["1200"] == pytest.approx(68.0)


def test_courses_cover_all_ten_jra_venues():
    from scraper.common import constants
    assert set(bbt.COURSES) == constants.JRA_VENUES
    assert set(bbt.VENUE_CODE) == constants.JRA_VENUES
    total = sum(len(d) for by_surface in bbt.COURSES.values() for d in by_surface.values())
    assert 100 <= total <= 130  # 仕様§4の「約120通り」


def test_collect_from_raw_picks_winning_runs_and_dedupes(tmp_path):
    """raw の past_runs から finish==1 の走だけを拾い、同じレースは重複排除する。"""
    raw = {
        "races": [{
            "horses": [
                {"past_runs": [
                    {"date": "2026-05-10", "venue": "東京", "surface": "芝", "dist": 1600,
                     "going": "良", "class": "op", "finish": 1, "time_sec": 93.4},
                    {"date": "2026-04-05", "venue": "中山", "surface": "芝", "dist": 1600,
                     "going": "良", "class": "op", "finish": 3, "time_sec": 94.1},
                ]},
                {"past_runs": [
                    # 1頭目と同じレース（勝ち馬を別の馬の過去走として重複して持っている状態）
                    {"date": "2026-05-10", "venue": "東京", "surface": "芝", "dist": 1600,
                     "going": "良", "class": "op", "finish": 1, "time_sec": 93.4},
                    {"date": "2026-03-01", "venue": "阪神", "surface": "ダ", "dist": 1800,
                     "going": "重", "class": "1win", "finish": 1, "time_sec": None},  # 時計欠損
                ]},
            ]
        }]
    }
    (tmp_path / "2026-W19.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    records = bbt.collect_from_raw(tmp_path)

    assert len(records) == 1
    assert records[0]["venue"] == "東京"
    assert records[0]["win_time"] == 93.4


def test_collect_from_raw_tolerates_missing_dir_and_broken_json(tmp_path):
    assert bbt.collect_from_raw(tmp_path / "nope") == []
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert bbt.collect_from_raw(tmp_path) == []


def test_load_records_reads_csv_with_mmss_times(tmp_path):
    path = tmp_path / "times.csv"
    path.write_text(
        "venue,surface,dist,class,going,win_time\n"
        "東京,芝,1600,op,良,1:33.4\n"
        "京都,芝,1200,mi,良,69.5\n",
        encoding="utf-8",
    )
    records = bbt.load_records(path)
    assert records[0]["win_time"] == pytest.approx(93.4)
    assert records[1]["win_time"] == pytest.approx(69.5)
    assert records[0]["dist"] == 1600


SEARCH_HTML = """
<table class="race_table_01">
  <thead><tr>
    <th>日付</th><th>開催</th><th>天気</th><th>R</th><th>レース名</th><th>頭数</th>
    <th>着順</th><th>騎手</th><th>距離</th><th>馬場</th><th>タイム</th>
  </tr></thead>
  <tbody>
    <tr><td>2026/05/10</td><td>2東京6</td><td>晴</td><td>11</td><td>NHKマイルC(GI)</td><td>18</td>
        <td>1</td><td>Ｃ.ルメール</td><td>芝1600</td><td>良</td><td>1:33.4</td></tr>
    <tr><td>2026/05/10</td><td>2東京6</td><td>晴</td><td>11</td><td>NHKマイルC(GI)</td><td>18</td>
        <td>2</td><td>武豊</td><td>芝1600</td><td>良</td><td>1:33.5</td></tr>
    <tr><td>2026/05/09</td><td>2東京5</td><td>曇</td><td>5</td><td>3歳未勝利</td><td>16</td>
        <td>1</td><td>横山武史</td><td>ダ1400</td><td>稍</td><td>1:24.8</td></tr>
  </tbody>
</table>
"""


def test_parse_race_search_html_keeps_only_winners():
    records = bbt.parse_race_search_html(SEARCH_HTML)

    assert len(records) == 2
    first = records[0]
    assert first["date"] == "2026-05-10"
    assert first["venue"] == "東京"          # "2東京6" から場名だけ
    assert first["surface"] == "芝"
    assert first["dist"] == 1600
    assert first["class"] == "g1"            # "(GI)" を正規化
    assert first["going"] == "良"
    assert first["win_time"] == pytest.approx(93.4)

    second = records[1]
    assert second["surface"] == "ダ"
    assert second["class"] == "mi"           # "3歳未勝利"
    assert second["going"] == "稍重"         # "稍" を正規化


def test_parse_race_search_resolves_columns_by_header_not_index():
    """列順が変わっても正しく読める（C-6 の列インデックス直指定問題を持ち込まない）。"""
    shuffled = SEARCH_HTML.replace(
        "<th>日付</th><th>開催</th><th>天気</th><th>R</th><th>レース名</th><th>頭数</th>\n"
        "    <th>着順</th><th>騎手</th><th>距離</th><th>馬場</th><th>タイム</th>",
        "<th>着順</th><th>タイム</th><th>距離</th><th>馬場</th><th>レース名</th><th>頭数</th>\n"
        "    <th>日付</th><th>騎手</th><th>開催</th><th>天気</th><th>R</th>",
    ).replace(
        "<td>2026/05/10</td><td>2東京6</td><td>晴</td><td>11</td><td>NHKマイルC(GI)</td><td>18</td>\n"
        "        <td>1</td><td>Ｃ.ルメール</td><td>芝1600</td><td>良</td><td>1:33.4</td>",
        "<td>1</td><td>1:33.4</td><td>芝1600</td><td>良</td><td>NHKマイルC(GI)</td><td>18</td>\n"
        "        <td>2026/05/10</td><td>Ｃ.ルメール</td><td>2東京6</td><td>晴</td><td>11</td>",
    )
    records = bbt.parse_race_search_html(shuffled)

    winner = [r for r in records if r["date"] == "2026-05-10"][0]
    assert winner["dist"] == 1600
    assert winner["win_time"] == pytest.approx(93.4)
    assert winner["venue"] == "東京"


def test_parse_race_search_raises_when_required_headers_missing():
    """静かに誤読するくらいなら止める。"""
    broken = "<table class='race_table_01'><thead><tr><th>日付</th><th>馬名</th></tr></thead>" \
             "<tbody><tr><td>2026/05/10</td><td>ヴァルキリーバース</td></tr></tbody></table>"
    with pytest.raises(ValueError, match="必要な列"):
        bbt.parse_race_search_html(broken)


def test_parse_race_search_returns_empty_without_table():
    assert bbt.parse_race_search_html("<html><body>該当するレースがありません</body></html>") == []


# 2026-09-18 の本番実行で netkeiba が実際に返したヘッダー。
# **1行＝1レース**で着順の列が無く、タイム＝勝ちタイム。
REAL_SEARCH_HTML = """
<table class="race_table_01">
  <thead><tr>
    <th>開催日</th><th>開催</th><th>天気</th><th>R</th><th>レース名</th><th>映像</th>
    <th>距離</th><th>頭数</th><th>馬場</th><th>タイム</th><th>ペース</th>
    <th>勝ち馬</th><th>騎手</th><th>調教師</th><th>2着馬</th><th>3着馬</th>
  </tr></thead>
  <tbody>
    <tr><td>2026/03/01</td><td>1阪神2</td><td>晴</td><td>11</td><td>阪急杯(GIII)</td><td>動画</td>
        <td>芝1400</td><td>16</td><td>良</td><td>1:19.8</td><td>34.5-35.3</td>
        <td>ウマA</td><td>川田将雅</td><td>杉山晴紀</td><td>ウマB</td><td>ウマC</td></tr>
    <tr><td>2025/12/14</td><td>5阪神4</td><td>曇</td><td>9</td><td>3歳以上1勝クラス</td><td>動画</td>
        <td>芝1400</td><td>14</td><td>稍</td><td>1:21.3</td><td>35.1-36.0</td>
        <td>ウマD</td><td>武豊</td><td>友道康夫</td><td>ウマE</td><td>ウマF</td></tr>
  </tbody>
</table>
"""


def test_parse_real_search_result_every_row_is_a_winning_time():
    """実際の結果表は1行＝1レース。着順の列が無くても全行を勝ちタイムとして取る。"""
    records = bbt.parse_race_search_html(REAL_SEARCH_HTML)

    assert len(records) == 2
    first = records[0]
    assert first["date"] == "2026-03-01"
    assert first["venue"] == "阪神"            # "1阪神2" から場名だけ
    assert first["surface"] == "芝" and first["dist"] == 1400
    assert first["going"] == "良"
    assert first["class"] == "g3"              # "(GIII)"
    assert first["heads"] == 16
    assert first["win_time"] == pytest.approx(79.8)

    assert records[1]["class"] == "1win"       # "3歳以上1勝クラス"
    assert records[1]["going"] == "稍重"       # "稍" を正規化


def test_real_search_result_feeds_build_table():
    """この形のまま build_table に渡せること（勝ちタイムがOP水準に正規化される）。"""
    records = bbt.parse_race_search_html(REAL_SEARCH_HTML)
    table, _ = bbt.build_table(records, CLASS_OFFSET, min_samples=2)

    # g3: 79.8 − (−0.3)×0.7 = 80.01 / 1win: 81.3 − 1.5×0.7 = 80.25 → 中央値 80.1
    assert table["阪神"]["芝"]["1400"] == pytest.approx(80.1, abs=0.05)


def test_search_params_match_the_real_form():
    """`race/search_detail.html` の実サンプルから採取したフィールド名・コードを固定する。"""
    params = bbt.build_search_params("東京", "芝", 1600, 2023, 2026)

    assert params["jyo[]"] == "05"                       # 05=東京
    assert params["track[]"] == "1"                      # 1=芝
    assert params["kf"] == params["kt"] == "1600"        # 距離の下限=上限
    assert (params["yf"], params["mf"]) == ("2023", "1")
    assert (params["yt"], params["mt"]) == ("2026", "12")
    assert params["limit"] == "100"                      # フォームの選択肢は 20/50/100
    assert params["sort"] == "date-desc"

    # クラスは絞らない（§4「全クラスの勝ちタイムを使う」）
    assert "class[]" not in params


def test_search_params_use_jra_codes_for_every_venue():
    for venue, code in bbt.VENUE_CODE.items():
        assert code in {f"{n:02d}" for n in range(1, 11)}, venue
    assert bbt.VENUE_CODE["札幌"] == "01" and bbt.VENUE_CODE["小倉"] == "10"
    assert bbt.TRACK_CODE == {"芝": "1", "ダ": "2"}       # 3=障害は使わない


def test_fetch_course_records_stops_when_a_page_adds_nothing_new(monkeypatch):
    """ページ送りの `page` が効かなかった場合に、同じ1ページ目を積み続けないこと。"""
    calls = []

    class _Resp:
        text = SEARCH_HTML
        encoding = None
        apparent_encoding = "EUC-JP"

    def fake_get(url, params=None, **kwargs):
        calls.append(params["page"])
        return _Resp()  # pageを無視して常に同じ内容を返すサーバーの模擬

    monkeypatch.setattr(bbt, "http_get", fake_get)
    records = bbt.fetch_course_records("東京", "芝", 1600, 2023, 2026, max_pages=10)

    assert calls == ["1", "2"]      # 2ページ目で新規0件と分かって打ち切る
    assert len(records) == 2        # 1ページ目の勝ち馬2件だけ（重複は積まれない）


def test_end_to_end_from_raw_produces_loadable_table(tmp_path):
    """collect_from_raw → build_table が speed_index.lookup_base_time で引ける形を作る。"""
    from logic import speed_index

    runs = [{"date": f"2026-05-{day:02d}", "venue": "東京", "surface": "芝", "dist": 1600,
             "going": "良", "class": "op", "finish": 1, "time_sec": 93.0 + day * 0.1}
            for day in range(1, 6)]
    raw = {"races": [{"horses": [{"past_runs": runs}]}]}
    (tmp_path / "2026-W19.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    table, _ = bbt.build_table(bbt.collect_from_raw(tmp_path), CLASS_OFFSET, min_samples=5)

    assert speed_index.lookup_base_time(table, "東京", "芝", 1600) == pytest.approx(93.3)


# --- 少しずつ埋める運用ラッパー（scripts/fill_base_times.py）-------------

from scripts import fill_base_times as fbt


def test_pending_skips_courses_already_in_the_table():
    table = {"阪神": {"ダ": {"1400": 82.5, "1800": 108.0}}}
    pending = fbt.pending_courses(table, ["阪神"], limit=10)

    keys = {(v, s, d) for v, s, d in pending}
    assert ("阪神", "ダ", 1400) not in keys      # 既にあるものは出ない
    assert ("阪神", "ダ", 1800) not in keys
    assert ("阪神", "ダ", 1200) in keys
    assert all(v == "阪神" for v, _, _ in pending)


def test_pending_respects_the_limit():
    assert len(fbt.pending_courses({}, None, limit=6)) == 6
    assert len(fbt.pending_courses({}, ["小倉"], limit=100)) == 7   # 小倉は芝4+ダ3


def test_merge_keeps_other_courses():
    """追記であって置き換えではない（前回までに埋めたコースを消さない）。"""
    base = {"阪神": {"ダ": {"1400": 82.5}}, "中山": {"芝": {"2000": 120.0}}}
    merged = fbt.merge_table(base, {"阪神": {"ダ": {"1800": 108.0}, "芝": {"1600": 93.0}}})

    assert merged["阪神"]["ダ"] == {"1400": 82.5, "1800": 108.0}
    assert merged["阪神"]["芝"] == {"1600": 93.0}
    assert merged["中山"]["芝"] == {"2000": 120.0}      # 触られていない


def test_demand_orders_the_courses_to_fetch(tmp_path, monkeypatch):
    """必要とされている走数の多い順に取る（表の並び順で埋めると噛み合わないことがある）。"""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    runs = ([{"venue": "中山", "surface": "ダ", "dist": 1800}] * 18
            + [{"venue": "京都", "surface": "ダ", "dist": 1400}] * 14
            + [{"venue": "中山", "surface": "芝", "dist": 1200}] * 1)
    (raw_dir / "2026-W38.json").write_text(json.dumps(
        {"races": [{"entries": [{"past_runs": runs}]}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bbt, "RAW_DIR", raw_dir)

    demand = fbt.demand_from_raw()
    assert demand[("中山", "ダ", 1800)] == 18
    assert demand[("中山", "芝", 1200)] == 1

    targets = fbt.pending_courses({}, None, limit=2, demand=demand)
    assert targets == [("中山", "ダ", 1800), ("京都", "ダ", 1400)]

    # --ignore-demand 相当（demand を渡さない）では表の並び順のまま
    assert fbt.pending_courses({}, None, limit=1)[0][0] == "札幌"


def test_demand_is_empty_without_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(bbt, "RAW_DIR", tmp_path / "nope")
    assert fbt.demand_from_raw() == {}
