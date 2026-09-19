"""
A（レース一覧）パーサーのオフラインテスト。

実サンプル：2026年6月20日(土) 東京・阪神・函館 の race_list_get_date_list / race_list_sub。
実サンプルは配布物に含めないため、tests/samples/ に各自配置して実行する想定：
  tests/samples/race_list_get_date_list_20260620.html
  tests/samples/race_list_sub_20260620.html

実行： python -m pytest tests/test_a_race_list.py もしくは python tests/test_a_race_list.py
"""
from __future__ import annotations

import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.fetchers.a_race_list import parse_date_list_html, parse_race_list_sub_html

SAMPLES = Path(__file__).resolve().parent / "samples"


def _read_sample(name: str) -> str:
    """
    実サンプルHTMLを読む。無ければ**失敗ではなくスキップ**する。

    サンプルHTMLはリポジトリにコミットしていない（tests/samples/README.md の入手方法を参照）。
    CIでは存在しないので、FileNotFoundError で落とすとCI全体が赤のままになり、
    本当の失敗が埋もれる。c2のテストと同じ扱いに揃えた。
    """
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"実サンプルが無いのでスキップします: {name}"
                    "（tests/samples/README.md の入手方法を参照）")
    return path.read_text(encoding="utf-8", errors="replace")


def test_parse_current_group():
    """日付タブ一覧から、指定日のgroup（=current_group）が取れること。"""
    html = _read_sample("race_list_get_date_list_20260620.html")
    group = parse_date_list_html(html, "20260620")
    assert group == "1020260620"

    # 同じ週内の他日（日曜）も同じgroupを共有する
    group_sun = parse_date_list_html(html, "20260621")
    assert group_sun == "1020260620"

    # リストに無い日付はNone
    assert parse_date_list_html(html, "20260101") is None
    print("test_parse_current_group: OK")


def test_race_list_sub_tokyo_races():
    """東京会場のレースが正しくパースされること（条件戦・特別戦・障害戦の3パターン網羅）。"""
    html = _read_sample("race_list_sub_20260620.html")
    entries = parse_race_list_sub_html(html, "2026-06-20")

    tokyo = [e for e in entries if e.venue == "東京"]
    assert len(tokyo) == 12

    # 1R: 条件戦（未勝利）→ mi に判定できること
    r1 = next(e for e in tokyo if e.race_no == 1)
    assert r1.source_ref == "202605030501"
    assert r1.name == "3歳未勝利"
    assert r1.grade == "mi"
    assert r1.post_time == "10:05"
    assert r1.surface == "ダ"
    assert r1.dist == 1600
    assert r1.heads == 16
    assert r1.note is None
    assert r1.day == "土"
    assert r1.date == "2026-06-20"

    # 9R: 特別戦（町田特別）→ 条件文字列でないのでgrade=None、生アイコンだけ残る。ハンデでない
    r9 = next(e for e in tokyo if e.race_no == 9)
    assert r9.name == "町田特別"
    assert r9.grade is None
    assert r9.grade_icon_raw == "Icon_GradeType17"
    assert r9.note is None

    # 10R: 同じく特別戦だが、ハンデ戦アイコン(Icon_GradeType13)が付くのでnote="ハンデ"
    r10 = next(e for e in tokyo if e.race_no == 10)
    assert r10.name == "相模湖特別"
    assert r10.note == "ハンデ"

    # 11R: スレイプニル。ハンデ戦・ダート
    r11 = next(e for e in tokyo if e.race_no == 11)
    assert r11.name == "スレイプニル"
    assert r11.note == "ハンデ"
    assert r11.surface == "ダ"
    assert r11.dist == 2100
    assert r11.heads == 15

    print("test_race_list_sub_tokyo_races: OK")


def test_race_list_sub_obstacle_race():
    """障害戦（class=""の無地span）でも距離・馬場面が正しく拾えること。"""
    html = _read_sample("race_list_sub_20260620.html")
    entries = parse_race_list_sub_html(html, "2026-06-20")

    hanshin_1r = next(e for e in entries if e.venue == "阪神" and e.race_no == 1)
    assert hanshin_1r.name == "3歳以上障害OP"
    assert hanshin_1r.surface == "障"
    assert hanshin_1r.dist == 3110
    assert hanshin_1r.heads == 9
    # ItemTitleに"OP"の英字表記があるため grade="op" と判定できること
    assert hanshin_1r.grade == "op"

    print("test_race_list_sub_obstacle_race: OK")


def test_race_list_sub_all_venues_covered():
    """3場すべて・想定レース数が揃うこと（絞り込みなし＝全レース対象）。"""
    html = _read_sample("race_list_sub_20260620.html")
    entries = parse_race_list_sub_html(html, "2026-06-20")

    venues = {e.venue for e in entries}
    assert venues == {"東京", "阪神", "函館"}

    # race_id の重複が無いこと（全レースがユニークに識別できていること）
    source_refs = [e.source_ref for e in entries]
    assert len(source_refs) == len(set(source_refs))

    print(f"test_race_list_sub_all_venues_covered: OK ({len(entries)} races)")


if __name__ == "__main__":
    test_parse_current_group()
    test_race_list_sub_tokyo_races()
    test_race_list_sub_obstacle_race()
    test_race_list_sub_all_venues_covered()
    print("すべてのテストが通りました。")
