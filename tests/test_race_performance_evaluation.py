"""
PR-C2：race-performance-v1 の forward 評価（research/model_evaluation.py ＋ research/race_performance_eval.py）のテスト。

事前登録 docs/research/RACE_PERFORMANCE_PREREG_V1.md §7（評価）・§12（監査項目）の集計を確かめる。
実データ（raw/2026-W39.json・data/race_results.json）は登録より前の既知レースなので、仕組みの確認だけに使う
（registry と評価式の登録日時をテストの中だけ早める。指標の値そのものは見ない）。
"""
from __future__ import annotations

import copy
import datetime
import json
import math
from pathlib import Path

import pytest

from logic import build_predictions as bp
from logic import model_registry, snapshots
from research import common
from research import model_evaluation as me
from research import race_performance_eval as rps_eval

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "race-performance-v1"
EVAL_CONFIG = ROOT / "config" / "model_evaluation_race_performance_v1.json"
SUMMARY_CONFIG = ROOT / "config" / "model_evaluation_race_performance_v1_summary.json"
PREREG = json.loads((ROOT / "config" / "race_performance_v1.json").read_text(encoding="utf-8"))
BUILT = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=bp.JST)
EARLY = "2026-09-10T00:00:00+09:00"
# capture の照合キー（評価設定の evaluation_config_hash）。PR-C1 のマージから変えていないことの確認に使う
CAPTURE_KEY = "c72cc7bf042615c6b9ef6907be794573309580aa89c302459a1290ca64d85618"
PREREG_SHA = "592cf78fef99866839d1c385eded9a7388c7e6a5a7e5a1635d7bd9e902121b70"


def _registry() -> dict:
    registry = copy.deepcopy(model_registry.load_registry())
    next(m for m in registry["challengers"] if m["id"] == MODEL_ID)["registered_at"] = EARLY
    return registry


def _cfg() -> dict:
    cfg = me.load_config(EVAL_CONFIG)
    cfg["prereg_effective_at"] = cfg["registered_at"] = EARLY
    return cfg


def _summary_cfg() -> dict:
    return me.load_config(SUMMARY_CONFIG)


def _raw() -> dict:
    return json.loads((ROOT / "raw" / "2026-W39.json").read_text(encoding="utf-8"))


def _results() -> dict:
    return json.loads((ROOT / "data" / "race_results.json").read_text(encoding="utf-8"))


@pytest.fixture()
def world(tmp_path: Path) -> dict:
    """Champion と race-performance-v1 の発走前 snapshot（同じビルド時刻）と capture を作る。"""
    registry = _registry()
    champion_dir, challenger_root = tmp_path / "snapshots", tmp_path / "challengers"
    challenger = next(m for m in registry["challengers"] if m["id"] == MODEL_ID)
    for spec, directory in ((registry["champion"], champion_dir), (challenger, challenger_root / MODEL_ID / "snapshots")):
        predictions = bp.build_predictions(copy.deepcopy(_raw()), model_spec=spec, generated_at=BUILT.isoformat())
        snapshots.freeze(predictions, now=BUILT, directory=directory)
    dirs = {"champion_dir": champion_dir, "challenger_root": challenger_root, "capture_dir": tmp_path / "capture"}
    report = me.capture(_raw(), now=BUILT, champion_dir=champion_dir, challenger_root=challenger_root,
                        output_dir=dirs["capture_dir"], cfg=_cfg(), registry=registry)
    return {**dirs, "registry": registry, "report": report, "tmp": tmp_path}


def _run(world: dict, **kwargs) -> dict:
    options = {"cfg": _cfg(), "registry": world["registry"], "summary_cfg": _summary_cfg(), **kwargs}
    return me.run(_results(), champion_dir=world["champion_dir"], challenger_root=world["challenger_root"],
                  capture_dir=world["capture_dir"], output_dir=world["tmp"] / "out", **options)


