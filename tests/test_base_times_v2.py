"""基準タイム表 v2（scripts/base_times_v2.py）と raw collector の修正のテスト。

設計: docs/audit/SPEED_BASE_TIMES_V2_DESIGN_20260928.md §12（PR A — reference foundation only）

fixture: tests/fixtures/base_times_v2/2026-W39.json
    実際の raw/2026-W39.json から 9/26 の2レースを切り出したもの（現行スキーマ races[].entries[].past_runs）。
"""
from __future__ import annotations

import datetime
import json
import random
import sys
from pathlib import Path

import pytest

from logic import speed_index
from scripts import base_times_v2 as v2
from scripts import build_base_times as bbt

ROOT = Path(__file__).resolve().parent.parent
FIX_DIR = ROOT / "tests" / "fixtures" / "base_times_v2"
CLASS_OFFSET = {"mi": 2.5, "1win": 1.5, "2win": 1.0, "3win": 0.5, "op": 0, "g3": -0.3, "g2": -0.6, "g1": -1.0}
CUTOFF = datetime.date(2026, 9, 27)
BUILT_AT = "2026-09-29T00:00:00+09:00"


def _run(date, venue="東京", surface="芝", dist=1600, klass="op", going="良", time=93.4, finish=1):
    return {"date": date, "venue": venue, "surface": surface, "dist": dist, "going": going,
            "class": klass, "finish": finish, "time_sec": time}


def _record(date, venue="東京", surface="芝", dist=1600, klass="op", going="良", time=93.4):
    return {"date": date, "venue": venue, "surface": surface, "dist": dist, "going": going,
            "class": klass, "win_time": time}


def _write_raw(directory: Path, name: str, races: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps({"races": races}, ensure_ascii=False), encoding="utf-8")


def _build(records, **kwargs):
    params = dict(cutoff=CUTOFF, artifact_id="test-artifact", built_at=BUILT_AT,
                  source={"kind": "test"}, class_offset=CLASS_OFFSET, min_samples=5)
    params.update(kwargs)
    return v2.build_v2(records, **params)


# ------------------------------------------------------------------ 現行 raw スキーマ

def test_current_raw_fixture_yields_records():
    records, report = bbt.collect_raw_records(FIX_DIR)
    assert report["files"] == 1 and report["winning_runs"] == 19
    assert len(records) == 19
    for record in records:
        assert record["win_time"] is not None and record["date"] and record["venue"]
        assert record["source"]["race_id"] in {"20260926-hanshin-11", "20260926-nakayama-11"}
    # 従来の呼び出し口も同じレコードを返す
    assert bbt.collect_from_raw(FIX_DIR) == records


def test_entries_schema_is_read_not_only_legacy_horses(tmp_path: Path):
    """race["horses"] しか見ていなかった回帰（現行 raw で0件になる）を防ぐ。"""
    _write_raw(tmp_path / "current", "w.json", [{"id": "r1", "entries": [{"num": 1, "past_runs": [_run("2026-05-10")]}]}])
    _write_raw(tmp_path / "legacy", "w.json", [{"id": "r1", "horses": [{"past_runs": [_run("2026-05-10")]}]}])
    assert len(bbt.collect_from_raw(tmp_path / "current")) == 1
    assert len(bbt.collect_from_raw(tmp_path / "legacy")) == 1      # 旧スキーマも後方互換で読む

    # 実際の raw（現行スキーマ）でも 0 件にならない
    assert bbt.collect_from_raw(FIX_DIR)


# ------------------------------------------------------------------ v1 と同じ値

