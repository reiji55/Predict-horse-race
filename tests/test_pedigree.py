"""
血統（父・母・母父）の取得と保存のテスト。

**ネットワークには一切アクセスしない**。`tests/fixtures/` の自作フィクスチャ（構造だけを再現、馬名は架空）で検証する。
C2 の Horse_Info の並び（Horse01=父 / Horse03=母 / Horse04=(母父)）は推定なので、本番の保存は
実ページでの確認（scripts/verify_pedigree_layout.py）が通るまで止まる。ここではその安全ゲートと、
「欠けた血統・食い違う血統を確定データにしない」「取れていないレースを済みにしない」を確かめる。

実行： python3 tests/test_pedigree.py  もしくは  python -m pytest tests/test_pedigree.py
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup  # noqa: E402

from scraper.fetchers import c2_shutuba_past, pedigree  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLES = Path(__file__).resolve().parent / "samples"
FULL = {"sire": "A", "dam": "B", "damsire": "C"}


def _c2_fixture() -> str:
    return (FIXTURES / "shutuba_past_pedigree.html").read_text(encoding="utf-8")


def _write_verified(path: Path, selectors=None) -> Path:
    path.write_text(json.dumps({"verified": True, "selectors": selectors or pedigree.C2_SELECTORS}), encoding="utf-8")
    return path


# --- 安全ゲート ------------------------------------------------------------------

def test_gate_closed_without_check_file_and_when_selectors_change():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "check.json"
        assert pedigree.c2_layout_verified(path) is False                       # 確認していない
        _write_verified(path)
        assert pedigree.c2_layout_verified(path) is True
        _write_verified(path, {**pedigree.C2_SELECTORS, "dam": ".Horse02"})
        assert pedigree.c2_layout_verified(path) is False                       # セレクタが変わったらやり直し
        path.write_text(json.dumps({"verified": False, "selectors": pedigree.C2_SELECTORS}), encoding="utf-8")
        assert pedigree.c2_layout_verified(path) is False
    print("test_gate_closed_without_check_file_and_when_selectors_change: OK")


def test_c2_does_not_read_pedigree_until_verified():
    orig = pedigree.LAYOUT_CHECK_PATH
    with tempfile.TemporaryDirectory() as d:
        pedigree.LAYOUT_CHECK_PATH = Path(d) / "missing.json"
        try:
            by_horse = c2_shutuba_past.parse_shutuba_past_html(_c2_fixture())
        finally:
            pedigree.LAYOUT_CHECK_PATH = orig
    assert all(v["pedigree"] is None for v in by_horse.values())
    assert by_horse["2021100001"]["popularity"] == 5                           # ほかの読み取りは変わらない
    print("test_c2_does_not_read_pedigree_until_verified: OK")


def test_store_is_not_written_until_verified():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "pedigree.csv"
        races = [{"entries": [{"horse_ref": {"netkeiba": "2021100001"}, "pedigree": FULL}]}]
        counts = pedigree.update_store_from_races(races, path, verified=False)
        assert counts["added"] == 0 and not path.exists()
    print("test_store_is_not_written_until_verified: OK")


# --- 読み取り --------------------------------------------------------------------

def test_c2_reads_pedigree_from_horse_info():
    by_horse = c2_shutuba_past.parse_shutuba_past_html(_c2_fixture(), read_pedigree=True)
    ped = by_horse["2021100001"]["pedigree"]
    assert (ped["sire"], ped["dam"], ped["damsire"]) == ("テスト種牡馬A", "テスト繁殖牝馬A", "テスト母父A")  # 括弧は外す
    assert by_horse["2021100002"]["pedigree"] is None                           # 血統の div が無い
    partial = by_horse["2021100003"]["pedigree"]                                # 父だけ
    assert partial["sire"] == "テスト種牡馬C" and partial["dam"] is None and not pedigree.is_complete(partial)
    print("test_c2_reads_pedigree_from_horse_info: OK")


def test_horse_info_with_other_classes_gives_nothing():
    html = '<td class="Horse_Info"><div class="HorseA">父?</div><div class="HorseB">母?</div></td>'
    assert pedigree.parse_horse_info_pedigree(BeautifulSoup(html, "lxml").td) is None
    print("test_horse_info_with_other_classes_gives_nothing: OK")


def test_merge_into_race_sets_only_complete_pedigree():
    by_horse = c2_shutuba_past.parse_shutuba_past_html(_c2_fixture(), read_pedigree=True)
    race = {"entries": [{"horse_ref": {"netkeiba": f"202110000{i}"}} for i in (1, 2, 3)]}
    c2_shutuba_past.merge_into_race(race, by_horse)
    assert race["entries"][0]["pedigree"]["sire"] == "テスト種牡馬A"
    assert "pedigree" not in race["entries"][1]
    assert "pedigree" not in race["entries"][2]                                 # 一部欠けは raw にも入れない
    print("test_merge_into_race_sets_only_complete_pedigree: OK")


def test_ped_page_three_generations():
    ped = pedigree.parse_ped_page_html((FIXTURES / "pedigree_ped_page.html").read_text(encoding="utf-8"))
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
        for side in ("父", "母"):
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
    assert (ped["sire"], ped["dam"], ped["damsire"]) == ("父", "母", "母父")
    print("test_ped_page_five_generations: OK")


def test_ped_page_with_unexpected_shape_returns_none():
    assert pedigree.parse_ped_page_html("<html><title>別ページ</title></html>") is None
    three_tops = ('<table class="blood_table"><tr><td rowspan="2">a</td><td>x</td></tr><tr><td>y</td></tr>'
                  '<tr><td rowspan="2">b</td><td>z</td></tr><tr><td>w</td></tr>'
                  '<tr><td rowspan="2">c</td><td>v</td></tr><tr><td>u</td></tr></table>')
    assert pedigree.parse_ped_page_html(three_tops) is None                      # 最大rowspanが3つ＝想定外
    print("test_ped_page_with_unexpected_shape_returns_none: OK")


# --- 保存の約束 ------------------------------------------------------------------

def test_incomplete_is_not_stored_and_can_be_filled_later():
    store: dict = {}
    assert pedigree.add_record(store, "2021100003", {"sire": "A", "dam": None, "damsire": ""}, "x") == "incomplete"
    assert pedigree.add_record(store, "2021100003", None, "x") == "incomplete"
    assert store == {}
    assert pedigree.add_record(store, "2021100003", FULL, "x") == "added"         # 後でそろえば入る
    print("test_incomplete_is_not_stored_and_can_be_filled_later: OK")


def test_conflict_is_logged_not_overwritten():
    with tempfile.TemporaryDirectory() as d:
        conflicts = Path(d) / "conflicts.csv"
        store: dict = {}
        assert pedigree.add_record(store, "h1", FULL, "shutuba_past", conflicts) == "added"
        assert pedigree.add_record(store, "h1", {"sire": "Ａ", "dam": "B ", "damsire": "C"}, "ped_page",
                                   conflicts) == "same"                          # 全角・空白の差は同じ扱い
        assert pedigree.add_record(store, "h1", {**FULL, "dam": "別の母"}, "ped_page", conflicts) == "conflict"
        assert store["h1"]["dam"] == "B"
        rows = list(csv.DictReader(conflicts.open(encoding="utf-8")))
        assert len(rows) == 1 and rows[0]["field"] == "dam" and rows[0]["new"] == "別の母"
        assert rows[0]["stored_source"] == "shutuba_past" and rows[0]["new_source"] == "ped_page"
    print("test_conflict_is_logged_not_overwritten: OK")


def test_store_from_races_when_verified():
    with tempfile.TemporaryDirectory() as d:
        path, conflicts = Path(d) / "pedigree.csv", Path(d) / "conflicts.csv"
        races = [{"entries": [
            {"horse_ref": {"netkeiba": "2021100001"}, "pedigree": FULL},
            {"horse_ref": {"netkeiba": "2021100002"}},
            {"horse_ref": {"netkeiba": "2021100003"}, "pedigree": {"sire": "A"}},
        ]}]
        counts = pedigree.update_store_from_races(races, path, verified=True, conflicts_path=conflicts)
        assert counts["added"] == 1 and counts["incomplete"] == 1
        store = pedigree.load_store(path)
        assert list(store) == ["2021100001"] and store["2021100001"]["source"] == "shutuba_past"
    print("test_store_from_races_when_verified: OK")


# --- 過去レースの取り込み：レースごとの完了判定 -----------------------------------------

def _complete(ref):
    return {ref: {"pedigree": {"sire": f"父{ref}", "dam": f"母{ref}", "damsire": f"母父{ref}"}}}


def _run_backfill(pages: dict, race_ids: list[str], d: Path, verified: bool = True):
    """ネットに出ずに backfill_pedigree_races.main を回す。pages: レースID → by_horse（RuntimeError なら取得失敗）。"""
    from scripts import backfill_pedigree_races as bf
    ids = d / "ids.txt"
    ids.write_text("\n".join(race_ids), encoding="utf-8")
    check = d / "check.json"
    if verified:
        _write_verified(check)
    calls: list[str] = []

    def fake_html(race_id):
        calls.append(race_id)
        if pages.get(race_id) is RuntimeError:
            raise RuntimeError("取得失敗")
        return race_id

    orig = (c2_shutuba_past.fetch_shutuba_past_html, c2_shutuba_past.parse_shutuba_past_html)
    c2_shutuba_past.fetch_shutuba_past_html = fake_html
    c2_shutuba_past.parse_shutuba_past_html = lambda html, n_runs=5, read_pedigree=None: pages.get(html, {})
    try:
        code = bf.main(["--race-ids", str(ids), "--limit", "50", "--store", str(d / "pedigree.csv"),
                        "--status", str(d / "status.csv"), "--conflicts", str(d / "conflicts.csv"),
                        "--layout-check", str(check)])
    finally:
        c2_shutuba_past.fetch_shutuba_past_html, c2_shutuba_past.parse_shutuba_past_html = orig
    status = bf.load_status(d / "status.csv")
    return code, calls, {k: v["status"] for k, v in status.items()}, status, pedigree.load_store(d / "pedigree.csv")


def test_backfill_refuses_until_verified():
    with tempfile.TemporaryDirectory() as tmp:
        code, calls, st, _, store = _run_backfill({"r1": _complete("h1")}, ["r1"], Path(tmp), verified=False)
    assert code == 2 and calls == [] and st == {} and store == {}
    print("test_backfill_refuses_until_verified: OK")


def test_backfill_marks_done_only_when_every_runner_is_complete():
    partial = {**_complete("p1"), "p2": {"pedigree": {"sire": "父だけ"}}}
    pages = {"r1": _complete("h1"), "r2": {}, "r3": {"n1": {"pedigree": None}, "n2": {"pedigree": None}},
             "r4": partial, "r5": RuntimeError, "r6": _complete("h6")}
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        code, _, st, status, store = _run_backfill(pages, ["r1", "r2", "r3", "r4", "r5", "r6"], d)
        assert code == 0
        assert st == {"r1": "done", "r2": "retry", "r3": "retry", "r4": "retry", "r5": "retry", "r6": "done"}
        assert status["r4"]["n_runners"] == "2" and status["r4"]["n_complete"] == "1" and status["r4"]["note"] == "partial"
        assert status["r2"]["note"] == "no_table" and status["r5"]["note"] == "fetch_error"
        assert set(store) == {"h1", "p1", "h6"}                                  # そろった馬だけ保存
        # 次の回：取り直せば回収できる。done は取りに行かない
        pages.update({"r2": _complete("h2"), "r3": _complete("h3"), "r4": {**partial, **_complete("p2")},
                      "r5": _complete("h5")})
        code, calls, st, status, store = _run_backfill(pages, ["r1", "r2", "r3", "r4", "r5", "r6"], d)
        assert calls == ["r2", "r3", "r4", "r5"]
        assert set(st.values()) == {"done"} and status["r2"]["attempts"] == "2"
        assert {"h2", "h3", "p2", "h5"} <= set(store)
    print("test_backfill_marks_done_only_when_every_runner_is_complete: OK")


def test_backfill_gives_up_after_max_attempts_without_calling_it_done():
    partial = {**_complete("p1"), "p2": {"pedigree": None}}
    pages = {"r0": _complete("h0"), "r1": {}, "r2": partial}
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for _ in range(3):
            code, _, st, _, _ = _run_backfill(pages, ["r0", "r1", "r2"], d)
        assert st == {"r0": "done", "r1": "unavailable", "r2": "partial_final"}
        code, calls, _, _, _ = _run_backfill(pages, ["r0", "r1", "r2"], d)
        assert calls == []                                                       # 終わったレースは取りに行かない
    print("test_backfill_gives_up_after_max_attempts_without_calling_it_done: OK")


def test_backfill_stops_early_without_recording():
    with tempfile.TemporaryDirectory() as tmp:
        pages = {f"r{i}": {"x": {"pedigree": None}} for i in range(12)}
        code, calls, st, _, store = _run_backfill(pages, list(pages), Path(tmp))
    assert code == 1 and len(calls) == 5 and st == {} and store == {}
    print("test_backfill_stops_early_without_recording: OK")


def test_backfill_stops_on_a_streak_and_keeps_the_streak_retryable():
    from scripts import backfill_pedigree_races as bf
    ids = ["ok"] + [f"z{i:02d}" for i in range(30)]
    pages = {"ok": _complete("h1"), **{f"z{i:02d}": {} for i in range(30)}}
    with tempfile.TemporaryDirectory() as tmp:
        code, calls, st, _, _ = _run_backfill(pages, ids, Path(tmp))
    assert code == 1 and len(calls) == 1 + bf.STREAK_LIMIT
    assert st == {"ok": "done"}                                                  # 0頭が続いた分は記録しない
    print("test_backfill_stops_on_a_streak_and_keeps_the_streak_retryable: OK")


# --- 実ページでの確認（scripts/verify_pedigree_layout.py） ------------------------------

def test_layout_check_passes_only_when_every_value_matches():
    from scripts import verify_pedigree_layout as v
    html = _c2_fixture()
    good = {"R1": {"2021100001": ("テスト馬イチ", "テスト種牡馬A", "テスト繁殖牝馬A", "テスト母父A")}}
    report = v.check_layout({"R1": html}, good)
    assert report["verified"] is True and report["n_ok"] == 1 and report["selectors"] == pedigree.C2_SELECTORS
    bad = {"R1": {"2021100001": ("テスト馬イチ", "テスト種牡馬A", "別の母", "テスト母父A")}}
    report = v.check_layout({"R1": html}, bad)
    assert report["verified"] is False and report["horses"][0]["match"]["dam"] is False
    assert any(item["class"].startswith("Horse03") for item in report["horses"][0]["structure"])  # 直す手がかり
    missing = {"R1": good["R1"], "R2": {"2021100009": ("x", "a", "b", "c")}}
    assert v.check_layout({"R1": html, "R2": None}, missing)["verified"] is False  # 取れないページがあれば不合格
    assert v.check_layout({"R1": html}, {"R1": {"2021100002": ("テスト馬ニ", "a", "b", "c")}})["verified"] is False
    print("test_layout_check_passes_only_when_every_value_matches: OK")


def test_real_sample_if_present():
    """実サンプルがあれば、今のセレクタで父・母・母父が取れるかを確かめる（無ければスキップ）。"""
    sample = SAMPLES / "shutuba_past_hanshin11.html"
    if not sample.exists():
        import pytest
        pytest.skip("tests/samples/shutuba_past_hanshin11.html が無い（実ページの確認は verify_pedigree_layout）")
    by_horse = c2_shutuba_past.parse_shutuba_past_html(sample.read_text(encoding="utf-8", errors="replace"),
                                                       read_pedigree=True)
    peds = [v["pedigree"] for v in by_horse.values()]
    got = sum(1 for p in peds if pedigree.is_complete(p))
    assert got >= len(peds) * 0.8, f"父・母・母父がそろったのは {got}/{len(peds)} 頭。Horse_Info の並びが推定と違う可能性"
    print(f"test_real_sample_if_present: OK（{got}/{len(peds)} 頭）")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except BaseException as e:      # pytest.skip は Skipped（BaseException）
                if type(e).__name__ == "Skipped":
                    print(f"{name}: SKIP（{e}）")
                else:
                    raise