def _capture_rows(world: dict, model_id: str) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((world["capture_dir"] / model_id).glob("*/*.json"))]


# ------------------------------------------------------------------ 発走前 capture の監査値

def test_only_the_challenger_capture_carries_the_prerace_audit(world):
    assert len(world["report"]["added"]) == 8                                # 4レース × 2モデル
    champion = _capture_rows(world, world["registry"]["champion"]["id"])
    challenger = _capture_rows(world, MODEL_ID)
    assert champion and all("race_performance_audit" not in r for r in champion)
    for row in challenger:
        audit = row["race_performance_audit"]
        assert audit["status"] == "recorded"
        assert audit["speed_rps_correlation"]["status"] in {"computed", "speed_not_used", "too_few_horses",
                                                            "constant_values"}
        assert 0 <= audit["non_jra_graded_usable_runs"] <= audit["graded_usable_runs"] <= audit["usable_runs"]


def _horse(speed, rps, runs=(), imputed=False):
    return {"speed_raw": speed, "speed_imputed": imputed, "race_performance_raw": rps, "_past_runs": list(runs)}


def _run_record(venue: str, cls: str, date: str = "2026-09-01") -> dict:
    return {"date": date, "surface": "芝", "dist": 1800, "class": cls, "margin_sec": 0.2, "venue": venue}


def test_capture_audit_measures_overlap_and_non_jra_graded_runs():
    race = {"id": "20261018-tokyo-11", "course": {"surface": "芝", "dist": 1800}}
    runs = [_run_record("大井", "g1"), _run_record("東京", "g2"), _run_record("中山", "3win"),
            _run_record("ロンシャン", "g1", "2026-08-01")]
    horses = [_horse(1.0, 0.2, runs), _horse(2.0, 0.4, runs), _horse(3.0, 0.5), _horse(4.0, 0.9),
              _horse(9.0, 0.1, imputed=True)]                                 # 補完した speed は相関に入れない
    audit = rps_eval.capture_audit(race, horses, PREREG)
    corr = audit["speed_rps_correlation"]
    assert corr["status"] == "computed" and corr["n"] == 4
    expected = __import__("statistics").correlation([1.0, 2.0, 3.0, 4.0], [0.2, 0.4, 0.5, 0.9])
    assert math.isclose(corr["r"], round(expected, 6))
    # 有効走は2頭 × 4走。重賞は2頭 × 3走、そのうち JRA 以外（大井・ロンシャン）は2頭 × 2走
    assert (audit["usable_runs"], audit["graded_usable_runs"], audit["non_jra_graded_usable_runs"]) == (8, 6, 4)

    off = rps_eval.capture_audit(race, [_horse(None, 0.2), _horse(None, 0.4)], PREREG)
    assert off["speed_rps_correlation"] == {"status": "speed_not_used", "n": 0, "r": None}
    few = rps_eval.capture_audit(race, [_horse(1.0, 0.2), _horse(2.0, None), _horse(3.0, 0.5)], PREREG)
    assert few["speed_rps_correlation"]["status"] == "too_few_horses"


# ------------------------------------------------------------------ 評価（結果確定後）

