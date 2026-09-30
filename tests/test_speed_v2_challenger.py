"""
PR C — speed-v2 Challenger（docs/audit/SPEED_BASE_TIMES_V2_DESIGN_20260928.md §9・§13・§14）のテスト。

Champion と同じロジックのまま、基準タイム表だけを凍結した v2 表（PR B）に差し替えた影モデル。
- Champion の予想・スナップショットは変わらない（config/models.json を含む config_hash だけが変わる）
- v2 表を読むのは登録した Challenger だけ。表が登録と違えば fail-closed
- スナップショットに表の id・hash・cutoff・推定式を残す
- 採点は registered_at より後に発走したレースだけ
"""
from __future__ import annotations

import copy
import datetime
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from logic import build_predictions as bp
from logic import model_registry, snapshots
from results import build_results, build_shadow_results

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "speed-base-times-v2-coverage"
ARTIFACT_ID = "base-times-v2-coverage-20260928-r2"
LOOKUP = ROOT / "data" / "reference" / "base_times" / f"{ARTIFACT_ID}.json"
META = ROOT / "data" / "reference" / "base_times" / f"{ARTIFACT_ID}.meta.json"
GENERATED_AT = "2026-09-26T09:00:00+09:00"
BEFORE_ALL_RACES = datetime.datetime(2026, 9, 1, tzinfo=bp.JST)
HANSHIN_0927 = "20260927-hanshin-11"


def _registry() -> dict:
    return model_registry.load_registry()


def _spec() -> dict:
    return next(m for m in _registry()["challengers"] if m["id"] == MODEL_ID)


def _raw() -> dict:
    return json.loads((ROOT / "raw" / "2026-W39.json").read_text(encoding="utf-8"))


def _build(spec: dict) -> dict:
    return bp.build_predictions(copy.deepcopy(_raw()), bp.load_configs(), model_registry_base_times(),
                                model_spec=spec, generated_at=GENERATED_AT)


def model_registry_base_times() -> dict:
    return json.loads((ROOT / "config" / "base_times.json").read_text(encoding="utf-8"))


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ------------------------------------------------------------------ 登録内容

def test_registry_entry_is_a_shadow_challenger_pinned_to_the_frozen_artifact():
    registry = _registry()
    spec = _spec()
    champion = registry["champion"]
    assert "base_times" not in champion
    assert spec["role"] == "challenger" and spec["enabled"] is True
    # Champion と同じロジック：買い目の相手選び・レジーム見送りは Champion と同じ設定
    assert spec["use_top3_partner"] == champion["use_top3_partner"] is False
    assert not spec.get("use_race_regime")

    meta = json.loads(META.read_text(encoding="utf-8"))
    override = spec["base_times"]
    assert override["artifact_id"] == meta["artifact_id"] == ARTIFACT_ID
    assert override["lookup_sha256"] == meta["hashes"]["lookup_sha256"]
    assert override["cutoff_date"] == meta["cutoff_date"] == "2026-09-27"

    # registered_at は凍結表を作った後（PR B マージ後）。2026-09-28 に遡らせない
    registered = datetime.datetime.fromisoformat(spec["registered_at"])
    assert registered > datetime.datetime.fromisoformat(meta["built_at"])
    assert registered > datetime.datetime(2026, 9, 28, tzinfo=bp.JST)


def test_frozen_table_loads_only_for_the_challenger_and_matches_the_artifact():
    assert model_registry.load_model_base_times(_registry()["champion"]) is None
    lookup, ref = model_registry.load_model_base_times(_spec())
    assert lookup == json.loads(LOOKUP.read_text(encoding="utf-8"))
    assert ref["artifact_id"] == ARTIFACT_ID and ref["cutoff_date"] == "2026-09-27"
    assert ref["method_version"] == "v1-median-class-offset"
    assert ref["lookup_file"] == f"data/reference/base_times/{ARTIFACT_ID}.json"


