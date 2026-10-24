"""
JRA馬場データ（含水率・クッション値）の過去分の読み取りと、調教ページの確認のテスト。
ネットワークには出ない。PDFの文字列・HTMLは自作（数字の並びだけを再現）。

実行： python -m pytest tests/test_jra_track_archive.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import backfill_jra_track_archive as jra  # noqa: E402
from scripts import check_training_page as tc  # noqa: E402

# 2020年9月以降の形（クッション値あり）。見出しは2行に割れ、日次の無い行（金曜の測定）もある
WITH_CUSHION = """２０２５年 第１回 中山競馬 クッション値・含水率一覧
ゴール前 4コーナー ゴール前 4コーナー
 1月 4日 土曜日 B 10:30 10.1 10:30 13.4 13.7 2.1 2.1
第 1日 1月 5日 日曜日 B 08:30 10.2 05:00 13.6 13.6 2.0 1.6
第 2日 1月 6日 月曜日 B 08:00 10.1 06:00 13.7 14.2 1.7 1.5
"""
# それより前の形（含水率だけ）。ダートを測っていない日は「－」
MOISTURE_ONLY = """2019年 5回阪神競馬
第 1日 11月30日 土曜日 A 06:00 11.2 12.0 4.1 4.5
第 2日 12月 1日 日曜日 A 06:00 10.8 11.5 － －
"""


def test_rows_with_cushion():
    rows = jra.parse_pdf_text(WITH_CUSHION, 2025, "中山", 1, "x.pdf")
    assert len(rows) == 3
    r = rows[0]
    assert (r["date"], r["day_no"], r["weekday"], r["course"]) == ("2025-01-04", "", "土", "B")
    assert (r["cushion_time"], r["cushion"], r["moisture_time"]) == ("10:30", 10.1, "10:30")
    assert [r["turf_goal"], r["turf_turn4"], r["dirt_goal"], r["dirt_turn4"]] == [13.4, 13.7, 2.1, 2.1]
    assert rows[1]["day_no"] == "1" and rows[1]["cushion"] == 10.2 and rows[1]["moisture_time"] == "05:00"


def test_rows_without_cushion_and_missing_dirt():
    rows = jra.parse_pdf_text(MOISTURE_ONLY, 2019, "阪神", 5)
    assert len(rows) == 2
    assert rows[0]["cushion"] is None and rows[0]["moisture_time"] == "06:00"
    assert [rows[0]["turf_goal"], rows[0]["turf_turn4"], rows[0]["dirt_goal"], rows[0]["dirt_turn4"]] == [11.2, 12.0, 4.1, 4.5]
    assert rows[1]["date"] == "2019-12-01" and rows[1]["dirt_goal"] is None and rows[1]["turf_goal"] == 10.8


def test_unreadable_text_gives_no_rows():
    assert jra.parse_pdf_text("中身の無いページ", 2025, "東京", 1) == []


def test_pdf_links_pattern():
    html = '<a href="2025pdf/nakayama01.pdf">1回</a><a href="/keiba/baba/archive/2025pdf/tokyo05.pdf">5回</a>'
    got = [(m[2], m[3]) for m in jra.PDF_RE.findall(html)]
    assert got == [("nakayama", "01"), ("tokyo", "05")]


def test_training_summary_reports_structure_only():
    html = """<html><title>調教タイム | netkeiba</title><body>
    <table class="OikiriTable"><tr><th>馬名</th><th>日付</th><th>評価</th></tr>
    <tr><td class="Horse_Info">テスト馬</td><td class="Training_Day">10/22 美坂 52.1</td><td class="Critic">好調</td></tr>
    <tr><td class="Horse_Info">テスト馬2</td><td class="Training_Day"></td><td class="Critic Premium">プレミアム会員限定</td></tr>
    </table></body></html>"""
    s = tc.summarize(html)
    assert s["title_has_oikiri"] and s["n_rows"] == 2 and s["headers"] == ["馬名", "日付", "評価"]
    assert s["columns"][1]["has_time"] == 1 and s["columns"][1]["filled"] == 1
    assert s["columns"][2]["locked"] == 1 and "プレミアム" in s["lock_words"]
    assert "テスト馬" not in str(s)          # 中身（馬名など）は出さない