def test_rps_evaluation_reports_what_the_preregistration_requires(world):
    summary = _run(world)
    assert summary["coverage"]["evaluated_pairs"] == summary["coverage"]["forward_races_with_result"] == 4
    assert summary["challenger_race_performance"] == next(
        m for m in world["registry"]["challengers"] if m["id"] == MODEL_ID)["race_performance"]
    assert summary["summary_config"]["version"] == "race-performance-v1-summary-v1"
    assert "race-performance-v1" in summary["note"] and "speed-v2" not in summary["note"]
    assert summary["promotion"]["status"] == "not_registered"

    block = summary["race_performance"]
    always = block["always_report"]
    assert always["paired_valid_race_rate"] == 1.0 and always["score_coverage"]["races"] == 4
    assert always["forward_races_with_result"] == always["evaluated_pairs"] == always["probability_pairs"] == 4
    assert always["probability_paired_valid_race_rate"] == always["probability_evaluated_rate"] == 1.0
    assert 0.0 < always["rps_coverage"]["mean"] <= 1.0
    for metric in ("log_loss", "brier"):
        boot = block["uncertainty"][metric]
        assert boot["status"] == "computed" and boot["cluster"] == "race_date" and boot["clusters"] == 2
        assert boot["interval"][0] <= boot["mean_delta"] <= boot["interval"][1]
        paired = summary["forward"]["primary"]["probability"][metric]
        assert math.isclose(boot["mean_delta"], paired["mean_delta_challenger_minus_champion"], abs_tol=1e-6)
    checkpoint = block["checkpoint"]
    assert checkpoint["first_checkpoint_paired_races"] == 40 and checkpoint["reached"] is False
    assert checkpoint["counted"] == "probability_pairs"
    assert "昇格の閾値ではない" in checkpoint["note"]
    audit = block["audit"]
    assert audit["factor_active_races"] + sum(audit["factor_inactive_reasons"].values()) == 4
    assert audit["speed_rps_correlation"]["races_recorded"] == 4
    assert audit["speed_rps_correlation"]["races_not_recorded"] == 0

    race = json.loads((world["tmp"] / "out" / "races" / "20260927-hanshin-11.json").read_text(encoding="utf-8"))
    rps = race["models"]["challenger"]["race_performance"]
    assert rps["ref"]["config_sha256"] == PREREG_SHA
    assert rps["capture_audit"]["status"] == "recorded"
    assert "race_performance" not in race["models"]["champion"]
    expected = 0.15 if rps["speed_used"] else round(0.15 / (0.15 + 0.255 + 0.2125), 6)
    assert rps["effective_rps_weight"] == (expected if rps["factor_active"] else 0.0)



def test_preregistered_evaluation_requires_its_summary_settings(world):
    with pytest.raises(ValueError, match="summary-config"):
        _run(world, summary_cfg=None)
    wrong = {**_summary_cfg(), "evaluation_version": "something-else"}
    with pytest.raises(ValueError, match="向けではありません"):
        _run(world, summary_cfg=wrong)
    # speed-v2 の評価にこの集計を当てない
    speed_cfg = me.load_config()
    speed_cfg["registered_at"] = EARLY
    with pytest.raises(ValueError, match="向けではありません"):
        _run(world, cfg=speed_cfg, summary_cfg={**_summary_cfg(), "evaluation_version": speed_cfg["version"]})