def test_same_records_give_v1_identical_base_times():
    records, _ = bbt.collect_raw_records(FIX_DIR)
    # 複数コースに min_samples 以上が集まるよう、合成レコードを足す（クラス混在・馬場混在）
    for i, klass in enumerate(["mi", "1win", "2win", "op", "g3", "g1"]):
        records.append(_record(f"2026-0{i + 1}-15", "中山", "ダ", 1800, klass, "稍重", 111.0 + i * 0.3))
        records.append(_record(f"2026-0{i + 1}-16", "阪神", "芝", 1600, klass, "良", 93.0 + i * 0.2))
    v1_table, _ = bbt.build_table(records, CLASS_OFFSET, min_samples=2)
    result = _build(records, min_samples=2)
    assert result["lookup"] == v1_table
    assert result["lookup"]["中山"]["ダ"]["1800"] == v1_table["中山"]["ダ"]["1800"]
    for venue, by_surface in v1_table.items():
        for surface, by_dist in by_surface.items():
            for dist, value in by_dist.items():
                assert result["meta"]["courses"][f"{venue}/{surface}/{dist}"]["value"] == value
                assert speed_index.lookup_base_time(result["lookup"], venue, surface, int(dist)) == value


# ------------------------------------------------------------------ cutoff

def test_records_after_cutoff_are_excluded(tmp_path: Path):
    records = [_record("2026-09-20", time=93.0), _record("2026-09-27", time=93.2),
               _record("2026-09-28", time=90.0), _record(None, time=91.0), _record("bad-date", time=91.5)]
    result = _build(records, min_samples=1)
    meta = result["meta"]
    assert meta["counts"]["rejected"] == {"missing_date": 2, "after_cutoff": 1, "duplicate": 0}
    assert meta["courses"]["東京/芝/1600"]["n"] == 2                   # cutoff 当日は使う
    assert meta["courses"]["東京/芝/1600"]["date_max"] == "2026-09-27"
    assert meta["source"]["period"] == {"date_min": "2026-09-20", "date_max": "2026-09-27"}
    assert meta["cutoff_date"] == "2026-09-27"

    # collector 側の cutoff も同じ規則
    _write_raw(tmp_path, "w.json", [{"id": "r", "entries": [{"past_runs": [
        _run("2026-09-27"), _run("2026-09-28", time=90.0), _run(None, time=91.0)]}]}])
    got, report = bbt.collect_raw_records(tmp_path, cutoff=CUTOFF)
    assert [r["date"] for r in got] == ["2026-09-27"]
    assert report["after_cutoff"] == 1 and report["missing_date"] == 1


# ------------------------------------------------------------------ 重複排除

def test_duplicates_are_removed_deterministically(tmp_path: Path):
    same = _run("2026-05-10")
    _write_raw(tmp_path, "2026-W19.json", [{"id": "a", "entries": [{"num": 1, "past_runs": [same]},
                                                                  {"num": 2, "past_runs": [same]}]}])
    _write_raw(tmp_path, "2026-W20.json", [{"id": "b", "entries": [{"num": 3, "past_runs": [same]}],
                                            "horses": [{"past_runs": [same]}]}])
    records, report = bbt.collect_raw_records(tmp_path)
    assert len(records) == 1 and report["duplicates"] == 3
    assert records[0]["source"] == {"file": "2026-W19.json", "race_id": "a", "horse": None}   # 最初に見つけたもの

    # 同じ日・同じコース・同じタイムでもクラスが違えば別レース（潰さない）
    distinct = [_record("2026-05-10", klass="1win"), _record("2026-05-10", klass="op")]
    assert _build(distinct, min_samples=1)["meta"]["counts"]["used_records"] == 2

    # 他の経路のレコードにも同じ重複排除が掛かる
    dup = [_record("2026-05-10"), _record("2026-05-10")]
    assert _build(dup, min_samples=1)["meta"]["counts"]["rejected"]["duplicate"] == 1


# ------------------------------------------------------------------ min_samples

def test_min_samples_keeps_thin_courses_out_of_lookup_but_in_meta():
    records = [_record(f"2026-05-0{d}", "東京", "芝", 1600, time=93.0 + d / 10) for d in range(1, 5)]   # 4本
    records += [_record(f"2026-06-0{d}", "東京", "芝", 1400, time=81.0 + d / 10) for d in range(1, 6)]  # 5本
    result = _build(records, min_samples=5)
    assert "1600" not in result["lookup"]["東京"]["芝"]
    assert result["lookup"]["東京"]["芝"]["1400"] == 81.3
    thin = result["meta"]["courses"]["東京/芝/1600"]
    assert thin["status"] == "insufficient_samples" and thin["value"] is None and thin["n"] == 4
    assert result["meta"]["courses"]["東京/芝/1400"]["status"] == "ok"
    assert "東京/芝/1600" in result["meta"]["coverage"]["courses_missing"]
    assert result["meta"]["estimator"]["min_samples"] == 5


