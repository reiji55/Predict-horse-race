"""
血統（父・母・母父）の取得と保存のテスト。

**ネットワークには一切アクセスしない**。`tests/fixtures/` の自作フィクスチャ（構造だけを再現、馬名は架空）で検証する。
C2 の Horse_Info の並び（Horse01=父 / Horse03=母 / Horse04=(母父)）は推定なので、
実サンプルが tests/samples/shutuba_past_hanshin11.html にあれば、その確認も行う（無ければスキップ）。

実行： python3 tests/test_pedigree.py  もしくは  python -m pytest tests/test_pedigree.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper.fetchers import c2_shutuba_past, pedigree

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLES = Path(__file__).resolve().parent / "samples"


def test_c2_reads_pedigree_from_horse_info():
    html = (FIXTURES / "shutuba_past_pedigree.html").read_text(encoding="utf-8")
    by_horse = c2_shutuba_past.parse_shutuba_past_html(html)
    ped = by_horse["2021100001"]["pedigree"]
    assert ped["sire"] == "テスト種牡馬A"
    assert ped["dam"] == "テスト繁殖牝馬A"
    assert ped["damsire"] == "テスト母父A"          # 括弧は外す
    assert by_horse["2021100002"]["pedigree"] is None   # 血統の div が無い馬は None（全体は止めない）
    assert by_horse["2021100001"]["popularity"] == 5    # 既存の読み取りは変わらない
    print("test_c2_reads_pedigree_from_horse_info: OK")


def test_merge_into_race_sets_pedigree_only_when_found():
    html = (FIXTURES / "shutuba_past_pedigree.html").read_text(encoding="utf-8")
    by_horse = c2_shutuba_past.parse_shutuba_past_html(html)
    race = {"entries": [{"horse_ref": {"netkeiba": "2021100001"}}, {"horse_ref": {"netkeiba": "2021100002"}}]}
    c2_shutuba_past.merge_into_race(race, by_horse)
    assert race["entries"][0]["pedigree"]["sire"] == "テスト種牡馬A"
    assert "pedigree" not in race["entries"][1]
    print("test_merge_into_race_sets_pedigree_only_when_found: OK")


def test_ped_page_three_generations():
    html = (FIXTURES / "pedigree_ped_page.html").read_text(encoding="utf-8")
    ped = pedigree.parse_ped_page_html(html)
    assert ped["sire"] == "テスト父" and ped["sire_ref"] == "0000000001"
    assert ped["dam"] == "テスト母" and ped["dam_ref"] == "0000000002"
    assert ped["damsire"] == "テスト母父 (米)" and ped["damsire_ref"] == "000a000021"
    print("test_ped_page_three_generations: OK")


def _five_generation_table() -> str:
    """5代表（父・母が rowspan=16）の並びを機械的に作る。"""
    rows, n = [], [0]

    def cell(span, label):
        n[0] += 1
        return f'<td rowspan="{span}"><a href="https://db.netkeiba.com/horse/{n[0]:010d}/">{label}</a></td>'

    def build(prefix, gen, row_cells):
        span = 2 ** (5 - gen)
        for side, name in (("父", "sire"), ("母", "dam")):
            label = prefix + side
            row_cells.append(cell(span if span > 1 else 1, label))
            if gen < 5:
                build(label, gen + 1, row_cells)
            else:
                rows.append("<tr>" + "".join(row_cells) + "</tr>")
            row_cells = []
    build("", 1, [])
    return '<table class="blood_table">' + "".join(rows) + "</table>"


def test_ped_page_five_generations():
    ped = pedigree.parse_ped_page_html(_five_generation_table())
    assert ped["sire"] == "父"
    assert ped["dam"] == "母"
    assert ped["damsire"] == "母父"
    print("test_ped_page_five_generations: OK")


def test_ped_page_without_table_returns_none():
    assert pedigree.parse_ped_page_html("<html><title>別ページ</title></html>") is None
    print("test_ped_page_without_table_returns_none: OK")


def test_store_appends_without_overwriting():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "pedigree.csv"
        races = [{"entries": [
            {"horse_ref": {"netkeiba": "2021100001"}, "pedigree": {"sire": "A", "dam": "B", "damsire": "C"}},
            {"horse_ref": {"netkeiba": "2021100002"}},                       # 血統なし → 追加しない
        ]}]
        assert pedigree.update_store_from_races(races, path) == 1
        races[0]["entries"][0]["pedigree"] = {"sire": "書き換え", "dam": "B", "damsire": "C"}
        assert pedigree.update_store_from_races(races, path) == 0         # 既存の馬は上書きしない
        store = pedigree.load_store(path)
        assert store["2021100001"]["sire"] == "A"
        assert store["2021100001"]["source"] == "shutuba_past"
    print("test_store_appends_without_overwriting: OK")


def test_real_sample_if_present():
    """実サンプルがあれば、推定した Horse_Info の並びで父・母・母父が取れるかを確かめる（無ければスキップ）。"""
    sample = SAMPLES / "shutuba_past_hanshin11.html"
    if not sample.exists():
        print("test_real_sample_if_present: SKIP（tests/samples/shutuba_past_hanshin11.html が無い）")
        return
    by_horse = c2_shutuba_past.parse_shutuba_past_html(sample.read_text(encoding="utf-8", errors="replace"))
    peds = [v["pedigree"] for v in by_horse.values()]
    got = sum(1 for p in peds if p and p.get("sire") and p.get("damsire"))
    assert got >= len(peds) * 0.8, f"父・母父が取れたのは {got}/{len(peds)} 頭。Horse_Info の並びが推定と違う可能性"
    print(f"test_real_sample_if_present: OK（{got}/{len(peds)} 頭）")


def _run_backfill_races(fake_fetch, n_races: int):
    """ネットに出ずに backfill_pedigree_races.main を回す（fetch_shutuba_past を差し替える）。"""
    from scripts import backfill_pedigree_races as bf
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ids = d / "ids.txt"
        ids.write_text("\n".join(f"2026050402{i:02d}" for i in range(1, n_races + 1)), encoding="utf-8")
        orig = c2_shutuba_past.fetch_shutuba_past
        c2_shutuba_past.fetch_shutuba_past = fake_fetch
        try:
            code = bf.main(["--race-ids", str(ids), "--limit", "50",
                            "--store", str(d / "pedigree.csv"), "--done", str(d / "done.txt")])
        finally:
            c2_shutuba_past.fetch_shutuba_past = orig
        done = (d / "done.txt").read_text(encoding="utf-8").split() if (d / "done.txt").exists() else []
        return code, pedigree.load_store(d / "pedigree.csv"), done


def test_backfill_races_collects_and_records_done():
    def fake(race_id):
        ref = "20211" + race_id[-5:]
        return {ref: {"pedigree": {"sire": "父" + race_id[-2:], "dam": "母", "damsire": "母父"}}}
    code, store, done = _run_backfill_races(fake, 8)
    assert code == 0
    assert len(store) == 8 and len(done) == 8
    print("test_backfill_races_collects_and_records_done: OK")


def test_backfill_races_stops_early_without_marking_done():
    calls = []

    def fake(race_id):
        calls.append(race_id)
        return {"2021100001": {"pedigree": None}}       # 並びの推定違い＝血統が取れない
    code, store, done = _run_backfill_races(fake, 12)
    assert code == 1
    assert len(calls) == 5                               # 6レース目以降は取りに行かない
    assert store == {} and done == []                    # 直したあと取り直せるよう「済み」にしない
    print("test_backfill_races_stops_early_without_marking_done: OK")


if __name__ == "__main__":
    test_c2_reads_pedigree_from_horse_info()
    test_merge_into_race_sets_pedigree_only_when_found()
    test_ped_page_three_generations()
    test_ped_page_five_generations()
    test_ped_page_without_table_returns_none()
    test_store_appends_without_overwriting()
    test_backfill_races_collects_and_records_done()
    test_backfill_races_stops_early_without_marking_done()
    test_real_sample_if_present()