def test_snapshot_built_from_another_raw_is_not_evaluated(world):
    # 両モデルの capture の raw の指紋だけを揃えて書き換える（モデル間の照合は通る）。
    # Challenger の snapshot に記録した入力 raw と食い違うので、§7.4 のモデルごとの照合で落とす
    for model_id in (world["registry"]["champion"]["id"], MODEL_ID):
        for path in (world["capture_dir"] / model_id / "20260927-hanshin-11").glob("*.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            row["provenance"]["input_raw_sha256"] = "f" * 64
            path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    summary = _run(world)
    assert {"race_id": "20260927-hanshin-11", "reason": "snapshot_input_raw_mismatch"} \
        in summary["coverage"]["not_evaluated"]
    assert summary["coverage"]["evaluated_pairs"] == 3


def test_snapshot_input_problem_rules():
    snap = {"race_performance_quality": {"input_raw_hash": "a" * 64}}
    capture = {"provenance": {"input_raw_sha256": "a" * 64}}
    assert rps_eval.snapshot_input_problem(snap, capture) is None
    assert rps_eval.snapshot_input_problem({}, capture) == "race_performance_quality_missing"
    assert rps_eval.snapshot_input_problem(snap, None) == "capture_missing"
    other = {"provenance": {"input_raw_sha256": "b" * 64}}
    assert rps_eval.snapshot_input_problem(snap, other) == "snapshot_input_raw_mismatch"


def _synthetic_row(i: int, probability_ok: bool) -> dict:
    """summary_block に渡す evaluated 行（合成）。4つの開催日に散らす。"""
    day = ("20261010", "20261011", "20261017", "20261018")[i % 4]
    probability = {"status": "evaluated" if probability_ok else "score_set_mismatch",
                   "evaluation_set": {"field_size": 16, "scored_horses": 16, "coverage": 1.0}}
    if probability_ok:
        probability["models"] = {"champion": {"log_loss": 2.0, "brier": 0.9},
                                 "challenger": {"log_loss": 2.0 + (i % 5 - 2) * 0.01, "brier": 0.9}}
    rps = {"factor_active": True, "rps_coverage": 0.9, "rps_available_horses": 14, "max_abs_z": 2.0,
           "speed_used": False, "effective_rps_weight": 0.242915, "excluded_by_reason_total": {},
           "capture_audit": {"status": "not_recorded"}}
    return {"race_id": f"{day}-tokyo-{i:02d}", "probability": probability,
            "models": {"challenger": {"race_performance": rps}}}


@pytest.mark.parametrize("probability_pairs, reached", [(25, False), (40, True)])
def test_checkpoint_counts_only_pairs_with_the_primary_metric(probability_pairs, reached):
    # 40 レースで比較は成立したが、主指標（確率の paired log loss）を算出できたのが 25 レースだけなら、
    # 「40 paired races」には届いていない（順位・カードだけ比べられたレースは数えない）
    rows = [_synthetic_row(i, i < probability_pairs) for i in range(40)]
    coverage = {"forward_races_with_result": 50, "coverage_rate": round(40 / 50, 4)}
    block = rps_eval.summary_block(rows, coverage, me.load_config(EVAL_CONFIG), _summary_cfg(), PREREG)
    checkpoint = block["checkpoint"]
    assert checkpoint["counted"] == "probability_pairs"
    assert (checkpoint["evaluated_pairs"], checkpoint["probability_pairs"]) == (40, probability_pairs)
    assert checkpoint["reached"] is reached
    always = block["always_report"]
    assert always["paired_valid_race_rate"] == 0.8                                   # 比較が成立した率
    assert always["probability_paired_valid_race_rate"] == round(probability_pairs / 50, 4)  # 主指標を算出できた率
    assert always["probability_evaluated_rate"] == round(probability_pairs / 40, 4)  # 比較が成立した中での率
    assert set(always["rate_definitions"]) == {"paired_valid_race_rate", "probability_paired_valid_race_rate",
                                               "probability_evaluated_rate"}
    for metric in ("log_loss", "brier"):
        assert block["uncertainty"][metric]["races"] == probability_pairs           # bootstrap も主指標の対だけ


def test_rates_without_forward_races_are_null():
    block = rps_eval.summary_block([], {"forward_races_with_result": 0, "coverage_rate": None},
                                   me.load_config(EVAL_CONFIG), _summary_cfg(), PREREG)
    always = block["always_report"]
    assert always["probability_paired_valid_race_rate"] is None and always["probability_evaluated_rate"] is None
    assert block["checkpoint"]["reached"] is False and block["checkpoint"]["probability_pairs"] == 0


# ------------------------------------------------------------------ 集計の部品

SETTINGS = {"cluster": "race_date", "replicates": 2000, "seed": 7, "interval": [0.025, 0.975], "min_clusters": 2}


def test_cluster_bootstrap_resamples_whole_meeting_days_with_a_fixed_seed():
    deltas = [("20261010", 0.2), ("20261010", 0.4), ("20261011", -0.1), ("20261017", 0.1)]
    first = rps_eval.cluster_bootstrap(deltas, SETTINGS)
    assert first == rps_eval.cluster_bootstrap(deltas, SETTINGS)          # 種を固定しているので同じ区間
    assert first["status"] == "computed" and first["clusters"] == 3 and first["races"] == 4
    assert first["mean_delta"] == round((0.2 + 0.4 - 0.1 + 0.1) / 4, 6)   # レース数で重み付けした平均
    # 同じ日のレースは一緒に抜き出すので、区間は日ごとの平均（0.3・−0.1・0.1）の範囲に収まる
    assert -0.1 <= first["interval"][0] <= first["interval"][1] <= 0.3

    constant = rps_eval.cluster_bootstrap([("20261010", 0.05), ("20261011", 0.05)], SETTINGS)
    assert constant["interval"] == [0.05, 0.05]
    one_day = rps_eval.cluster_bootstrap([("20261010", 0.1), ("20261010", 0.3)], SETTINGS)
    assert one_day["status"] == "too_few_clusters" and one_day["interval"] is None
    assert rps_eval.cluster_bootstrap([], SETTINGS)["mean_delta"] is None


def test_percentile_uses_linear_interpolation():
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert rps_eval._percentile(values, 0.25) == 1.0
    assert rps_eval._percentile(values, 0.5) == 2.0
    assert rps_eval._percentile([0.0, 1.0], 0.975) == pytest.approx(0.975)


def test_effective_weight_and_z_expansion():
    active = {"factor_active": True}
    assert rps_eval.effective_rps_weight(active, True, PREREG) == 0.15
    assert rps_eval.effective_rps_weight(active, False, PREREG) == round(0.15 / 0.6175, 6)
    assert rps_eval.effective_rps_weight({"factor_active": False}, True, PREREG) == 0.0
    quality = {"horses": [{"rps_raw": 0.2}, {"rps_raw": 0.4}, {"rps_raw": None}, {"rps_raw": 0.9}]}
    from logic import base_score
    expected = max(abs(z) for z in base_score.z_standardize([0.2, 0.4, 0.9]))
    assert rps_eval.max_abs_z(quality) == round(expected, 6)
    assert rps_eval.max_abs_z({"horses": [{"rps_raw": None}]}) is None


# ------------------------------------------------------------------ 既存の評価・設定・workflow

def test_speed_v2_summary_has_no_rps_blocks(world):
    # speed-v2 の評価（設定に prereg_config が無い）は、集計の設定なしで従来どおり動き、RPS の項目を足さない
    speed_cfg = me.load_config()
    assert "prereg_config" not in speed_cfg and me.capture_dir_for(speed_cfg) == me.CAPTURE_DIR
    summary = _run(world, cfg=speed_cfg, summary_cfg=None)
    assert not {"race_performance", "challenger_race_performance", "summary_config"} & set(summary)
    assert "speed-v2" in summary["note"]


def test_the_capture_key_is_unchanged_and_the_summary_settings_are_separate():
    cfg = me.load_config(EVAL_CONFIG)
    assert common.config_hash(cfg) == CAPTURE_KEY                       # 変えると PR-C1 以降の capture が使えなくなる
    settings = _summary_cfg()
    assert settings["evaluation_version"] == cfg["version"]
    assert settings["evaluation_config"] == "config/model_evaluation_race_performance_v1.json"
    assert settings["uncertainty"]["cluster"] == "race_date" and settings["uncertainty"]["metrics"] == ["log_loss", "brier"]
    assert cfg["primary_metric"] == "log_loss" and cfg["key_secondary_metric"] == "brier"


def test_results_workflow_evaluates_rps_after_scoring_without_blocking():
    results = (ROOT / ".github" / "workflows" / "run_results.yml").read_text(encoding="utf-8")
    command = "--summary-config config/model_evaluation_race_performance_v1_summary.json"
    at = results.index(command)
    step = results[results.rindex("- name:", 0, at):at]
    assert "continue-on-error: true" in step
    assert "--config config/model_evaluation_race_performance_v1.json" in step
    assert results.index("python -m results.build_shadow_results") < at < results.index("生成物をコミット")
    assert "tests/test_race_performance.py" in results and "tests/test_race_performance_evaluation.py" in results
