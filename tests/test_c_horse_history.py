"""
C（馬戦績）パーサーのオフラインテスト。

実サンプルは配布物に含めないため、tests/samples/ に各自配置して実行する想定：
  tests/samples/horse_valkyrie.html  （ヴァルキリーバース db.netkeiba.com/horse/2022104764）

実行： python -m pytest tests/test_c_horse_history.py  もしくは  python tests/test_c_horse_history.py
"""
from __future__ import annotations

import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.fetchers.c_horse_history import parse_horse_history_html

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


def test_valkyrie_past_runs():
    """直近5走が新しい順に、型どおり（取得項目仕様§2.5）取れること。"""
    html = _read_sample("horse_valkyrie.html")
    runs = parse_horse_history_html(html, n_runs=5)

    assert len(runs) == 5

    # 最新走：2026/06/21 府中牝馬S(GIII) 東京 芝1800 稍重 16頭 14着
    latest = runs[0]
    assert latest["date"] == "2026-06-21"
    assert latest["venue"] == "東京"
    assert latest["surface"] == "芝"
    assert latest["dist"] == 1800
    assert latest["going"] == "稍重"  # "稍"が正規化されること
    assert latest["class"] == "g3"   # "(GIII)"→g3
    assert latest["heads"] == 16
    assert latest["finish"] == 14
    assert latest["time_sec"] == 108.0  # "1:48.0" → 108.0秒
    assert latest["margin_sec"] == 2.5
    assert latest["last3f"] == 36.6
    assert latest["impost"] == 55.5
    assert latest["jockey_name"] == "ルメール"
    assert latest["note"] is None

    # 2走前：東風S(L) → Listedはopに丸める（要確認事項として引き継ぎ済み）
    assert runs[1]["class"] == "op"

    # フリージア賞(1勝クラス) → 1win
    assert runs[3]["class"] == "1win"

    # 1着馬の着差は0に上書きされること（netkeiba生値の表記ゆれを吸収、共通内部フォーマット仕様§2.5）
    win_rows = [r for r in runs if r["finish"] == 1]
    assert win_rows, "このサンプルには1着走が含まれるはず"
    for r in win_rows:
        assert r["margin_sec"] == 0.0

    print("test_valkyrie_past_runs: OK")


def test_shinba_class_mapped_to_mi():
    """6走目（新馬戦）は未勝利と同じ mi に丸められること。n_runs=6で確認。"""
    html = _read_sample("horse_valkyrie.html")
    runs = parse_horse_history_html(html, n_runs=6)
    assert len(runs) == 6
    assert runs[4]["class"] == "mi"  # "2歳未勝利"
    assert runs[5]["class"] == "mi"  # "2歳新馬"
    print("test_shinba_class_mapped_to_mi: OK")


if __name__ == "__main__":
    test_valkyrie_past_runs()
    test_shinba_class_mapped_to_mi()
    print("すべてのテストが通りました。")
