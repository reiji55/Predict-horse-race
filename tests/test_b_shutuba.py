"""
B（出馬表）・B2（オッズAPI）パーサーのオフラインテスト。

実サンプルHTML/ APIレスポンスに対してパース結果を検証する。
実サンプルは配布物に含めないため、tests/samples/ に各自で配置して実行する想定：
  tests/samples/shutuba_fuchu.html         （府中牝馬S 出馬表・枠順確定後）
  tests/samples/shutuba_tanabata.html       （七夕賞 出馬表・枠順未確定）
  tests/samples/odds_fuchu.txt              （府中牝馬S オッズAPIレスポンス）

実行： python -m pytest tests/test_b_shutuba.py  もしくは  python tests/test_b_shutuba.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.fetchers.b_shutuba import parse_shutuba_html
from scraper.fetchers.b2_odds import (
    extract_win_place_odds,
    merge_odds_into_race,
    parse_odds_response,
)

SAMPLES = Path(__file__).resolve().parent / "samples"


def _require(*names: str):
    """
    必要な実サンプルが tests/samples/ に無ければ、落とさずスキップする。
    サンプルはnetkeibaの著作物でリポジトリに含めていないため、
    「手元にある分だけテストが走る」ようにしておく（配置手順は tests/samples/README.md）。
    """
    missing = [n for n in names if not (SAMPLES / n).exists()]
    if missing:
        print(f"  … スキップ（tests/samples/ に {', '.join(missing)} が必要）")
        return None
    return [(SAMPLES / n).read_text(encoding="utf-8", errors="replace") for n in names]


def test_shutuba_confirmed():
    """枠順確定後の出馬表：枠番・馬番・馬体重・馬場状態が取れること。"""
    files = _require("shutuba_fuchu.html")
    if files is None:
        return
    race = parse_shutuba_html(files[0], "202605030611")

    assert race["id"] == "20260621-tokyo-11"
    assert race["venue"] == "東京"
    assert race["grade"] == "g3"
    assert race["course"]["surface"] == "芝"
    assert race["course"]["dist"] == 1800
    assert race["going"] == "稍重"  # 正規化された馬場状態
    assert race["weather"] == "曇"

    nums = [e["num"] for e in race["entries"]]
    assert nums == list(range(1, 17))  # 1..16 が過不足なく
    assert race["entries"][0]["waku"] == 1
    assert race["entries"][0]["body_weight"] is not None

    print("test_shutuba_confirmed: OK")


def test_shutuba_unconfirmed():
    """枠順未確定の出馬表：馬番はtr idから取れ、枠番/馬場はNoneでも落ちないこと。"""
    files = _require("shutuba_tanabata.html")
    if files is None:
        return
    race = parse_shutuba_html(files[0], "202603020611")

    assert race["id"] == "20260712-fukushima-11"
    nums = sorted(e["num"] for e in race["entries"])
    assert nums == list(range(1, len(nums) + 1))  # 連番で重複なし
    assert all(e["waku"] is None for e in race["entries"])  # 枠番未確定
    assert race["going"] is None  # 馬場状態はまだ無い

    print("test_shutuba_unconfirmed: OK")


def test_odds_decode_and_merge():
    """オッズAPI：jsonp剥がし→base64+zlib解凍→馬番マップ→出馬表への統合。"""
    files = _require("odds_fuchu.txt", "shutuba_fuchu.html")
    if files is None:
        return
    text, shutuba_html = files
    body = parse_odds_response(text)
    assert body["official_datetime"] is not None

    by_num = extract_win_place_odds(body)
    assert len(by_num) == 16
    # 1番人気の馬の単勝が最小であること（サンプルでは6番3.3倍）
    assert by_num[6]["win_odds"] == 3.3
    assert by_num[6]["popularity"] == 1
    assert by_num[6]["place_odds"] == [1.4, 1.7]

    # 出馬表に統合すると win_odds の欠損が消えること
    race = parse_shutuba_html(shutuba_html, "202605030611")
    merge_odds_into_race(race, {"official_datetime": body["official_datetime"], "by_num": by_num})
    assert all(e["win_odds"] is not None for e in race["entries"])
    assert race["odds_updated_at"] == body["official_datetime"]

    print("test_odds_decode_and_merge: OK")


# --- 負担重量条件（2026-09-21 の修正）----------------------------------------

def test_weight_rule_recognises_all_four_jra_conditions():
    """
    JRAの負担重量条件は ハンデ / 別定 / 定量 / 馬齢 の4つ。
    以前は「ハンデ」だけを見ていたので、別定戦の答えが None（＝不明）になり、
    Aページのアイコン推定に上書きされて**全レースがハンデ表示**になっていた。
    """
    from scraper.fetchers.b_shutuba import _parse_weight_rule

    assert _parse_weight_rule("3回 東京 6日目 サラ系３歳以上 オープン (国際) 牝(特指) ハンデ 16頭") == "ハンデ"
    assert _parse_weight_rule("4回 中山 7日目 サラ系3歳以上 オープン (国際) 別定 13頭") == "別定"
    assert _parse_weight_rule("5回 阪神 2日目 サラ系3歳以上 3勝クラス (混合) 定量 16頭") == "定量"
    assert _parse_weight_rule("1回 福島 3日目 サラ系2歳 新馬 馬齢 12頭") == "馬齢"
    # 判らないときは推測しない（取得項目仕様§2.0）
    assert _parse_weight_rule("4回 中山 7日目 サラ系3歳以上 オープン 13頭") is None
    assert _parse_weight_rule("") is None


def test_the_real_sample_is_a_handicap():
    """実サンプル（府中牝馬S）はハンデ戦。RaceData02 に独立したspanで載っている。"""
    files = _require("shutuba_fuchu.html")
    if files is None:
        return
    race = parse_shutuba_html(files[0], "202605030611")
    assert race["course"]["note"] == "ハンデ"


if __name__ == "__main__":
    test_shutuba_confirmed()
    test_shutuba_unconfirmed()
    test_odds_decode_and_merge()
    test_weight_rule_recognises_all_four_jra_conditions()
    test_the_real_sample_is_a_handicap()
    print("すべてのテストが通りました。")
