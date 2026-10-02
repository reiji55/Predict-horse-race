"""
speed-v2 Challenger の forward 評価（research/model_evaluation.py・speed-v2-forward-eval-v1）のテスト。

実データ（raw/2026-W39.json・data/race_results.json）で Champion と speed-v2 Challenger の発走前 snapshot を
一時ディレクトリに作り、capture → evaluate を通す。W39 は Challenger の本当の登録より前なので、
テストでは registry と評価式の registered_at を仮に早めて「forward のつもり」で動かす（仕組みの確認だけ）。
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
from research import model_evaluation as me

ROOT = Path(__file__).resolve().parent.parent
SPEED_V2 = "speed-base-times-v2-coverage"
BUILT = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=bp.JST)
EARLY = "2026-09-10T00:00:00+09:00"


def _registry(registered_at: str = EARLY) -> dict:
    registry = copy.deepcopy(model_registry.load_registry())
    next(m for m in registry["challengers"] if m["id"] == SPEED_V2)["registered_at"] = registered_at
    return registry


def _cfg(registered_at: str = EARLY) -> dict:
    cfg = me.load_config()
    cfg["registered_at"] = registered_at
    return cfg


def _raw() -> dict:
    return json.loads((ROOT / "raw" / "2026-W39.json").read_text(encoding="utf-8"))


def _results() -> dict:
    return json.loads((ROOT / "data" / "race_results.json").read_text(encoding="utf-8"))


@pytest.fixture()
def world(tmp_path: Path) -> dict:
    """両モデルの発走前 snapshot（同じビルド時刻）と capture を作る。"""
    registry = _registry()
    configs = bp.load_configs()
    base_times = json.loads((ROOT / "config" / "base_times.json").read_text(encoding="utf-8"))
    champion_dir = tmp_path / "snapshots"
    challenger_root = tmp_path / "challengers"
    challenger = next(m for m in registry["challengers"] if m["id"] == SPEED_V2)
    for spec, directory in ((registry["champion"], champion_dir), (challenger, challenger_root / SPEED_V2 / "snapshots")):
        predictions = bp.build_predictions(copy.deepcopy(_raw()), configs, base_times, model_spec=spec,
                                           generated_at=BUILT.isoformat())
        snapshots.freeze(predictions, now=BUILT, directory=directory)
    dirs = {"champion_dir": champion_dir, "challenger_root": challenger_root, "capture_dir": tmp_path / "capture"}
    report = me.capture(_raw(), now=BUILT, champion_dir=champion_dir, challenger_root=challenger_root,
                        output_dir=dirs["capture_dir"], cfg=_cfg(), registry=registry)
    return {**dirs, "registry": registry, "report": report, "tmp": tmp_path}


def _run(world: dict, cfg: dict | None = None, registry: dict | None = None, results: dict | None = None) -> dict:
    return me.run(results or _results(), champion_dir=world["champion_dir"], challenger_root=world["challenger_root"],
                  capture_dir=world["capture_dir"], output_dir=world["tmp"] / "out",
                  cfg=cfg or _cfg(), registry=registry or world["registry"])


# ------------------------------------------------------------------ capture（発走前）

def test_capture_records_both_models_with_full_precision_and_sel_orders(world):
    added = world["report"]["added"]
    assert {a["model_id"] for a in added} == {world["registry"]["champion"]["id"], SPEED_V2}
    assert len(added) == 8                                                   # 4レース × 2モデル
    path = next((world["capture_dir"] / SPEED_V2 / "20260927-hanshin-11").glob("*.json"))
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["fidelity"]["recomputed_matches_snapshot"] is True
    assert set(row["sel_orders"]) == {"kei", "tetsu", "gen"} and row["scores"]
    assert row["provenance"]["snapshot"]["base_times_ref"]["artifact_id"] == "base-times-v2-coverage-20260928-r2"

    again = me.capture(_raw(), now=BUILT + datetime.timedelta(minutes=5), champion_dir=world["champion_dir"],
                       challenger_root=world["challenger_root"], output_dir=world["capture_dir"],
                       cfg=_cfg(), registry=world["registry"])
    assert again["added"] == [] and {s["reason"] for s in again["skipped"]} == {"unchanged_snapshot"}


def test_capture_never_records_after_post(world):
    late = datetime.datetime(2026, 9, 28, 9, 0, tzinfo=bp.JST)
    report = me.capture(_raw(), now=late, champion_dir=world["champion_dir"], challenger_root=world["challenger_root"],
                        output_dir=world["tmp"] / "late", cfg=_cfg(), registry=world["registry"])
    assert report["added"] == [] and {s["reason"] for s in report["skipped"]} == {"already_posted"}


# ------------------------------------------------------------------ evaluate（対の評価）

def test_paired_evaluation_covers_every_forward_race(world):
    summary = _run(world)
    cov = summary["coverage"]
    assert cov["evaluated_pairs"] == cov["forward_races_with_result"] == 4 and cov["coverage_rate"] == 1.0
    assert cov["not_evaluated"] == [] and cov["excluded_pre_registration"] == []
    prob = summary["forward"]["primary"]["probability"]
    assert prob["races"] == 4 and prob["score_sources"] == ["capture_full_precision"]
    assert prob["log_loss"]["n"] == prob["brier"]["n"] == 4
    assert prob["log_loss"]["direction"] == "lower_is_better"
    assert summary["promotion"]["status"] == "not_registered"
    assert summary["official_results_untouched"] is True and summary["used_for_prediction"] is False

    race = json.loads((world["tmp"] / "out" / "races" / "20260927-hanshin-11.json").read_text(encoding="utf-8"))
    assert race["status"] == "evaluated" and race["sel_orders_paired"] is True
    assert race["frozen_at"] == BUILT.isoformat()
    assert race["models"]["challenger"]["snapshot"]["base_times_ref"]["artifact_id"] == "base-times-v2-coverage-20260928-r2"


def test_turf_speed_activation_is_compared_race_by_race_and_cumulatively(world):
    summary = _run(world)
    speed = summary["forward"]["primary"]["speed_quality"]
    assert speed["champion"]["by_surface"]["芝"]["used_races"] == 0
    assert speed["challenger"]["by_surface"]["芝"] == {**speed["challenger"]["by_surface"]["芝"],
                                                      "races": 3, "used_races": 3, "used_rate": 1.0}
    race = json.loads((world["tmp"] / "out" / "races" / "20260927-hanshin-11.json").read_text(encoding="utf-8"))
    assert race["models"]["champion"]["speed_quality"] == {**race["models"]["champion"]["speed_quality"],
                                                          "surface": "芝", "used": False, "qualified_horses": 0}
    assert race["models"]["challenger"]["speed_quality"]["qualified_horses"] == 15
    for key in ("used", "qualified_horses", "total_horses", "coverage", "raw_available_horses"):
        assert key in race["models"]["challenger"]["speed_quality"]


def test_ranking_and_diversity_are_recorded_for_both_models(world):
    summary = _run(world)
    ranking = summary["forward"]["primary"]["ranking"]
    for key in ("base_recall@4", "base_recall@6", "top3_recall@4", "top3_recall@6", "kei_sel_recall@4",
                "gen_sel_recall@6", "kei_card_selection_recall"):
        assert ranking[key]["n"] == 4 and ranking[key]["direction"] == "higher_is_better"
    div = summary["forward"]["primary"]["diversity"]
    assert set(div["champion"]["pairwise_rank_spearman"]) == {"kei-tetsu", "kei-gen", "tetsu-gen"}
    assert div["paired"]["axis_agreement"]["direction"] == "descriptive_only"
    assert "challenger_better" not in div["paired"]["jaccard"]
    race = json.loads((world["tmp"] / "out" / "races" / "20260926-nakayama-11.json").read_text(encoding="utf-8"))
    d = race["models"]["challenger"]["diversity"]
    for key in ("pairwise_rank_spearman", "max_axis_agreement", "dominant_axis", "dominant_axis_exposure_ratio",
                "bet_horse_jaccard"):
        assert key in d


def test_secondary_metrics_are_stored_but_kept_out_of_primary(world):
    summary = _run(world)
    assert "secondary" not in summary["forward"]["primary"]
    sec = summary["forward"]["secondary"]["challenger"]
    for key in ("fixed_three", "by_char", "auto_chappy", "largest_single_race_payout_share", "conversion_loss",
                "winning_coverage"):
        assert key in sec
    assert sec["fixed_three"]["roi"] is not None and sec["winning_coverage"]["wide_pairs_total"] == 12


def test_pre_registration_races_are_excluded_not_counted(world):
    cfg = _cfg("2026-09-27T00:00:00+09:00")
    summary = _run(world, cfg=cfg)
    cov = summary["coverage"]
    assert cov["excluded_pre_registration"] == ["20260926-hanshin-11", "20260926-nakayama-11"]
    assert cov["forward_races_with_result"] == 2 and cov["coverage_rate"] == 1.0
    assert summary["forward_start"] == "2026-09-27T00:00:00+09:00"
    assert not (world["tmp"] / "out" / "races" / "20260926-hanshin-11.json").exists()

    # Challenger の登録の方が遅ければ、そちらが境界になる
    later = _run(world, cfg=_cfg(), registry=_registry("2026-09-26T16:00:00+09:00"))
    assert later["forward_start"] == "2026-09-26T16:00:00+09:00"
    assert later["coverage"]["forward_races_with_result"] == 2


def test_missing_challenger_race_is_reported_in_coverage(world):
    (world["challenger_root"] / SPEED_V2 / "snapshots" / "20260926-hanshin-11.json").unlink()
    summary = _run(world)
    cov = summary["coverage"]
    assert cov["not_evaluated"] == [{"race_id": "20260926-hanshin-11", "reason": "challenger_missing"}]
    assert cov["forward_races_with_result"] == 4 and cov["evaluated_pairs"] == 3 and cov["coverage_rate"] == 0.75
    row = json.loads((world["tmp"] / "out" / "races" / "20260926-hanshin-11.json").read_text(encoding="utf-8"))
    assert row["status"] == "not_evaluated" and row["reason"] == "challenger_missing"


def test_pending_results_are_not_counted(world):
    results = {k: v for k, v in _results().items() if k != "20260927-nakayama-11"}
    cov = _run(world, results=results)["coverage"]
    assert cov["pending_result"] == ["20260927-nakayama-11"] and cov["forward_races_with_result"] == 3


def test_without_captures_both_models_fall_back_together(world):
    import shutil
    shutil.rmtree(world["capture_dir"] / SPEED_V2)
    summary = _run(world)
    assert summary["forward"]["primary"]["probability"]["score_sources"] == ["snapshot_marks_rounded"]
    ranking = summary["forward"]["primary"]["ranking"]
    assert ranking["kei_sel_recall@4"]["n"] == 0              # 片方だけの選定順では比べない
    assert ranking["base_recall@4"]["n"] == 4


# ------------------------------------------------------------------ 対にする条件

def _pair(world, race_id="20260927-hanshin-11"):
    champ = json.loads((world["champion_dir"] / f"{race_id}.json").read_text(encoding="utf-8"))
    chall = json.loads((world["challenger_root"] / SPEED_V2 / "snapshots" / f"{race_id}.json").read_text(encoding="utf-8"))
    post = snapshots.post_datetime({"id": race_id, "post_time": champ["post_time"]})
    challenger = next(m for m in world["registry"]["challengers"] if m["id"] == SPEED_V2)
    return champ, chall, post, challenger


@pytest.mark.parametrize("mutate, reason", [
    (lambda c, h: h.update(frozen_at="2026-09-01T09:05:00+09:00"), "frozen_at_mismatch"),
    (lambda c, h: h.update(config_hash="0" * 16), "config_hash_mismatch"),
    (lambda c, h: h["base_times_ref"].update(lookup_sha256="0" * 64), "challenger_artifact_mismatch"),
    (lambda c, h: h["marks"][0].update(odds=99.9), "prerace_inputs_differ"),
    (lambda c, h: (c.update(frozen_at="2026-09-27T16:00:00+09:00"), h.update(frozen_at="2026-09-27T16:00:00+09:00")),
     "champion_frozen_after_post"),
    (lambda c, h: h.update(model_id="other"), "challenger_model_id_mismatch"),
])
def test_pairing_requires_the_same_prerace_information(world, mutate, reason):
    champ, chall, post, challenger = _pair(world)
    assert me.pairing_problem(champ, chall, post, challenger, _cfg()) is None
    mutate(champ, chall)
    assert me.pairing_problem(champ, chall, post, challenger, _cfg()) == reason


# ------------------------------------------------------------------ 計算式

def test_probability_formulas():
    def snap(scores):
        return {"marks": [{"num": n, "score": s, "odds": o} for n, s, o in scores]}
    champ = snap([(1, 60.0, 2.0), (2, 50.0, 4.0), (3, 40.0, 4.0)])
    chall = snap([(1, 50.0, 2.0), (2, 60.0, 4.0), (3, 40.0, 4.0)])
    out = me.probability_block(champ, chall, {"champion": None, "challenger": None}, winner=2,
                               temperature=10.0, cfg=_cfg())
    z = math.exp(1) + 1 + math.exp(-1)
    p_champ = {1: math.exp(1) / z, 2: 1 / z, 3: math.exp(-1) / z}
    assert out["models"]["champion"]["log_loss"] == round(-math.log(p_champ[2]), 6)
    brier = (p_champ[1]) ** 2 + (p_champ[2] - 1) ** 2 + (p_champ[3]) ** 2
    assert out["models"]["champion"]["brier"] == round(brier, 6)
    assert out["models"]["challenger"]["p_winner"] == round(math.exp(1) / z, 6)
    assert out["models"]["q_market"]["p_winner"] == 0.25          # 1/4 ÷ (1/2+1/4+1/4)
    assert out["score_source"] == "snapshot_marks_rounded"


def test_probability_requires_coverage_and_evaluable_winner():
    champ = {"marks": [{"num": 1, "score": 60.0, "odds": 2.0}, {"num": 2, "score": 50.0, "odds": None},
                       {"num": 3, "score": 40.0, "odds": None}]}
    out = me.probability_block(champ, champ, {"champion": None, "challenger": None}, 1, 10.0, _cfg())
    assert out["status"] == "insufficient_coverage"


def test_calibration_buckets_and_paired_directions():
    rows = [{"probability": {"horses": [{"p_x": 0.01, "y": 0}, {"p_x": 0.12, "y": 1}, {"p_x": 1.0, "y": 1}]}}]
    buckets = me.calibration_buckets(rows, "p_x", [0.0, 0.02, 0.5, 1.0])
    assert [b["n"] for b in buckets] == [1, 1, 1] and buckets[2]["observed_win_rate"] == 1.0

    lower = me.paired([1.0, 2.0, None], [0.5, 2.5, 1.0], me.LOWER_IS_BETTER)
    assert lower["n"] == 2 and lower["challenger_better"] == 1 and lower["champion_better"] == 1
    higher = me.paired([0.3, 0.3], [0.6, 0.3], me.HIGHER_IS_BETTER)
    assert higher["challenger_better"] == 1 and higher["ties"] == 1
    desc = me.paired([1.0], [2.0], me.DESCRIPTIVE)
    assert desc["challenger_higher"] == 1 and "challenger_better" not in desc


# ------------------------------------------------------------------ 分離・事前登録

def test_evaluation_never_feeds_predictions(world, tmp_path):
    raw = json.loads((ROOT / "docs" / "samples" / "raw.sample.json").read_text(encoding="utf-8"))
    base_times = json.loads((ROOT / "tests" / "fixtures" / "base_times.sample.json").read_text(encoding="utf-8"))

    def build() -> str:
        return json.dumps(bp.build_predictions(copy.deepcopy(raw), bp.load_configs(), base_times,
                                               generated_at="2026-07-05T13:00:00+09:00"), sort_keys=True)
    before = build()
    _run(world)
    assert build() == before
    for directory in ("logic", "scraper", "results"):
        for path in (ROOT / directory).rglob("*.py"):
            assert "model_evaluation" not in path.read_text(encoding="utf-8"), path
    assert "model_evaluation.json" not in model_registry.HASH_CONFIGS


def test_spec_is_registered_before_the_first_forward_race():
    cfg = me.load_config()
    registered = datetime.datetime.fromisoformat(cfg["registered_at"])
    challenger = next(m for m in model_registry.load_registry()["challengers"] if m["id"] == SPEED_V2)
    first_forward = datetime.datetime.fromisoformat(challenger["registered_at"])
    assert registered < first_forward                     # 評価式は Challenger の forward 開始より前に固定
    assert cfg["challenger_model_id"] == SPEED_V2 and cfg["promotion"]["status"] == "not_registered"
    assert (ROOT / cfg["spec"]).exists()


def test_workflows_capture_before_post_and_evaluate_after_results_without_blocking():
    pipeline = (ROOT / ".github" / "workflows" / "run_pipeline.yml").read_text(encoding="utf-8")
    build_at = pipeline.index("python -m logic.build_predictions")
    capture_at = pipeline.index("python -m research.model_evaluation capture")
    assert build_at < capture_at
    step = pipeline[pipeline.rindex("- name:", 0, capture_at):capture_at]
    assert "continue-on-error: true" in step

    results = (ROOT / ".github" / "workflows" / "run_results.yml").read_text(encoding="utf-8")
    settle_at = results.index("python -m results.build_shadow_results")
    eval_at = results.index("python -m research.model_evaluation evaluate")
    assert settle_at < eval_at < results.index("生成物をコミット")
    step = results[results.rindex("- name:", 0, eval_at):eval_at]
    assert "continue-on-error: true" in step
    assert "tests/test_model_evaluation.py" in results


def test_secondary_failure_keeps_primary(world, monkeypatch):
    from results import build_results

    def broken(*args, **kwargs):
        raise ValueError("払戻データが壊れている")
    monkeypatch.setattr(build_results, "build_race_result", broken)
    summary = _run(world)
    assert summary["coverage"]["evaluated_pairs"] == 4 and summary["errors"] == []
    assert summary["forward"]["primary"]["probability"]["races"] == 4
    assert summary["forward"]["secondary"]["champion"]["not_settled_races"] == 4
