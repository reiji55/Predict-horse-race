"""
天皇賞(春)(GI) / 天皇賞(秋)(GI) のクラス判定（本番の入力経路 scraper/fetchers/c_horse_history.py）。

以前の _parse_class は最初の括弧だけを見ていたため、"天皇賞(春)(GI)" では "春" を拾って後ろの "(GI)" を見ず、
class=None になっていた（speed の usable 判定で落ちる）。constants.normalize_class_label に揃えて直した。

- 実サンプル HTML はリポジトリに無いので、戦績テーブルの形だけを持つ最小の HTML で確かめる（CI で必ず走る）
- それ以外のレース名のクラス判定は変わらない
- Champion と speed-v2 Challenger は同じ raw（＝同じ修正済みの past_runs）から予想を作る
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

from logic import build_predictions as bp
from logic import model_registry, snapshots, speed_index
from scraper.common import constants
from scraper.fetchers import c_horse_history

ROOT = Path(__file__).resolve().parent.parent
SPEED_V2 = "speed-base-times-v2-coverage"


def _row(date: str, kaisai: str, racename: str, heads: int, finish: str, dist: str, going: str,
         time: str, margin: str) -> str:
    cells = [""] * 28
    cells[0], cells[1], cells[4], cells[6] = date, kaisai, racename, str(heads)
    cells[11], cells[12], cells[13], cells[14] = finish, "ルメール", "58.0", dist
    cells[16], cells[18], cells[19], cells[27] = going, time, margin, "33.9"
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


HISTORY_HTML = (
    '<html><body><table class="db_h_race_results"><thead><tr><th>日付</th></tr></thead><tbody>'
    + _row("2025/11/02", "4東京9", "天皇賞(秋)(GI)", 14, "3", "芝2000", "良", "1:58.9", "0.3")
    + _row("2025/05/04", "3京都4", "天皇賞(春)(GI)", 15, "1", "芝3200", "良", "3:14.0", "-0.2")
    + _row("2024/10/14", "4京都5", "京都大賞典(GII)", 12, "2", "芝2400", "稍", "2:24.1", "0.1")
    + "</tbody></table></body></html>"
)


def _tenno_runs() -> list[dict]:
    return c_horse_history.parse_horse_history_html(HISTORY_HTML, n_runs=5)


# ------------------------------------------------------------------ パーサ

@pytest.mark.parametrize("racename", ["天皇賞(春)(GI)", "天皇賞(秋)(GI)"])
def test_tenno_sho_is_g1(racename):
    assert c_horse_history._parse_class(racename) == "g1"


def test_tenno_sho_rows_parse_as_g1_from_history_table():
    runs = _tenno_runs()
    assert [(r["date"], r["venue"], r["surface"], r["dist"], r["class"]) for r in runs] == [
        ("2025-11-02", "東京", "芝", 2000, "g1"),
        ("2025-05-04", "京都", "芝", 3200, "g1"),
        ("2024-10-14", "京都", "芝", 2400, "g2"),
    ]
    assert runs[1]["finish"] == 1 and runs[1]["margin_sec"] == 0.0


@pytest.mark.parametrize("racename, expected", [
    # 以前と同じ結果になるもの（括弧1つのグレード・条件・括弧なし）
    ("府中牝馬S(GIII)", "g3"), ("ローズS(GII)", "g2"), ("有馬記念(GI)", "g1"), ("東風S(L)", "op"),
    ("朱鷺S(OP)", "op"), ("かしわ記念(JpnI)", "g1"), ("TCK女王盃(JpnIII)", "g3"),
    ("フリージア賞(1勝クラス)", "1win"), ("STV賞(3勝クラス)", "3win"), ("3歳以上2勝クラス", "2win"),
    ("3歳未勝利", "mi"), ("2歳新馬", "mi"), ("オープン", "op"),
    # 判定できないものは今までどおり None（障害の J・GI、無印の特別戦）
    ("中山大障害(J・GI)", None), ("萩S", None), ("アイビーS", None), ("", None),
])
def test_other_race_names_keep_their_class(racename, expected):
    assert c_horse_history._parse_class(racename) == expected
    assert constants.normalize_class_label(racename) == expected


# ------------------------------------------------------------------ Champion と speed-v2 Challenger は同じ修正済み入力を使う

def test_champion_and_speed_v2_challenger_get_the_same_fixed_past_runs(tmp_path: Path, monkeypatch):
    raw = json.loads((ROOT / "raw" / "2026-W39.json").read_text(encoding="utf-8"))
    entry = raw["races"][0]["entries"][0]
    entry["past_runs"] = _tenno_runs() + entry["past_runs"][:2]
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "2026-W39.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    seen: dict[str, list[dict]] = {}
    original = bp.build_predictions

    def spy(raw_arg, *args, model_spec=None, **kwargs):
        seen[model_spec["id"]] = copy.deepcopy(raw_arg["races"][0]["entries"][0]["past_runs"])
        return original(raw_arg, *args, model_spec=model_spec, **kwargs)

    monkeypatch.setattr(bp, "build_predictions", spy)
    monkeypatch.setattr(bp, "RAW_DIR", raw_dir)
    monkeypatch.setattr(bp, "OUTPUT_PATH", tmp_path / "predictions.json")
    monkeypatch.setattr(bp, "CHALLENGER_ROOT", tmp_path / "challengers")
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(sys, "argv", ["build_predictions", "--week", "2026-W39"])
    bp.main()

    registry = model_registry.load_registry()
    champion_id = registry["champion"]["id"]
    assert champion_id in seen and SPEED_V2 in seen
    assert seen[SPEED_V2] == seen[champion_id]                    # 全モデルが同じ過去走を受け取る
    assert all(runs == seen[champion_id] for runs in seen.values())
    assert [r["class"] for r in seen[champion_id][:2]] == ["g1", "g1"]
    assert (tmp_path / "challengers" / SPEED_V2 / "predictions.json").exists()


def test_fixed_class_reaches_the_speed_usable_check():
    """
    class だけが speed の usable 判定に効く。修正前（None）は表に関係なく落ち、修正後は表に基準タイムがあれば使える。
    基準タイムの有無は表ごとの違いのまま（Champion の v1 表には東京芝2000 が無い、v2 表には京都芝3200 が無い）。
    """
    config = speed_index.load_config()
    champion_table = speed_index.load_base_times()
    v2_spec = next(m for m in model_registry.load_registry()["challengers"] if m["id"] == SPEED_V2)
    v2_table, _ref = model_registry.load_model_base_times(v2_spec)
    autumn, spring, _ = _tenno_runs()

    for run in (autumn, spring):
        assert run["class"] in config["class_offset"]
        broken = {**run, "class": None}
        for table in (champion_table, v2_table):
            assert speed_index.is_usable(broken, config, table) is False
            has_base_time = speed_index.lookup_base_time(table, run["venue"], run["surface"], run["dist"]) is not None
            assert speed_index.is_usable(run, config, table) is has_base_time

    assert speed_index.is_usable(autumn, config, v2_table) is True      # 東京芝2000 は v2 表にある
    assert speed_index.is_usable(spring, config, v2_table) is False     # 京都芝3200 は n=4 で表に無い


def test_speed_v2_registration_is_after_this_input_fix():
    """
    この修正（2026-10-02 JST）で Challenger の入力が変わるので、forward の登録は修正より後へ進めてある。
    登録前のレースは採点しないため、修正前の入力で作った予想が forward 評価に混ざらない。
    """
    import datetime
    spec = next(m for m in model_registry.load_registry()["challengers"] if m["id"] == SPEED_V2)
    registered = datetime.datetime.fromisoformat(spec["registered_at"])
    assert registered >= datetime.datetime(2026, 10, 3, tzinfo=bp.JST)
