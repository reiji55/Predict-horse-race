"""
調教ページ（無料で見られる評価と記号）の読み取りと、過去レースの取り込みの完了判定のテスト。
ネットワークには出ない。HTMLは自作（構造だけ、馬名は架空）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.fetchers import training  # noqa: E402

HTML = """<html><body><table class="race_table_01 nk_tb_common OikiriTable OikiriType2">
<tr><th>枠</th><th>馬番</th><th>印</th><th>馬名</th><th>評価</th><th>映像</th></tr>
<tr><td class="Waku1">1</td><td class="Umaban">1</td><td class="CheckMark">-</td>
    <td class="Horse_Info"><a href="https://db.netkeiba.com/horse/2021100001">テスト馬A</a></td>
    <td class="Training_Critic">動き軽快</td><td class="Rank_A">A</td></tr>
<tr><td class="Waku2">2</td><td class="Umaban">2</td><td class="CheckMark">-</td>
    <td class="Horse_Info"><a href="https://db.netkeiba.com/horse/2021100002">テスト馬B</a></td>
    <td class="Training_Critic">平凡</td><td class="Rank_C">C</td></tr>
<tr><td class="Waku3">3</td><td class="Umaban">3</td><td class="CheckMark">-</td>
    <td class="Horse_Info"><a href="https://db.netkeiba.com/horse_x/2021100003">テスト馬C</a></td>
    <td class="Training_Critic">上々</td><td class="Rank_B">B</td></tr>
</table></body></html>"""


def test_parse_reads_grade_and_critic_and_counts_rows():
    by_horse, n_rows = training.parse_oikiri_html(HTML)
    assert n_rows == 3 and set(by_horse) == {"2021100001", "2021100002"}     # 3頭目は馬IDが読めない
    assert by_horse["2021100001"] == {"critic": "動き軽快", "grade": "A"}
    s = training.summarize(by_horse, n_rows)
    assert s["grades"] == {"A": 1, "C": 1} and s["n_rows"] == 3 and "テスト馬" not in str(s)


def test_no_table():
    assert training.parse_oikiri_html("<html><title>x</title></html>") == ({}, 0)


def test_classify_does_not_call_skipped_rows_done():
    from scripts import backfill_training_races as bf
    by_horse, n_rows = training.parse_oikiri_html(HTML)
    assert bf.classify(by_horse, n_rows) == ("runner_id_missing", 3, 2)
    assert bf.classify({}, 0) == ("no_table", 0, 0)
    assert bf.classify({"a": {"grade": "A", "critic": "x"}}, 1) == ("done", 1, 1)
    assert bf.classify({"a": {"grade": None, "critic": None}}, 1) == ("no_eval", 1, 0)
    assert bf.next_status("partial", 3, 1) == "partial_final" and bf.next_status("no_table", 3, 0) == "unavailable"


def test_backfill_records_status_and_retries():
    from scripts import backfill_training_races as bf
    pages = {"r1": HTML.replace("horse_x", "horse"), "r2": "<html></html>"}
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "ids.txt").write_text("r1\nr2", encoding="utf-8")
        orig = training.fetch_oikiri_html
        training.fetch_oikiri_html = lambda rid: pages[rid]
        try:
            assert bf.main(["--race-ids", str(d / "ids.txt"), "--store", str(d / "e.csv"),
                            "--status", str(d / "s.csv")]) == 0
        finally:
            training.fetch_oikiri_html = orig
        st = bf._load(d / "s.csv", lambda r: r["race_id"])
        assert st["r1"]["status"] == "done" and st["r2"]["status"] == "retry" and st["r2"]["note"] == "no_table"
        assert len(bf._load(d / "e.csv", lambda r: (r["race_id"], r["horse_ref"]))) == 3