@pytest.mark.parametrize("change, message", [
    ({"lookup_sha256": "0" * 64}, "lookup_sha256"),
    ({"artifact_id": "base-times-v2-other"}, "artifact_id"),
    ({"cutoff_date": "2026-09-20"}, "cutoff_date"),
    ({"lookup_file": "config/base_times.json"}, "の下だけ"),
    ({"lookup_file": "data/reference/base_times/../../../config/base_times.json"}, "の下だけ"),
])
def test_mismatched_or_outside_table_fails_closed(change, message):
    spec = copy.deepcopy(_spec())
    spec["base_times"].update(change)
    with pytest.raises(ValueError, match=message):
        model_registry.load_model_base_times(spec)
    with pytest.raises(ValueError):
        _build(spec)          # 黙って Champion の表に戻して作ることはしない


def test_edited_table_bytes_fail_closed(tmp_path: Path, monkeypatch):
    ref_dir = tmp_path / "data" / "reference" / "base_times"
    ref_dir.mkdir(parents=True)
    shutil.copy(META, ref_dir / META.name)
    lookup = json.loads(LOOKUP.read_text(encoding="utf-8"))
    lookup["東京"]["芝"]["1600"] += 0.1
    (ref_dir / LOOKUP.name).write_text(json.dumps(lookup, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(model_registry, "ROOT", tmp_path)
    monkeypatch.setattr(model_registry, "REFERENCE_BASE_TIMES_DIR", ref_dir)
    with pytest.raises(ValueError, match="lookup_sha256"):
        model_registry.load_model_base_times(_spec())


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r["champion"].update(base_times=_spec()["base_times"]), "champion"),
    (lambda r: r["challengers"][-1]["base_times"].pop("meta_file"), "meta_file"),
    (lambda r: r["challengers"][-1].pop("registered_at"), "registered_at"),
    (lambda r: r["challengers"][-1].update(registered_at="2026-09-27T23:00:00+09:00"), "cutoff_date"),
    (lambda r: r["challengers"][-1].update(registered_at="2026-10-01T03:00:00"), "タイムゾーン"),
])
def test_registry_rejects_invalid_override(tmp_path: Path, mutate, message):
    registry = json.loads((ROOT / "config" / "models.json").read_text(encoding="utf-8"))
    assert registry["challengers"][-1]["id"] == MODEL_ID
    mutate(registry)
    path = tmp_path / "models.json"
    path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        model_registry.load_registry(path)


# ------------------------------------------------------------------ Champion は変わらない

def test_champion_output_is_unchanged_except_the_config_hash(tmp_path: Path, monkeypatch):
    """
    Challenger を登録する前（models.json から外した config）と後で、Champion の予想とスナップショットを比べる。
    違うのは models.json を含む config_hash だけ（race-regime Challenger を登録したときと同じ扱い）。
    """
    def build_champion(config_dir: Path) -> tuple[str, dict[str, str]]:
        monkeypatch.setattr(model_registry, "CONFIG_DIR", config_dir)
        predictions = _build(model_registry.load_registry(config_dir / "models.json")["champion"])
        snap_dir = tmp_path / config_dir.name / "snapshots"
        snapshots.freeze(predictions, now=BEFORE_ALL_RACES, directory=snap_dir)
        snaps = {p.name: p.read_text(encoding="utf-8") for p in sorted(snap_dir.glob("*.json"))}
        return _dump(predictions), snaps

    before_dir = tmp_path / "before"
    shutil.copytree(ROOT / "config", before_dir)
    registry = json.loads((before_dir / "models.json").read_text(encoding="utf-8"))
    registry["challengers"] = [m for m in registry["challengers"] if m["id"] != MODEL_ID]
    (before_dir / "models.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")

    before, before_snaps = build_champion(before_dir)
    after, after_snaps = build_champion(ROOT / "config")
    assert before != after                                    # config_hash は変わる
    assert model_registry.config_hash(before_dir) in before
    normalize = lambda text: re.sub(r'"config_hash": "[0-9a-f]{16}"', '"config_hash": "H"', text)  # noqa: E731
    assert normalize(before) == normalize(after)
    assert sorted(before_snaps) == sorted(after_snaps) and len(after_snaps) == 4
    assert all(normalize(before_snaps[n]) == normalize(after_snaps[n]) for n in after_snaps)
    assert "base_times_ref" not in after and all("base_times_ref" not in s for s in after_snaps.values())