# ------------------------------------------------------------------ metadata

def test_metadata_contents():
    times = [93.0, 93.2, 93.4, 93.6, 94.4]
    goings = ["良", "良", "稍重", None, "良"]
    records = [_record(f"2026-0{i + 1}-10", klass="op", going=g, time=t)
               for i, (t, g) in enumerate(zip(times, goings))]
    records.append(_record("2026-07-10", klass="1win", time=94.8))    # 正規化すると 94.8 - 1.5*0.8 = 93.6
    meta = _build(records)["meta"]
    course = meta["courses"]["東京/芝/1600"]
    # 正規化後: [93.0, 93.2, 93.4, 93.6, 93.6, 94.4]
    assert course["n"] == 6
    assert course["value"] == 93.5 and course["median"] == 93.5
    assert course["mad"] == 0.2                     # |x-93.5| = .5 .3 .1 .1 .1 .9 → 中央値 .2
    assert course["p25"] == 93.25 and course["p75"] == 93.6
    assert course["min"] == 93.0 and course["max"] == 94.4
    assert course["date_min"] == "2026-01-10" and course["date_max"] == "2026-07-10"
    assert course["class_counts"] == {"1win": 1, "op": 5}
    assert course["going_counts"] == {"unknown": 1, "稍重": 1, "良": 4}

    assert meta["schema"] == v2.META_SCHEMA and meta["artifact_id"] == "test-artifact"
    assert meta["built_at"] == BUILT_AT and meta["used_for_prediction"] is False
    assert meta["champion_base_times_untouched"] is True
    est = meta["estimator"]
    assert est["version"] == v2.ESTIMATOR_VERSION and est["class_offset"] == dict(sorted(CLASS_OFFSET.items()))
    assert est["goings"] == "all" and len(est["class_offset_hash"]) == 64
    cov = meta["coverage"]
    assert cov["courses_total"] == 101 and cov["courses_filled"] == 1
    assert cov["by_surface"]["芝"] == {"filled": 1, "total": 64}
    assert cov["by_surface"]["ダ"] == {"filled": 0, "total": 37}
    assert set(meta["hashes"]) == {"lookup_sha256", "records_sha256", "meta_content_sha256"}


# ------------------------------------------------------------------ 決定的な出力

def test_output_and_hashes_are_deterministic(tmp_path: Path):
    records, _ = bbt.collect_raw_records(FIX_DIR)
    records += [_record(f"2026-0{d}-01", "中山", "ダ", 1800, time=111.0 + d / 10) for d in range(1, 7)]
    shuffled = records[:]
    random.Random(7).shuffle(shuffled)

    a = _build(records)
    b = _build(shuffled)
    c = _build(records, built_at="2030-01-01T00:00:00+09:00")
    assert v2.canonical_json(a) == v2.canonical_json(b)                  # 入力順に依存しない
    assert a["meta"]["hashes"] == c["meta"]["hashes"]                    # built_at はハッシュに入らない
    assert a["meta"]["hashes"]["lookup_sha256"] == v2.sha256_text(v2.canonical_json(a["lookup"]))

    changed = _build(records + [_record("2026-08-01", "中山", "ダ", 1800, time=120.0)])
    assert changed["meta"]["hashes"]["records_sha256"] != a["meta"]["hashes"]["records_sha256"]

    paths_1 = v2.write_artifacts(a, tmp_path / "one")
    paths_2 = v2.write_artifacts(b, tmp_path / "two")
    for p1, p2 in zip(paths_1, paths_2):
        assert p1.name == p2.name and p1.read_bytes() == p2.read_bytes()
    assert [p.name for p in paths_1] == ["test-artifact.json", "test-artifact.meta.json"]


