"""PR B（frozen artifact）の取得・監査ツールのテスト。ネットワークは使わない（取得関数は偽物）。

scripts/acquire_base_times_v2.py … 管理された取得（順番・途中再開・固定）
scripts/audit_base_times_v2.py   … 監査（再現性・カバレッジ・外れ値・v1 との差・speed guard）
"""
from __future__ import annotations

import collections
import datetime
import json
import random
from pathlib import Path

import pytest

from scripts import acquire_base_times_v2 as acq
from scripts import audit_base_times_v2 as aud
from scripts import base_times_v2 as v2
from scripts import build_base_times as bbt

ROOT = Path(__file__).resolve().parent.parent
FIX_DIR = ROOT / "tests" / "fixtures" / "base_times_v2"
CLASS_OFFSET = {"mi": 2.5, "1win": 1.5, "2win": 1.0, "3win": 0.5, "op": 0, "g3": -0.3, "g2": -0.6, "g1": -1.0}
CLASSES = list(CLASS_OFFSET)


class FakeFetch:
    """コースごとに決まった勝ちタイムを返す偽の fetch_course_records。呼ばれた順を記録する。"""

    def __init__(self, per_course: int = 8, empty: set[str] | None = None, outlier: str | None = None):
        self.calls: list[str] = []
        self.per_course = per_course
        self.empty = empty or set()
        self.outlier = outlier

    def __call__(self, venue, surface, dist, start_year, end_year):
        key = f"{venue}/{surface}/{dist}"
        self.calls.append(key)
        if key in self.empty:
            return []
        rng = random.Random(key)
        base = dist * (0.0585 if surface == "芝" else 0.0625)
        rows = []
        for i in range(self.per_course):
            klass = CLASSES[i % len(CLASSES)]
            win = base + CLASS_OFFSET[klass] * dist / 2000 + rng.uniform(-0.3, 0.3)
            rows.append({"date": f"2025-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}", "venue": venue,
                         "surface": surface, "dist": dist, "going": ["良", "稍重"][i % 2],
                         "class": klass, "heads": 16, "win_time": round(win, 1)})
        if key == self.outlier:
            rows.append({"date": "2025-06-30", "venue": venue, "surface": surface, "dist": dist,
                         "going": "不良", "class": "op", "heads": 16, "win_time": round(base + 12.0, 1)})
        # 未来日付（cutoff より後）と、検索条件と違うコースの行も混ぜる
        rows.append({"date": "2026-09-28", "venue": venue, "surface": surface, "dist": dist,
                     "going": "良", "class": "op", "heads": 16, "win_time": round(base, 1)})
        rows.append({"date": "2025-05-05", "venue": venue, "surface": surface, "dist": dist + 200,
                     "going": "良", "class": "op", "heads": 16, "win_time": round(base, 1)})
        return rows


def _courses(n: int | None = None):
    demand = collections.Counter({("東京", "芝", 1600): 20, ("阪神", "ダ", 1400): 30, ("阪神", "芝", 1200): 13})
    plan = acq.plan_courses(demand)
    return plan if n is None else plan[:n]


# ------------------------------------------------------------------ 取得順

def test_plan_is_turf_first_then_raw_demand():
    plan = _courses()
    assert len(plan) == sum(len(d) for ss in bbt.COURSES.values() for d in ss.values())
    surfaces = [s for _, s, _ in plan]
    assert surfaces == sorted(surfaces, key=acq.SURFACE_ORDER.index)            # 芝が全部先
    assert plan[:2] == [("東京", "芝", 1600), ("阪神", "芝", 1200)]               # 芝の中では需要順
    assert plan[surfaces.index("ダ")] == ("阪神", "ダ", 1400)                    # ダの先頭も需要順
    real = acq.plan_courses(acq.raw_demand(FIX_DIR))
    assert real[0][1] == "芝"


# ------------------------------------------------------------------ 取得（途中再開・固定）

def test_acquire_is_resumable_and_frozen_once_complete(tmp_path: Path):
    courses = _courses(5)
    out = tmp_path / "sources" / "records.json"
    fetch = FakeFetch()
    clock = lambda: datetime.datetime(2026, 9, 29, 12, 0, tzinfo=v2.JST)  # noqa: E731

    first = acq.acquire(courses, 2023, 2026, out, fetch=fetch, max_courses=2, clock=clock)
    assert first["status"] == "partial" and first["remaining"] == 3 and not out.exists()
    assert out.with_suffix(".partial.json").exists()

    second = acq.acquire(courses, 2023, 2026, out, fetch=fetch, clock=clock)
    assert second["status"] == "complete" and out.exists()
    assert not out.with_suffix(".partial.json").exists()
    assert fetch.calls == [acq.course_key(c) for c in courses]                  # 同じコースを2回取らない

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema"] == acq.SCHEMA and data["records_sha256"]
    assert data["source"]["kind"] == "netkeiba_race_search"
    assert data["source"]["http"]["min_interval_sec"] >= 3
    for key, row in data["courses"].items():
        assert row["dropped_mismatched_course"] == 1 and row["records"] == 9      # 別距離の行は捨てる
    assert any(r["date"] == "2026-09-28" for r in data["records"])               # cutoff は取得では掛けない
    assert data["records"] == sorted(data["records"], key=v2._record_sort_key)

    before = out.read_bytes()
    third = acq.acquire(courses, 2023, 2026, out, fetch=fetch, clock=clock)      # 完了済みは変えない
    assert third["status"] == "complete" and out.read_bytes() == before and len(fetch.calls) == 5


