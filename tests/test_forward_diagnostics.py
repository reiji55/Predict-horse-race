"""research/forward_diagnostics.py（forward-diagnostics-v1・observe-only）のテスト。

仕様: docs/audit/FORWARD_DIAGNOSTICS_SPEC_20260927.md §8

fixture は 2026-09-27 の実データ（tests/fixtures/forward_diagnostics/）:
    snapshots/ … 採点時の data/snapshots（バイト単位で同一）
    prerace/   … 13:21 の記録（その後 snapshot が再凍結されたため SHA 不一致）と 14:57 の記録（一致）
    race_results.json … 確定着順（阪神 1-4-8、中山 16-6-5）
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import logging
import shutil
from pathlib import Path

import pytest

from logic import build_predictions, model_registry
from research import common, forward_diagnostics as fd

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "forward_diagnostics"
HANSHIN = "20260927-hanshin-11"
NAKAYAMA = "20260927-nakayama-11"
NOW = datetime.datetime(2026, 9, 27, 23, 0, tzinfo=common.JST)


def _results() -> dict:
    return json.loads((FIX / "race_results.json").read_text(encoding="utf-8"))


def _run(tmp_path: Path, snapshot_dir: Path = FIX / "snapshots", prerace_dir: Path = FIX / "prerace"):
    out = tmp_path / "out"
    summary = fd.run(_results(), snapshot_dir=snapshot_dir, prerace_dir=prerace_dir,
                     output_dir=out / "diagnostics", summary_path=out / "diagnostics_summary.json", now=NOW)
    rows = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (out / "diagnostics").glob("*.json")}
    return summary, rows


def _copy_fixture(tmp_path: Path) -> tuple[Path, Path]:
    snap_dir, prerace_dir = tmp_path / "snapshots", tmp_path / "prerace"
    shutil.copytree(FIX / "snapshots", snap_dir)
    shutil.copytree(FIX / "prerace", prerace_dir)
    return snap_dir, prerace_dir


def _rewrite_snapshot(snap_dir: Path, prerace_dir: Path, race_id: str, mutate, rebind: bool) -> None:
    """snapshot を書き換える。rebind=True なら 14:57 の prerace record の SHA も新しい版へ付け替える。"""
    path = snap_dir / f"{race_id}.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    mutate(snapshot)
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    if rebind:
        rec_path = prerace_dir / race_id / "20260927T145701_pipeline.json"
        record = json.loads(rec_path.read_text(encoding="utf-8"))
        record["provenance"]["snapshot"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        rec_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ 1. 阪神 9/27

def test_hanshin_0927_pseudo_consensus_and_gen_conversion_loss(tmp_path: Path):
    _, rows = _run(tmp_path)
    row = rows[HANSHIN]
    assert row["status"] == "available"
    # SHA が一致する最新の発走前 record（14:57）を使い、不一致の 13:21 は使わない
    assert row["evidence"]["status"] == "prerace_record_matched"
    assert row["evidence"]["prerace_captured_at"] == "2026-09-27T14:57:01+09:00"
    assert row["evidence"]["snapshot_sha256"] == hashlib.sha256(
        (FIX / "snapshots" / f"{HANSHIN}.json").read_bytes()).hexdigest()
    assert row["evidence"]["post_at"] == "2026-09-27T15:20:00+09:00"

    ft = row["pre_race"]["fixed_three"]
    assert ft["axes"] == {"kei": 1, "tetsu": 1, "gen": 1}
    assert ft["axis_counts"] == {"1": 3} and ft["max_axis_agreement"] == 3 and ft["axis_all_same"] is True
    assert ft["dominant_axis"] == 1
    # 3連複100円も軸を含めば依存として数える: ケイ400 + 哲500 + 源500
    assert ft["dominant_axis_exposure_yen"] == 1400 and ft["total_stake_yen"] == 1500
    assert ft["dominant_axis_exposure_ratio"] == 0.9333
    assert ft["redundancy_flag"] is True
    assert set(ft["pairwise_rank_spearman"]) == {"kei-tetsu", "kei-gen", "tetsu-gen"}
    assert all(v is not None for v in ft["pairwise_rank_spearman"].values())

    iq = row["pre_race"]["input_quality"]
    assert iq["speed_degraded"] is True and iq["model_running_as_designed"] is False
    assert iq["speed_quality"]["used"] is False

    gen = row["post_race"]["cards"]["gen"]
    assert gen["selected_horses"] == [1, 2, 3, 8]
    assert gen["actual_top3_selected"] == [1, 8]
    assert gen["actual_top3_pairs_selected"] == [[1, 8]]
    # 3連複 1-3-8 / 1-2-8 は 1-8 を含むが「直接の2頭馬券」には数えない
    assert gen["actual_top3_pairs_directly_ticketed"] == []
    assert gen["winning_pair_present_but_not_ticketed"] is True
    assert "ticket_conversion_loss" in gen["failure_stage"]
    assert "character_selection_cut" in gen["failure_stage"]   # #4 は源の選定順6位（Top6内・買い目外）

    chappy = row["post_race"]["cards"]["chappy"]
    assert {1, 4} <= set(chappy["selected_horses"])
    assert chappy["actual_top3_pairs_directly_ticketed"] == [[1, 4]]
    assert chappy["winning_pair_present_but_not_ticketed"] is False
    assert "ticket_conversion_loss" not in chappy["failure_stage"]

    stage = row["post_race"]["chappy_stage"]
    assert stage["status"] == "evaluated"
    assert stage["actual_top3_role_pairs"] == [[1, 4]]
    assert stage["actual_top3_role_pairs_not_directly_ticketed"] == []
    assert {"role": "long_edge", "num": 4, "integrated_rank": 6, "actual_top3": True} in \
        stage["roles_outside_integrated_top4"]

    recall = row["post_race"]["recall"]
    assert recall["gen_sel"] == {"status": "available", "denominator": 3,
                                 "at4": 0.6667, "count4": 2, "at6": 1.0, "count6": 3}
    assert "ticket_conversion_loss" in row["post_race"]["failure_stage"]["race"]


# ------------------------------------------------------------------ 2. 中山 9/27

def test_nakayama_0927_axis_10_and_recall_at6_above_at4(tmp_path: Path):
    _, rows = _run(tmp_path)
    row = rows[NAKAYAMA]
    ft = row["pre_race"]["fixed_three"]
    assert ft["axes"] == {"kei": 10, "tetsu": 10, "gen": 10}
    assert ft["dominant_axis"] == 10 and ft["dominant_axis_exposure_ratio"] == 0.9333

    orders = row["pre_race"]["candidate_orders"]
    top3_order = orders["top3"]["order"]
    assert top3_order.index(5) + 1 == 3 and top3_order.index(6) + 1 == 4   # Top3順では #5/#6 が上位
    assert orders["base"]["order"].index(6) + 1 == 6

    recall = row["post_race"]["recall"]
    assert recall["top3"]["count4"] == 2
    assert recall["base"]["count4"] == 0 and recall["base"]["count6"] == 1
    assert recall["base"]["at6"] > recall["base"]["at4"]
    assert recall["gen_sel"]["at6"] > recall["gen_sel"]["at4"]

    fs = row["post_race"]["failure_stage"]
    horses = {h["num"]: h for h in fs["by_horse"]}
    assert horses[16]["labels"] == ["feature_miss"]           # どの順位でも Top6 圏外
    assert horses[6]["labels"] == ["base_rank_cut"]           # base 6位: Top6 内・Top4 圏外
    assert horses[5]["labels"] == []
    assert set(fs["race"]) >= {"feature_miss", "base_rank_cut", "character_selection_cut",
                               "chappy_integration_cut"}
    # #6 は condition=0.00 で integrated 7位（role外）。missing 処理そのものはこのPRで直さず、記録だけする
    assert "chappy_integration_cut" in fs["by_card"]["chappy"]
    assert row["post_race"]["chappy_stage"]["actual_top3_drops"]["integration_cut"] == 1


def test_fixture_snapshots_are_read_only_and_outputs_stay_in_diagnostics(tmp_path: Path):
    before = {p: p.read_bytes() for p in FIX.rglob("*.json")}
    summary, rows = _run(tmp_path)
    assert {p: p.read_bytes() for p in FIX.rglob("*.json")} == before
    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert written == ["out/diagnostics/20260927-hanshin-11.json", "out/diagnostics/20260927-nakayama-11.json",
                       "out/diagnostics_summary.json"]
    assert summary["official_results_untouched"] is True and summary["used_for_prediction"] is False
    fwd = summary["forward"]
    assert fwd["available_races"] == 2
    assert fwd["fixed_three_axis_all_same_count"] == 2 and fwd["fixed_three_axis_all_same_rate"] == 1.0
    assert fwd["speed_used_races"] == 0
    assert fwd["by_char"]["gen"]["conversion_loss_count"] == 1
    assert fwd["recall"]["base"] == {"races": 2, "mean_at4": 0.1667, "mean_at6": 0.3333}
    assert summary["errors"] == []


# ------------------------------------------------------------------ 3. SHA 不一致は fail-closed

def test_snapshot_sha_mismatch_fails_closed_without_rebuilding_selection(tmp_path: Path):
    snap_dir, prerace_dir = _copy_fixture(tmp_path)
    # 同じ内容でも1バイト違えば別の版。発走前 record は旧版の SHA のまま
    _rewrite_snapshot(snap_dir, prerace_dir, HANSHIN, lambda s: None, rebind=False)
    summary, rows = _run(tmp_path, snap_dir, prerace_dir)

    row = rows[HANSHIN]
    assert row["status"] == "unavailable" and row["reason"] == "prerace_record_unmatched"
    assert row["pre_race"] == {"status": "unavailable", "reason": "prerace_record_unmatched"}
    assert row["post_race"] == {"status": "unavailable", "reason": "prerace_record_unmatched"}
    text = json.dumps(row, ensure_ascii=False)
    for key in ("fixed_three", "candidate_orders", "recall", "cards", "chappy_stage"):
        assert key not in text          # 結果や snapshot から選定を作り直さない
    assert rows[NAKAYAMA]["status"] == "available"
    assert summary["forward"]["unavailable"] == {"prerace_record_unmatched": 1}


def test_missing_or_post_time_prerace_record_is_not_used(tmp_path: Path):
    snap_dir, prerace_dir = _copy_fixture(tmp_path)
    shutil.rmtree(prerace_dir / NAKAYAMA)
    # 阪神: SHA は一致するが発走後に取られた record しか無い
    for p in (prerace_dir / HANSHIN).glob("*.json"):
        p.unlink()
    late = json.loads((FIX / "prerace" / HANSHIN / "20260927T145701_pipeline.json").read_text(encoding="utf-8"))
    late["captured_at"] = "2026-09-27T15:21:00+09:00"
    (prerace_dir / HANSHIN / "20260927T152100_pipeline.json").write_text(
        json.dumps(late, ensure_ascii=False), encoding="utf-8")

    _, rows = _run(tmp_path, snap_dir, prerace_dir)
    assert rows[NAKAYAMA]["reason"] == "no_prerace_record"
    assert rows[HANSHIN]["reason"] == "no_prerace_record"


def test_fidelity_false_makes_only_sel_orders_unavailable(tmp_path: Path):
    snap_dir, prerace_dir = _copy_fixture(tmp_path)
    rec_path = prerace_dir / HANSHIN / "20260927T145701_pipeline.json"
    record = json.loads(rec_path.read_text(encoding="utf-8"))
    record["fidelity"]["recomputed_matches_snapshot"] = False
    rec_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    _, rows = _run(tmp_path, snap_dir, prerace_dir)
    recall = rows[HANSHIN]["post_race"]["recall"]
    for char in ("kei", "tetsu", "gen"):
        assert recall[f"{char}_sel"] == {"status": "unavailable", "reason": "prerace_fidelity_false"}
    assert recall["base"]["status"] == "available"
    assert all(v is None for v in rows[HANSHIN]["pre_race"]["fixed_three"]["pairwise_rank_spearman"].values())


# ------------------------------------------------------------------ 4. PASS カード

def test_pass_card_is_not_added_to_stake_or_conversion_denominators(tmp_path: Path):
    snap_dir, prerace_dir = _copy_fixture(tmp_path)

    def make_gen_pass(snapshot):
        for i, card in enumerate(snapshot["cards"]):
            if card["char"] == "gen":
                snapshot["cards"][i] = {"char": "gen", "action": "pass", "budget": 500, "total": 0, "bets": []}

    _rewrite_snapshot(snap_dir, prerace_dir, HANSHIN, make_gen_pass, rebind=True)
    summary, rows = _run(tmp_path, snap_dir, prerace_dir)
    row = rows[HANSHIN]
    assert row["status"] == "available"
    ft = row["pre_race"]["fixed_three"]
    assert ft["passed_chars"] == ["gen"] and ft["active_chars"] == ["kei", "tetsu"]
    assert ft["total_stake_yen"] == 1000 and ft["dominant_axis_exposure_yen"] == 900
    assert ft["axis_all_same"] is False                  # 3人揃っていないので「全員同じ軸」に数えない
    assert row["post_race"]["cards"]["gen"] == {"status": "pass"}
    assert "gen" not in row["post_race"]["failure_stage"]["by_card"]
    gen = summary["forward"]["by_char"]["gen"]
    assert gen["cards"] == 1 and gen["passes"] == 1        # 中山の1枚だけが分母
    assert gen["conversion_loss_count"] == 0
    assert summary["forward"]["fixed_three_full_races"] == 1


# ------------------------------------------------------------------ 5. 同一 pair の重複

def test_duplicate_pairs_are_normalized_to_one_unique_pair():
    card = {"char": "kei", "bets": [
        {"type": "ワイド", "horses": [1, 2], "amt": 200},
        {"type": "馬連", "horses": [2, 1], "amt": 100},
        {"type": "ワイド", "horses": [2, 1], "amt": 100},
        {"type": "3連複", "horses": [1, 2, 3], "amt": 100},
    ]}
    conv = fd.conversion(card, [1, 3, 9])
    assert conv["direct_pairs_ticketed"] == [[1, 2]] and conv["direct_pairs_ticketed_count"] == 1
    assert conv["direct_pairs_available_count"] == 3
    assert conv["direct_pair_coverage_ratio"] == 0.3333
    assert conv["actual_top3_pairs_selected"] == [[1, 3]]
    assert conv["winning_pair_present_but_not_ticketed"] is True   # 1-3 は3連複にしか無い
    assert conv["stake_yen"] == 500


def test_winning_trio_selected_but_not_ticketed():
    card = {"bets": [{"type": "ワイド", "horses": [1, 2], "amt": 100},
                     {"type": "ワイド", "horses": [1, 3], "amt": 100},
                     {"type": "ワイド", "horses": [2, 3], "amt": 100},
                     {"type": "3連複", "horses": [1, 2, 4], "amt": 100}]}
    conv = fd.conversion(card, [3, 2, 1])
    assert conv["all_top3_selected"] is True and conv["winning_trio_ticketed"] is False
    assert conv["winning_trio_selected_but_not_ticketed"] is True
    assert conv["winning_pair_present_but_not_ticketed"] is False


# ------------------------------------------------------------------ エラーを黙らない

def test_race_error_is_logged_with_race_id_and_recorded(tmp_path: Path, caplog):
    snap_dir, prerace_dir = _copy_fixture(tmp_path)

    def break_bets(snapshot):
        snapshot["cards"][0]["bets"][0]["horses"] = ["x", 2]

    _rewrite_snapshot(snap_dir, prerace_dir, HANSHIN, break_bets, rebind=True)
    with caplog.at_level(logging.ERROR, logger="research.forward_diagnostics"):
        summary, rows = _run(tmp_path, snap_dir, prerace_dir)
    assert any(HANSHIN in r.getMessage() for r in caplog.records)
    assert summary["errors"] and summary["errors"][0]["race_id"] == HANSHIN
    assert rows[HANSHIN]["status"] == "error"
    assert rows[NAKAYAMA]["status"] == "available"


# ------------------------------------------------------------------ Champion 不変・予想経路への漏れなし

def test_champion_predictions_are_byte_identical_and_prediction_path_never_reads_diagnostics(tmp_path: Path):
    raw = json.loads((ROOT / "docs" / "samples" / "raw.sample.json").read_text(encoding="utf-8"))
    base_times = json.loads((ROOT / "tests" / "fixtures" / "base_times.sample.json").read_text(encoding="utf-8"))
    configs = build_predictions.load_configs()

    def build() -> bytes:
        predictions = build_predictions.build_predictions(copy.deepcopy(raw), configs, base_times,
                                                          generated_at="2026-07-05T13:00:00+09:00")
        return json.dumps(predictions, ensure_ascii=False, sort_keys=True).encode("utf-8")

    before = build()
    _run(tmp_path)
    assert build() == before

    for directory in ("logic", "scraper"):
        for path in (ROOT / directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "forward_diagnostics" not in text and "diagnostics_summary" not in text, path
    assert "forward_diagnostics.json" not in model_registry.HASH_CONFIGS


def test_results_workflow_runs_diagnostics_after_settle_without_blocking():
    workflow = (ROOT / ".github" / "workflows" / "run_results.yml").read_text(encoding="utf-8")
    settle_at = workflow.index("python -m research.settle")
    diag_at = workflow.index("python -m research.forward_diagnostics")
    commit_at = workflow.index("生成物をコミット")
    assert settle_at < diag_at < commit_at
    step = workflow[workflow.rindex("- name:", 0, diag_at):diag_at]
    assert "continue-on-error: true" in step
    assert "tests/test_forward_diagnostics.py" in workflow
    pipeline = (ROOT / ".github" / "workflows" / "run_pipeline.yml").read_text(encoding="utf-8")
    assert "forward_diagnostics" not in pipeline


@pytest.mark.parametrize("race_id", [HANSHIN, NAKAYAMA])
def test_fixture_snapshots_match_the_prerace_record(race_id):
    record = json.loads((FIX / "prerace" / race_id / "20260927T145701_pipeline.json").read_text(encoding="utf-8"))
    sha = hashlib.sha256((FIX / "snapshots" / f"{race_id}.json").read_bytes()).hexdigest()
    assert record["provenance"]["snapshot"]["sha256"] == sha