# ------------------------------------------------------------------ Champion の表を守る

def test_champion_base_times_are_never_written(tmp_path: Path, monkeypatch):
    champion = (ROOT / "config" / "base_times.json").read_bytes()
    result = _build([_record("2026-05-10")], min_samples=1)
    with pytest.raises(ValueError):
        v2.write_artifacts(result, ROOT / "config")
    with pytest.raises(ValueError):
        v2.write_artifacts(result, ROOT / "config" / "sub")

    # v2 CLI の既定は dry-run（何も書かない）
    out = tmp_path / "out"
    assert v2.main(["--source", "raw", "--raw-dir", str(FIX_DIR), "--cutoff", "2026-09-27",
                    "--output-dir", str(out), "--built-at", BUILT_AT]) == 0
    assert not out.exists()

    # v1 CLI の --source raw は dry-run 専用（config/base_times.json を raw 由来の表で上書きしない）
    monkeypatch.setattr(sys, "argv", ["build_base_times", "--source", "raw"])
    with pytest.raises(SystemExit):
        bbt.main()
    assert (ROOT / "config" / "base_times.json").read_bytes() == champion


# ------------------------------------------------------------------ 凍結 artifact は変えない

def test_artifact_is_immutable_once_written(tmp_path: Path):
    records = [_record(f"2026-05-0{d}", time=93.0 + d / 10) for d in range(1, 6)]
    first = _build(records)
    out = tmp_path / "ref"

    lookup_path, meta_path = v2.write_artifacts(first, out)            # 1. 初回は書く
    before = (lookup_path.read_bytes(), meta_path.read_bytes())

    assert v2.write_artifacts(_build(records), out) == (lookup_path, meta_path)   # 2. 同じ内容は no-op
    assert (lookup_path.read_bytes(), meta_path.read_bytes()) == before

    changed = _build(records + [_record("2026-06-01", time=99.0)])      # 3. 同じ id で中身が違う → 拒否
    with pytest.raises(ValueError, match="別の内容"):
        v2.write_artifacts(changed, out)
    meta_only = dict(first, meta={**first["meta"], "built_at": "2030-01-01T00:00:00+09:00"})
    with pytest.raises(ValueError, match="別の内容"):                     #    meta だけ違っても拒否
        v2.write_artifacts(meta_only, out)
    assert (lookup_path.read_bytes(), meta_path.read_bytes()) == before

    for missing in (meta_path, lookup_path):                            # 4. 片方だけ存在 → 拒否
        other = tmp_path / f"partial-{missing.name}"
        other.mkdir()
        (other / lookup_path.name if missing is meta_path else other / meta_path.name).write_bytes(b"{}\n")
        with pytest.raises(ValueError, match="片方だけ"):
            v2.write_artifacts(first, other)
        assert len(list(other.iterdir())) == 1


# ------------------------------------------------------------------ 書き出し先のガード

@pytest.mark.parametrize("bad_id", ["../../../config/base_times", "../escape", "sub/dir", "/abs/path",
                                    "..", ".hidden", "a\\b", "", "a b"])
def test_traversal_or_absolute_artifact_ids_are_rejected(tmp_path: Path, bad_id):
    result = _build([_record("2026-05-10")], min_samples=1, artifact_id=bad_id)
    with pytest.raises(ValueError):
        v2.write_artifacts(result, tmp_path / "ref")
    assert not (tmp_path / "ref").exists()