def test_building_the_challenger_does_not_leak_into_the_champion():
    champion_spec = _registry()["champion"]
    first = _dump(_build(champion_spec))
    challenger = _build(_spec())
    again = _dump(_build(champion_spec))
    assert first == again
    champion = json.loads(first)
    assert champion["model"]["base_times_hash"] == model_registry.base_times_hash()
    assert challenger["model"]["base_times_hash"] != champion["model"]["base_times_hash"]
    assert (ROOT / "config" / "base_times.json").read_text(encoding="utf-8") != LOOKUP.read_text(encoding="utf-8")


# ------------------------------------------------------------------ Challenger が v2 表で動く

def test_challenger_uses_v2_table_and_records_it_in_snapshots(tmp_path: Path):
    champion = _build(_registry()["champion"])
    challenger = _build(_spec())
    races_c = {r["id"]: r for r in champion["races"]}
    races_v2 = {r["id"]: r for r in challenger["races"]}
    assert sorted(races_c) == sorted(races_v2)

    # 9/27 阪神芝1600：Champion は 0/16 で speed が切れる。v2 表なら guard を通る（カバレッジの確認のみ）
    assert races_c[HANSHIN_0927]["speed_quality"]["used"] is False
    assert races_v2[HANSHIN_0927]["speed_quality"]["used"] is True
    assert races_v2[HANSHIN_0927]["speed_quality"]["qualified_horses"] == 15
    # guard の設定は Champion と同じ
    for key in ("min_usable_runs", "min_race_coverage", "same_surface_only"):
        assert races_v2[HANSHIN_0927]["speed_quality"][key] == races_c[HANSHIN_0927]["speed_quality"][key]

    ref = challenger["model"]["base_times_ref"]
    assert ref["artifact_id"] == ARTIFACT_ID
    for race in challenger["races"]:
        assert race["base_times_ref"] == ref and race["model_id"] == MODEL_ID
        assert race["base_times_hash"] == ref["file_hash"]
        assert race["config_hash"] == champion["model"]["config_hash"]

    snap_dir = tmp_path / "snapshots"
    snapshots.freeze(challenger, now=BEFORE_ALL_RACES, directory=snap_dir)
    snap = json.loads((snap_dir / f"{HANSHIN_0927}.json").read_text(encoding="utf-8"))
    assert snap["base_times_ref"] == ref and snap["base_times_hash"] == ref["file_hash"]
    frozen = snapshots.as_predictions(snap_dir)["races"]
    assert all(r["base_times_ref"] == ref for r in frozen)

    settled = build_results.build_results({"races": frozen}, {HANSHIN_0927: {"finish": [1, 2, 3], "dividends": {}}})
    assert settled["results"][0]["base_times_ref"] == ref