def test_acquire_refuses_mismatched_checkpoint_and_protected_paths(tmp_path: Path):
    out = tmp_path / "records.json"
    acq.acquire(_courses(3), 2023, 2026, out, fetch=FakeFetch(), max_courses=1)
    with pytest.raises(ValueError):
        acq.acquire(_courses(3), 2022, 2026, out, fetch=FakeFetch())             # 期間が違う
    with pytest.raises(ValueError):
        acq.acquire(_courses(4), 2023, 2026, out, fetch=FakeFetch())             # 予定コースが違う
    with pytest.raises(ValueError):
        acq.acquire(_courses(1), 2023, 2026, ROOT / "config" / "base_times.json", fetch=FakeFetch())


def test_empty_course_is_recorded_not_hidden(tmp_path: Path):
    courses = _courses(3)
    empty = acq.course_key(courses[1])
    out = tmp_path / "records.json"
    acq.acquire(courses, 2023, 2026, out, fetch=FakeFetch(empty={empty}))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["courses"][empty] == {**data["courses"][empty], "records": 0, "empty": True}


# ------------------------------------------------------------------ build → 監査

def _build_artifact(tmp_path: Path, fetch: FakeFetch, n_courses: int = 6):
    source = tmp_path / "records.json"
    acq.acquire(_courses(n_courses), 2023, 2026, source, fetch=fetch)
    out_dir = tmp_path / "ref"
    assert v2.main(["--source", "file", "--input", str(source), "--raw-dir", str(FIX_DIR),
                    "--cutoff", "2026-09-27", "--artifact-id", "base-times-v2-test",
                    "--built-at", "2026-09-29T12:00:00+09:00", "--write", "--output-dir", str(out_dir)]) == 0
    lookup = json.loads((out_dir / "base-times-v2-test.json").read_text(encoding="utf-8"))
    meta = json.loads((out_dir / "base-times-v2-test.meta.json").read_text(encoding="utf-8"))
    return source, lookup, meta


def test_audit_reproduces_artifact_and_checks_cutoff(tmp_path: Path):
    source, lookup, meta = _build_artifact(tmp_path, FakeFetch())
    records = bbt.load_records(source)
    result = aud.audit(lookup, meta, records, {}, FIX_DIR)

    assert all(result["reproducibility"].values())
    assert result["cutoff_check"]["within_cutoff"] is True
    assert result["cutoff_check"]["rejected"]["after_cutoff"] == 6               # 各コース1件の未来日付
    assert result["coverage"]["turf"]["filled"] == 6 and result["coverage"]["turf"]["total"] == 64
    assert result["sample_size"]["thin"] and result["sample_size"]["thin"][0]["n"] == 8
    focus = {r["course"]: r for r in result["focus_courses"]}
    assert focus["東京/芝/1600"]["status"] == "ok" and focus["東京/芝/1600"]["n"] == 8

    # artifact を1文字でも改ざんすると再現性チェックが落ちる
    tampered = json.loads(json.dumps(lookup))
    tampered["東京"]["芝"]["1600"] += 0.1
    assert aud.audit(tampered, meta, records, {}, FIX_DIR)["reproducibility"]["lookup_matches"] is False


def test_audit_flags_outliers_and_v1_differences_without_changing_values(tmp_path: Path):
    source, lookup, meta = _build_artifact(tmp_path, FakeFetch(outlier="東京/芝/1600"))
    records = bbt.load_records(source)
    champion = {"東京": {"芝": {"1600": lookup["東京"]["芝"]["1600"] + 1.0}}}
    before = json.dumps(lookup, sort_keys=True)
    result = aud.audit(lookup, meta, records, champion, FIX_DIR)

    outliers = result["outliers_within_course"]
    assert outliers["count"] >= 1 and outliers["rows"][0]["course"] == "東京/芝/1600"
    assert outliers["rows"][0]["going"] == "不良"
    flags = result["v1_comparison"]["flags"]
    assert flags == [{"course": "東京/芝/1600", "v1": champion["東京"]["芝"]["1600"],
                      "v2": lookup["東京"]["芝"]["1600"], "diff_sec": -1.0, "n_v2": 9}]
    assert json.dumps(lookup, sort_keys=True) == before                           # 監査は値を変えない
    assert set(result["speed_guard"]["focus_races"]) == set(aud.FOCUS_RACES)


def test_audit_cli_writes_only_to_report_dir(tmp_path: Path):
    source, _, _ = _build_artifact(tmp_path, FakeFetch())
    with pytest.raises(ValueError):
        aud.main(["--artifact-id", "base-times-v2-test", "--artifact-dir", str(tmp_path / "ref"),
                  "--source", str(source), "--raw-dir", str(FIX_DIR),
                  "--output", str(ROOT / "config" / "audit.json")])
    out = tmp_path / "audit" / "a.json"
    assert aud.main(["--artifact-id", "base-times-v2-test", "--artifact-dir", str(tmp_path / "ref"),
                     "--source", str(source), "--raw-dir", str(FIX_DIR), "--output", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == aud.AUDIT_SCHEMA


def test_relative_input_path_is_recorded_repo_relative(monkeypatch):
    """--input に相対パスを渡しても、入力の記録（パス・sha256）が作れる（PR B で見つかった不具合）。"""
    monkeypatch.chdir(ROOT)
    rel = Path("tests") / "fixtures" / "base_times_v2" / "2026-W39.json"
    digest = v2._file_digest(rel)
    assert digest["path"] == "tests/fixtures/base_times_v2/2026-W39.json" and len(digest["sha256"]) == 64
    assert v2._file_digest(ROOT / rel) == digest