def test_report_and_artifacts_never_land_in_protected_dirs(tmp_path: Path):
    champion = (ROOT / "config" / "base_times.json").read_bytes()
    common_args = ["--source", "raw", "--raw-dir", str(FIX_DIR), "--cutoff", "2026-09-27", "--built-at", BUILT_AT]

    protected_files = ["config/base_times.json", "scripts/build_base_times.py", "tests/test_base_times_v2.py",
                       ".github/workflows/ci.yml", "requirements-dev.txt"]
    before = {name: (ROOT / name).read_bytes() for name in protected_files}
    for report in (*protected_files, str(ROOT / "config" / "base_times.json"), "data/results.json",
                   "raw/x.json", "docs/new_report.json", "data/reference/base_times/x.json"):
        with pytest.raises(SystemExit):
            v2.main(common_args + ["--report", report])
    assert {name: (ROOT / name).read_bytes() for name in protected_files} == before
    assert not (ROOT / "docs" / "new_report.json").exists()
    # リポジトリ内で report を書けるのは専用ディレクトリだけ
    assert v2.ensure_writable(v2.REPORT_DIR / "coverage.json", v2.REPORT_ALLOWED_DIRS)
    with pytest.raises(ValueError):
        v2.ensure_writable(ROOT / "scripts" / "x.json", v2.ARTIFACT_ALLOWED_DIRS)   # artifact も許可リスト外は不可
    with pytest.raises(SystemExit):
        v2.main(common_args + ["--artifact-id", "../../../config/base_times", "--write",
                               "--output-dir", str(tmp_path / "ref")])
    with pytest.raises(ValueError):
        v2.ensure_writable(ROOT / "data" / "reference" / "base_times" / ".." / ".." / "results.json",
                           v2.ARTIFACT_ALLOWED_DIRS)
    assert v2.ensure_writable(v2.OUTPUT_DIR / "x.json", v2.ARTIFACT_ALLOWED_DIRS)   # 研究用の置き場は可

    # 正常なパスなら成功する
    report_path = tmp_path / "reports" / "coverage.json"
    assert v2.main(common_args + ["--artifact-id", "base-times-v2-test", "--write",
                                  "--output-dir", str(tmp_path / "ref"), "--report", str(report_path)]) == 0
    assert report_path.exists()
    assert sorted(p.name for p in (tmp_path / "ref").iterdir()) == ["base-times-v2-test.json",
                                                                   "base-times-v2-test.meta.json"]
    assert (ROOT / "config" / "base_times.json").read_bytes() == champion


def test_prediction_path_does_not_read_v2_artifacts():
    for directory in ("logic", "scraper", "results", "research"):
        for path in (ROOT / directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "base_times_v2" not in text and "data/reference" not in text, path


# ------------------------------------------------------------------ coverage dry-run

def test_coverage_report_matches_guard_and_is_read_only():
    champion = speed_index.load_base_times()
    before = json.dumps(champion, sort_keys=True)
    report = v2.coverage_report(FIX_DIR, {"champion_v1": champion, "empty": {}})
    assert json.dumps(champion, sort_keys=True) == before

    all_runs = [run for r in json.loads((FIX_DIR / "2026-W39.json").read_text(encoding="utf-8"))["races"]
                for e in r["entries"] for run in e["past_runs"]]
    eligible = [run for run in all_runs if run["venue"] in bbt.constants.JRA_VENUES
                and run["surface"] in speed_index.VALID_SURFACES]
    demand = report["demand"]
    assert demand["eligible_runs"] == len(eligible)
    assert demand["eligible_runs"] + demand["ineligible_runs"] == len(all_runs)
    assert demand["ineligible_runs"] > 0                     # 地方・海外・障害の走は需要に混ぜない
    assert report["tables"]["empty"]["runs_covered"] == 0
    assert report["tables"]["ceiling_all_demanded"]["runs_missing"] == 0

    rows = {r["race_id"]: r for r in report["races"]}
    # 実データの 9/26 と同じ結果（docs/audit/SPEED_BASE_TIMES_V2_DESIGN_20260928.md §1.3）
    assert (rows["20260926-hanshin-11"]["champion_v1"]["qualified_horses"],
            rows["20260926-hanshin-11"]["champion_v1"]["used"]) == (11, True)
    assert (rows["20260926-nakayama-11"]["champion_v1"]["qualified_horses"],
            rows["20260926-nakayama-11"]["champion_v1"]["used"]) == (6, False)
    assert rows["20260926-hanshin-11"]["ceiling_all_demanded"]["qualified_horses"] == 14
    assert rows["20260926-nakayama-11"]["ceiling_all_demanded"]["qualified_horses"] == 14
    assert rows["20260926-hanshin-11"]["empty"]["used"] is False