def test_pipeline_skips_a_broken_challenger_without_touching_the_champion(tmp_path: Path, monkeypatch):
    registry = _registry()
    broken = copy.deepcopy(_spec())
    broken["id"] = "speed-broken"
    broken["base_times"]["lookup_sha256"] = "0" * 64
    registry["challengers"] = [broken, _spec()]

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "2026-W39.json").write_text(json.dumps(_raw(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bp, "RAW_DIR", raw_dir)
    monkeypatch.setattr(bp, "OUTPUT_PATH", tmp_path / "predictions.json")
    monkeypatch.setattr(bp, "CHALLENGER_ROOT", tmp_path / "challengers")
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(model_registry, "load_registry", lambda path=None: registry)
    monkeypatch.setattr(sys, "argv", ["build_predictions", "--week", "2026-W39"])

    bp.main()

    champion = json.loads((tmp_path / "predictions.json").read_text(encoding="utf-8"))
    assert champion["model"]["model_id"] == registry["champion"]["id"]
    assert "base_times_ref" not in champion["model"]
    assert not (tmp_path / "challengers" / "speed-broken").exists()
    good = json.loads((tmp_path / "challengers" / MODEL_ID / "predictions.json").read_text(encoding="utf-8"))
    assert good["model"]["base_times_ref"]["artifact_id"] == ARTIFACT_ID


def test_prediction_path_never_reads_post_race_results():
    for path in (ROOT / "logic").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "race_results" not in text and "results.json" not in text, path


# ------------------------------------------------------------------ 採点：登録後のレースだけ・speed の起動率も並べる

def _snapshot(race_id: str, post_time: str, model_id: str, used: bool, qualified: int) -> dict:
    return {
        "race_id": race_id, "week_id": "2026-W40", "frozen_at": "2026-10-03T10:00:00+09:00",
        "pre_race": True, "post_time": post_time, "model_id": model_id, "model_role": "challenger",
        "speed_quality": {"used": used, "qualified_horses": qualified, "total_horses": 16},
        "cards": [{"char": "kei", "total": 500, "model_version": model_id, "model_role": "challenger",
                   "bets": [{"type": "ワイド", "horses": [1, 2], "amt": 500}]}],
        "marks": [{"num": 1, "odds": 2.0}, {"num": 2, "odds": 4.0}],
    }


def test_shadow_results_count_only_races_after_registration(tmp_path: Path, monkeypatch):
    challenger_root = tmp_path / "challengers"
    champion_dir = tmp_path / "snapshots"
    snap_dir = challenger_root / MODEL_ID / "snapshots"
    snap_dir.mkdir(parents=True)
    champion_dir.mkdir()
    before = "20260927-hanshin-11"     # 登録より前のレース（表に勝ちタイムが入っている）
    after = "20261004-hanshin-11"
    for race_id in (before, after):
        (snap_dir / f"{race_id}.json").write_text(
            json.dumps(_snapshot(race_id, "15:40", MODEL_ID, True, 15), ensure_ascii=False), encoding="utf-8")
        (champion_dir / f"{race_id}.json").write_text(
            json.dumps(_snapshot(race_id, "15:40", "champ", False, 0), ensure_ascii=False), encoding="utf-8")

    dividends = {"ワイド": [{"horses": [1, 2], "pay": 300}], "馬連": [], "3連複": []}
    race_results = {rid: {"finish": [1, 2, 3], "dividends": dividends} for rid in (before, after)}
    champion_results = {"results": [
        {"race_id": rid, "model_id": "champ", "cards": [{"char": "kei", "hit": False, "spent": 500, "payout": 0}]}
        for rid in (before, after)
    ]}
    registry = {"champion": {"id": "champ", "role": "champion"}, "challengers": [_spec()]}

    monkeypatch.setattr(build_shadow_results, "CHALLENGER_ROOT", challenger_root)
    monkeypatch.setattr(build_shadow_results, "CHAMPION_SNAPSHOT_DIR", champion_dir)
    monkeypatch.setattr(build_shadow_results, "COMPARISON_PATH", tmp_path / "model_comparison.json")
    comparison = build_shadow_results.build_all(race_results, champion_results, registry=registry)

    settled = json.loads((challenger_root / MODEL_ID / "results.json").read_text(encoding="utf-8"))
    assert [r["race_id"] for r in settled["results"]] == [after]
    row = comparison["comparisons"][0]
    assert row["common_race_ids"] == [after]
    assert row["coverage"]["missing_in_challenger"] == [before]      # 登録前のレースは欠けとして見える
    assert row["speed_quality"]["champion"]["speed_used_rate"] == 0.0
    assert row["speed_quality"]["challenger"] == {
        "races": 1, "missing_snapshots": 0, "speed_used_races": 1, "speed_used_rate": 1.0,
        "qualified_horses": 15, "total_horses": 16, "qualified_horse_rate": 0.9375,
    }
