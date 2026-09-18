"""
D（騎手リーディング）・E（調教師リーディング）・F（レース結果・払戻）のパーサーのテスト。

**ネットワークには一切アクセスしない**。`tests/fixtures/` に置いた、実サンプルの構造を
再現したフィクスチャに対してパース結果を検証する
（netkeibaの実HTMLそのものは著作物なのでリポジトリには含めない）。

実行： python3 tests/test_fetchers_def.py  もしくは  python -m pytest tests/test_fetchers_def.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from results import build_results
from scraper.common import leading_api
from scraper.fetchers import f_results

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --- D / E：リーディングAPI ---------------------------------------------

def _leading_fixture() -> str:
    return (FIXTURES / "leading_jockey.sample.json").read_text(encoding="utf-8")


def test_unwrap_json_string_response():
    """output=json は「HTML断片をJSON文字列で包んだもの」なので、デコードしてから使う。"""
    html = leading_api.unwrap_response(_leading_fixture())
    assert html.lstrip().startswith("<li>")
    assert "\\n" not in html          # エスケープが解けていること
    assert "01174" in html
    print("test_unwrap_json_string_response: OK")


def test_unwrap_jsonp_response():
    """output=jsonp（callback で包まれた形）でも剥がせること。"""
    html = leading_api.unwrap_response('jQuery111("<li>\\n<p>104\\u52dd</p></li>");')
    assert html == "<li>\n<p>104勝</p></li>"
    assert leading_api.unwrap_response("<li>生HTML</li>") == "<li>生HTML</li>"
    assert leading_api.unwrap_response("") == ""
    print("test_unwrap_jsonp_response: OK")


def test_parse_leading_extracts_refs_and_counts():
    """騎手ID・名前・所属が取れ、率から着度数が逆算されること。"""
    stats = leading_api.parse_leading_html(_leading_fixture(), "jockey", "2026")

    assert set(stats) == {"01174", "05339", "01088"}   # 5桁ゼロ埋め（B出馬表と同形式）

    lemaire = stats["05339"]
    assert lemaire["name"] == "Ｃ．ルメール"
    assert lemaire["barn"] == "栗東"
    assert lemaire["scope"] == "overall" and lemaire["period"] == "2026"
    assert lemaire["wins"] == 103
    assert lemaire["starts"] == 372          # 103 / 27.7%
    assert lemaire["seconds"] == 63          # 372 × 44.6% − 103
    assert lemaire["thirds"] is None         # 複勝率は sort_key=win のレスポンスに載らない
    print("test_parse_leading_extracts_refs_and_counts: OK")


def test_derived_counts_round_trip():
    """逆算した着度数から率を計算し直すと、元の率にほぼ戻ること（丸め誤差1%未満）。"""
    for ref, expected_win_rate, expected_place2 in (("01174", 18.1, 31.2), ("05339", 27.7, 44.6)):
        s = leading_api.parse_leading_html(_leading_fixture(), "jockey", "2026")[ref]
        win_rate = s["wins"] / s["starts"] * 100
        place2 = (s["wins"] + s["seconds"]) / s["starts"] * 100
        assert abs(win_rate - expected_win_rate) < 0.2, (ref, win_rate)
        assert abs(place2 - expected_place2) < 0.2, (ref, place2)
    print("test_derived_counts_round_trip: OK")


def test_derive_counts_handles_missing_rates():
    """勝率が無ければ starts は出せない（Noneのまま）。複勝率があれば thirds も埋まる。"""
    assert leading_api._derive_counts(10, None, None, None)["starts"] is None

    full = leading_api._derive_counts(100, 20.0, 35.0, 50.0)   # starts=500
    assert full["starts"] == 500
    assert full["seconds"] == 75      # 500×35% − 100
    assert full["thirds"] == 75       # 500×50% − 100 − 75
    print("test_derive_counts_handles_missing_rates: OK")


def test_leading_api_params():
    """getLeadingData() が投げるクエリと同じ形になっていること。"""
    params = leading_api.build_params("trainer", "2026", page=2)
    assert params["pid"] == "api_get_leading"
    assert params["category"] == "trainer"
    assert params["show"] == "leading_detail"
    assert params["page"] == 2
    assert params["bel"] == ""        # 全国（所属での絞り込みはしない）
    print("test_leading_api_params: OK")


# --- F：レース結果・払戻 -------------------------------------------------

def _result_fixture() -> str:
    return (FIXTURES / "race_result.sample.html").read_text(encoding="utf-8")


def test_parse_finish_order():
    """着順テーブルから1着順の馬番が取れ、中止・除外の行は除かれること。"""
    result = f_results.parse_result_html(_result_fixture())
    assert result["finish"] == [4, 5, 8, 10]    # 5行目の「中止」は入らない
    print("test_parse_finish_order: OK")


def test_parse_dividends_shape():
    """公式配当表がデータスキーマ仕様§5 の形で取れること（使う3券種のみ）。"""
    dividends = f_results.parse_result_html(_result_fixture())["dividends"]

    assert set(dividends) == {"ワイド", "馬連", "3連複"}   # 単勝・複勝・枠連等は取らない
    assert dividends["馬連"] == [{"horses": [4, 5], "pay": 1730}]
    assert dividends["3連複"] == [{"horses": [4, 5, 8], "pay": 3630}]
    print("test_parse_dividends_shape: OK")


def test_wide_has_three_combinations():
    """ワイドは3組が当たるので、組み合わせと配当が正しく対応すること。"""
    wide = f_results.parse_result_html(_result_fixture())["dividends"]["ワイド"]
    assert wide == [
        {"horses": [4, 5], "pay": 610},
        {"horses": [4, 8], "pay": 580},
        {"horses": [5, 8], "pay": 710},
    ]
    print("test_wide_has_three_combinations: OK")


def test_result_feeds_build_results_directly():
    """Fの戻り値をそのまま成績集計に渡せること（タスク7との接続確認）。"""
    race_result = f_results.parse_result_html(_result_fixture())

    predictions = {
        "generated_at": "2026-09-13T07:30:00+09:00", "week_id": "2026-W37",
        "myomi_threshold": 80,
        "races": [{
            "id": "20260913-nakayama-01",
            "marks": [{"mk": "◎", "num": 4}, {"mk": "○", "num": 5}, {"mk": "▲", "num": 8}],
            "cards": [{"char": "kei", "total": 500, "bets": [
                {"type": "ワイド", "horses": [4, 5], "amt": 200},
                {"type": "ワイド", "horses": [4, 8], "amt": 100},
                {"type": "馬連", "horses": [4, 9], "amt": 200},      # 外れ
            ]}],
        }],
    }

    results = build_results.build_results(
        predictions, {"20260913-nakayama-01": race_result})

    card = results["results"][0]["cards"][0]
    assert card["hit"] is True
    assert card["spent"] == 500
    # ワイド4-5: 610×200/100=1220 / ワイド4-8: 580×100/100=580 / 馬連4-9: 外れ
    assert card["payout"] == 1220 + 580
    assert results["results"][0]["finish"] == [4, 5, 8, 10]
    print("test_result_feeds_build_results_directly: OK（払戻 %d円）" % card["payout"])


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
    print(f"\nすべてのテストが通りました（{len(ALL_TESTS)}件）。")
