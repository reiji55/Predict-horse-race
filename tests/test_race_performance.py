"""PR-C1：Challenger race-performance-v1（事前登録 docs/research/RACE_PERFORMANCE_PREREG_V1.md）のテスト。

- 事前登録した式どおりに計算する（§4.6 の検算例・除外理由と順番・入力位置での重み・null）
- 識別力の無いレースでは因子ごと無効にし、score・p・カードが Champion と同じになる（§5.2）
- 変わり得る経路と変わらない経路（§5.3）
- Champion と既存の Challenger は計算値・カード・金額が変わらない（metadata の差だけ）
- 設定の照合と fail-closed（記録あり）・snapshot の監査記録・発走前 capture の一致

既知のレース（W39・W40）は探索・バグ回帰専用。ここでは仕組みの確認にだけ使い、成績は見ない。
"""
from __future__ import annotations

import copy
import datetime
import json
import math
import re
import shutil
import sys
from pathlib import Path

import pytest

from logic import base_score, cards, model_registry, snapshots
from logic import build_predictions as bp
from logic import race_performance as rp
from research import common
from research import model_evaluation as me
from research import prerace_capture

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "race-performance-v1"
CONFIG = json.loads((ROOT / "config" / "race_performance_v1.json").read_text(encoding="utf-8"))
EVAL_CONFIG = ROOT / "config" / "model_evaluation_race_performance_v1.json"
TODAY = {"surface": "芝", "dist": 1800}
RACE_DAY = datetime.date(2026, 10, 18)
BUILT = datetime.datetime(2026, 9, 1, 9, 0, tzinfo=bp.JST)
METADATA_KEYS = {"config_hash", "git_commit", "generated_at"}


def _run(date="2026-09-01", surface="芝", dist=1800, cls="g2", margin=0.3, **extra):
    return {"date": date, "surface": surface, "dist": dist, "class": cls, "margin_sec": margin, **extra}


def _spec() -> dict:
    return copy.deepcopy(next(m for m in model_registry.load_registry()["challengers"] if m["id"] == MODEL_ID))


def _raw(week: str = "2026-W40") -> dict:
    return json.loads((ROOT / "raw" / f"{week}.json").read_text(encoding="utf-8"))


def _strip(value):
    """比較から外すもの：config_hash・git_commit・生成時刻（metadata）と、モデル名（model_id・model_role・
    カードの model_version）。"""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items()
                if k not in METADATA_KEYS | {"model_id", "model_role", "model_version", "model"}}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


# ------------------------------------------------------------------ 1走の値・集約（§4）

def test_worked_examples_from_the_preregistration():
    assert rp.run_value("g1", 0.0, CONFIG) == pytest.approx(1.0)
    assert rp.run_value("3win", 0.0, CONFIG) == pytest.approx(0.55)
    assert round(rp.run_value("g2", 0.1, CONFIG), 6) == 0.814354
    assert round(rp.run_value("g2", 2.0, CONFIG), 6) == 0.121802
    # G1 の 0.0秒差4着は 3勝クラスの 0.0秒差1着より高い。G2 の 0.1秒差は 2.0秒差より高い
    assert rp.run_value("g1", 0.0, CONFIG) > rp.run_value("3win", 0.0, CONFIG)
    assert rp.run_value("g2", 0.1, CONFIG) > rp.run_value("g2", 2.0, CONFIG)

    runs = [
        _run(cls="g1", margin=0.0, dist=1800),
        _run(surface="ダ"),                                   # surface_mismatch
        _run(cls="3win", margin=0.5, dist=2000),
        _run(cls=None),                                       # class_unknown
        _run(cls="op", margin=1.2, dist=2200),
    ]
    result = rp.horse_rps(runs, TODAY, RACE_DAY, CONFIG)
    assert round(result["rps_raw"], 6) == 0.66495
    assert [u["position"] for u in result["usable_runs"]] == [0, 2, 4]
    # 除外した走を詰めず、入力位置のまま重みを付ける
    assert [u["w_recency"] for u in result["usable_runs"]] == [1.0, 0.8, 0.6]
    assert [u["w_dist"] for u in result["usable_runs"]] == [1.0, 0.75, 0.5]
    assert result["excluded_by_reason"] == {"surface_mismatch": 1, "class_unknown": 1}
    assert result["n_input_runs"] == 5 and result["n_usable"] == 3


def test_fewer_than_two_usable_runs_is_null_not_guessed():
    one = rp.horse_rps([_run(), _run(surface="ダ")], TODAY, RACE_DAY, CONFIG)
    assert one["rps_raw"] is None and one["n_usable"] == 1
    assert rp.horse_rps([], TODAY, RACE_DAY, CONFIG)["rps_raw"] is None


@pytest.mark.parametrize("run, reason", [
    (_run(date=None), "date_invalid"),
    (_run(date="不明"), "date_invalid"),
    (_run(date="2026-10-18"), "same_day"),
    (_run(date="2026-10-25"), "future"),
    (_run(surface="障"), "run_not_flat"),
    (_run(surface="ダ"), "surface_mismatch"),
    (_run(dist=None), "dist_missing"),
    (_run(dist=2201), "dist_out_of_range"),
    (_run(cls="jg1"), "class_unknown"),
    (_run(margin=None), "margin_missing"),
    (_run(margin=float("nan")), "margin_invalid"),
    (_run(margin=float("inf")), "margin_invalid"),
    (_run(margin="0.3"), "margin_invalid"),
    (_run(margin=True), "margin_invalid"),
])
def test_each_exclusion_reason(run, reason):
    assert rp.exclusion_reason(0, run, TODAY, RACE_DAY, CONFIG) == reason


def test_exclusion_reasons_follow_the_registered_order():
    assert rp.exclusion_reason(5, _run(), TODAY, RACE_DAY, CONFIG) == "beyond_max_runs"
    # 日付も surface も悪い走は、先に見る日付の理由だけを数える
    assert rp.exclusion_reason(0, _run(date=None, surface="ダ"), TODAY, RACE_DAY, CONFIG) == "date_invalid"
    assert rp.exclusion_reason(0, _run(surface="障"), {"surface": "障", "dist": 3000}, RACE_DAY,
                               CONFIG) == "today_not_flat"
    assert rp.exclusion_reason(0, _run(dist=2200), TODAY, RACE_DAY, CONFIG) is None   # 400m ちょうどは有効
    order = CONFIG["usable_run"]["exclusion_reasons"]
    assert order[:4] == ["beyond_max_runs", "date_invalid", "same_day", "future"]


def test_negative_margin_counts_as_zero_and_is_flagged():
    result = rp.horse_rps([_run(margin=-0.2), _run(margin=0.0)], TODAY, RACE_DAY, CONFIG)
    first = result["usable_runs"][0]
    assert first["margin_clamped_negative"] is True and first["v_run"] == pytest.approx(0.9)
    assert result["margin_clamped_negative_runs"] == 1
    # 大きな有限値は除外せず、exp で 0 に近づくだけ（NaN や −Inf にならない）
    big = rp.run_value("g1", 1e6, CONFIG)
    assert big == 0.0 and not math.isnan(big)


def test_rps_ignores_finish_and_odds():
    runs = [_run(margin=0.1), _run(cls="g3", margin=0.4, date="2026-08-01")]
    a = rp.horse_rps([{**r, "finish": 1} for r in runs], TODAY, RACE_DAY, CONFIG)
    b = rp.horse_rps([{**r, "finish": 15, "win_odds": 999.0} for r in runs], TODAY, RACE_DAY, CONFIG)
    assert a["rps_raw"] == b["rps_raw"]


# ------------------------------------------------------------------ 因子ごと無効にする（§5.2）

def test_factor_activation_rules():
    assert rp.factor_activation([None, 0.4], CONFIG)[:2] == (False, rp.INACTIVE_TOO_FEW)
    assert rp.factor_activation([None, None], CONFIG)[:2] == (False, rp.INACTIVE_TOO_FEW)
    assert rp.factor_activation([0.5, 0.5, None], CONFIG)[:2] == (False, rp.INACTIVE_ZERO_SD)
    active, reason, sd = rp.factor_activation([0.5, 0.6, None], CONFIG)
    assert active is True and reason is None and sd > 0


def _race_with_identical_rps() -> dict:
    """全馬に同じ過去2走を与えたレース（RPS が全馬同じ値＝sd=0）。"""
    race = copy.deepcopy(_raw()["races"][3])
    course = race["course"]
    same = [_run(date="2026-08-01", surface=course["surface"], dist=course["dist"], cls="g2", margin=0.3),
            _run(date="2026-06-01", surface=course["surface"], dist=course["dist"], cls="g3", margin=0.1)]
    for entry in race["entries"]:
        entry["past_runs"] = copy.deepcopy(same)
    return race


def _build(race: dict, spec: dict) -> dict:
    raw = {"races": [race]}
    configs = bp.load_configs()
    rates = bp._overall_rates(raw)
    from logic import speed_index
    return bp.build_race(copy.deepcopy(race), configs, speed_index.load_base_times(), *rates, model_spec=spec)


def test_inactive_factor_gives_exactly_the_champion_scores_p_and_cards():
    race = _race_with_identical_rps()
    champion = _build(race, model_registry.load_registry()["champion"])
    challenger = _build(race, _spec())
    quality = challenger["race_performance_quality"]
    assert quality["factor_active"] is False and quality["factor_inactive_reason"] == "zero_sd"
    assert quality["rps_available_horses"] == len(race["entries"])
    for key in ("marks", "cards", "myomi", "myomi_parts", "chappy_decision", "legendary", "card_ev_myomi"):
        assert _strip(challenger[key]) == _strip(champion[key]), key


def test_active_factor_changes_scores_but_not_the_character_specific_three_factor_paths():
    """§5.3：印・p は変わり得る。ケイ・源さんの sel_score・sel_p（キャラ固有の3因子）と q・T は変わらない。"""
    race = copy.deepcopy(_raw()["races"][3])
    configs = bp.load_configs()
    from logic import speed_index
    base_times = speed_index.load_base_times()
    rates = bp._overall_rates({"races": [race]})
    loaded = model_registry.load_model_race_performance(_spec())

    def horses_for(config):
        horses, _ = bp.prepare_horses(copy.deepcopy(race), configs, base_times, *rates)
        quality = bp.compute_model_base_scores(race, horses, configs["cards"], config)
        from logic import prob_model
        p = prob_model.softmax_scores([h["score"] for h in horses], configs["myomi"]["prob_model"]["temperature"])
        q = prob_model.market_support([h["odds"] for h in horses])
        for horse, p_i, q_i in zip(horses, p, q):
            horse["p"], horse["q"] = p_i, q_i
        cards.assign_marks(horses, configs["cards"])
        return horses, quality

    champion, _ = horses_for(None)
    challenger, quality = horses_for(loaded[0])
    assert quality["factor_active"] is True
    assert [h["score"] for h in champion] != [h["score"] for h in challenger]      # 共通の score は変わる
    assert [h["q"] for h in champion] == [h["q"] for h in challenger]              # q は変わらない
    for char in ("kei", "gen"):
        char_config = configs["cards"]["characters"][char]
        for horses in (champion, challenger):
            cards.assign_character_ranks(horses, configs["cards"], char_config,
                                         configs["myomi"]["prob_model"]["temperature"])
        assert [h["sel_score"] for h in champion] == [h["sel_score"] for h in challenger], char
        assert [h["sel_p"] for h in champion] == [h["sel_p"] for h in challenger], char
    # 哲さん（上書き無し）は共通の score をそのまま使うので変わり得る
    tetsu = configs["cards"]["characters"]["tetsu"]
    for horses in (champion, challenger):
        cards.assign_character_ranks(horses, configs["cards"], tetsu, configs["myomi"]["prob_model"]["temperature"])
    assert [h["sel_score"] for h in challenger] == [h["score"] for h in challenger]


def test_gen_axis_floor_reads_the_common_base_rank():
    """§5.3：源さんの軸の制約は共通の base_rank を見る（RPS で共通の順位が変われば源さんの軸も変わり得る）。"""
    def horses(common_ranks):
        return [{"num": n, "sel_base_rank": n, "sel_value_rank": 1 if n == 1 else n, "base_rank": r}
                for n, r in zip((1, 2, 3), common_ranks)]
    first = cards.select_horses(horses([9, 1, 2]), 0.75, axis_base_rank_floor=6,
                                base_rank_key="sel_base_rank", value_rank_key="sel_value_rank")
    second = cards.select_horses(horses([1, 2, 3]), 0.75, axis_base_rank_floor=6,
                                 base_rank_key="sel_base_rank", value_rank_key="sel_value_rank")
    assert first[0]["num"] != second[0]["num"]


def test_uncertain_flag_is_not_raised_by_missing_rps():
    horses = [{"speed_raw": 1.0, "aptitude_raw": 0.5, "human_raw": 0.2, "race_performance_raw": None},
              {"speed_raw": 2.0, "aptitude_raw": 0.4, "human_raw": 0.3, "race_performance_raw": 0.7},
              {"speed_raw": 1.5, "aptitude_raw": 0.6, "human_raw": 0.1, "race_performance_raw": 0.6}]
    base_score.compute_base_scores(horses, bp.load_configs()["cards"],
                                   score_weights=CONFIG["integration"]["score_weights"],
                                   factor_keys=base_score.RACE_PERFORMANCE_FACTOR_KEYS)
    assert [h["uncertain"] for h in horses] == [False, False, False]


# ------------------------------------------------------------------ Champion と既存の Challenger は変わらない

def test_champion_and_existing_challengers_are_unchanged_except_metadata(tmp_path: Path, monkeypatch):
    """models.json から race-performance-v1 を外した config と比べ、計算値・カード・金額が完全に同じ。"""
    before_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", before_dir)
    registry = json.loads((before_dir / "models.json").read_text(encoding="utf-8"))
    registry["challengers"] = [m for m in registry["challengers"] if m["id"] != MODEL_ID]
    (before_dir / "models.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")

    def outputs(config_dir: Path) -> dict:
        monkeypatch.setattr(model_registry, "CONFIG_DIR", config_dir)
        reg = model_registry.load_registry(config_dir / "models.json")
        specs = [reg["champion"]] + [m for m in model_registry.enabled_challengers(reg) if m["id"] != MODEL_ID]
        return {spec["id"]: bp.build_predictions(_raw(), model_spec=spec, generated_at="X") for spec in specs}

    before, after = outputs(before_dir), outputs(ROOT / "config")
    assert set(before) == set(after) and len(after) == 4
    for model_id in after:
        assert _strip(after[model_id]) == _strip(before[model_id]), model_id
        assert after[model_id]["model"]["config_hash"] != before[model_id]["model"]["config_hash"]
        assert "race_performance_ref" not in after[model_id]["model"]
        assert all("race_performance_quality" not in r for r in after[model_id]["races"])


# ------------------------------------------------------------------ 設定の照合（fail-closed）

def test_registered_config_is_loaded_and_pinned():
    config, ref = model_registry.load_model_race_performance(_spec())
    assert ref == {"version": "race-performance-v1", "config_file": "config/race_performance_v1.json",
                   "config_sha256": "592cf78fef99866839d1c385eded9a7388c7e6a5a7e5a1635d7bd9e902121b70"}
    assert config["integration"]["score_weights"]["race_performance"] == 0.15


@pytest.mark.parametrize("mutate, message", [
    (lambda s: s["race_performance"].update(config_sha256="0" * 64), "設定が登録と違います"),
    (lambda s: s["race_performance"].update(version="race-performance-v2"), "version"),
    (lambda s: s["race_performance"].update(config_file="../config/race_performance_v1.json"), "config/ の直下"),
    (lambda s: s.update(id="other-challenger"), "別のモデル向け"),
])
def test_config_mismatch_fails_closed(mutate, message):
    spec = _spec()
    mutate(spec)
    with pytest.raises(ValueError, match=message):
        model_registry.load_model_race_performance(spec)


def test_changed_champion_weights_fail_closed(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_dir)
    cards_path = config_dir / "cards.json"
    data = json.loads(cards_path.read_text(encoding="utf-8"))
    data["score_weights"]["speed"] = 0.5
    cards_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(model_registry, "CONFIG_DIR", config_dir)
    with pytest.raises(ValueError, match="既存3因子の重み"):
        model_registry.load_model_race_performance(_spec())


@pytest.mark.parametrize("mutate, message", [
    (lambda r: r["champion"].update(race_performance=_spec()["race_performance"]), "champion"),
    (lambda r: next(m for m in r["challengers"] if m["id"] == MODEL_ID)["race_performance"].pop("config_sha256"),
     "config_sha256"),
])
def test_registry_rejects_invalid_race_performance(tmp_path: Path, mutate, message):
    registry = json.loads((ROOT / "config" / "models.json").read_text(encoding="utf-8"))
    mutate(registry)
    path = tmp_path / "models.json"
    path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        model_registry.load_registry(path)


def test_broken_challenger_is_recorded_and_does_not_stop_the_champion(tmp_path: Path, monkeypatch):
    registry = copy.deepcopy(model_registry.load_registry())
    broken = next(m for m in registry["challengers"] if m["id"] == MODEL_ID)
    broken["race_performance"]["config_sha256"] = "0" * 64
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "2026-W40.json").write_text(json.dumps(_raw(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bp, "RAW_DIR", raw_dir)
    monkeypatch.setattr(bp, "OUTPUT_PATH", tmp_path / "predictions.json")
    monkeypatch.setattr(bp, "CHALLENGER_ROOT", tmp_path / "challengers")
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(model_registry, "load_registry", lambda path=None: registry)
    monkeypatch.setattr(sys, "argv", ["build_predictions", "--week", "2026-W40"])

    bp.main()

    assert (tmp_path / "predictions.json").exists()
    assert not (tmp_path / "challengers" / MODEL_ID).exists()
    assert (tmp_path / "challengers" / "top3-partner-v1" / "predictions.json").exists()
    [record_path] = (tmp_path / "challengers" / "_failures" / MODEL_ID).glob("*_build.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["stage"] == "build" and record["error_type"] == "ValueError"
    assert "設定が登録と違います" in record["error"]


def test_a_race_that_fails_only_in_the_challenger_is_recorded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(bp, "CHALLENGER_ROOT", tmp_path / "challengers")
    now = datetime.datetime(2026, 10, 10, 9, 0, tzinfo=bp.JST)
    path = bp.record_challenger_failure(MODEL_ID, now, "race", race_ids=["20261018-tokyo-11"])
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["stage"] == "race" and record["race_ids"] == ["20261018-tokyo-11"] and record["error"] is None


def test_an_unwritable_failure_record_does_not_raise(tmp_path: Path, monkeypatch):
    # 記録が書けなくても止めない（予想ビルドのステップが落ちると Champion のコミットまで止まるため）
    blocker = tmp_path / "challengers"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(bp, "CHALLENGER_ROOT", blocker)
    now = datetime.datetime(2026, 10, 10, 9, 0, tzinfo=bp.JST)
    assert bp.record_challenger_failure(MODEL_ID, now, "build", error=ValueError("x")) is None


# ------------------------------------------------------------------ snapshot の監査記録（発走前）

def test_only_the_challenger_snapshot_carries_the_audit_record(tmp_path: Path):
    registry = model_registry.load_registry()
    for spec, directory in ((registry["champion"], tmp_path / "champion"), (_spec(), tmp_path / "challenger")):
        predictions = bp.build_predictions(_raw(), model_spec=spec, generated_at=BUILT.isoformat())
        snapshots.freeze(predictions, now=BUILT, directory=directory)
    champion = json.loads(next((tmp_path / "champion").glob("*.json")).read_text(encoding="utf-8"))
    challenger = json.loads(next((tmp_path / "challenger").glob("*.json")).read_text(encoding="utf-8"))
    assert "race_performance_ref" not in champion and "race_performance_quality" not in champion
    assert challenger["race_performance_ref"]["config_sha256"] == _spec()["race_performance"]["config_sha256"]
    quality = challenger["race_performance_quality"]
    for key in ("field_size", "rps_available_horses", "rps_coverage", "factor_active", "factor_inactive_reason",
                "excluded_by_reason_total", "config_sha256", "version", "input_raw_hash"):
        assert key in quality, key
    horse = next(h for h in quality["horses"] if h["usable_runs"])
    for key in ("position", "date", "class", "margin_sec", "dist", "v_run", "w_recency", "w_dist"):
        assert key in horse["usable_runs"][0], key
    assert challenger["pre_race"] is True


def test_raw_is_not_mutated_by_the_challenger():
    # main() は同じ raw から Champion → Challenger の順に作る。どちらも raw を書き換えないので、
    # snapshot の input_raw_hash は capture が読み直した raw の指紋と同じになる
    raw = _raw()
    before = json.dumps(raw, sort_keys=True, ensure_ascii=False)
    bp.build_predictions(raw, model_spec=model_registry.load_registry()["champion"], generated_at="X")
    challenger = bp.build_predictions(raw, model_spec=_spec(), generated_at="X")
    assert json.dumps(raw, sort_keys=True, ensure_ascii=False) == before
    fresh = json.loads(before)
    for race, built in zip(fresh["races"], challenger["races"]):
        assert built["race_performance_quality"]["input_raw_hash"] == rp.canonical_sha256(race)


# ------------------------------------------------------------------ 発走前 capture（fidelity）と登録の境界

def _eval_cfg() -> dict:
    return me.load_config(EVAL_CONFIG)


def test_capture_recomputes_the_challenger_with_rps_and_matches_the_snapshot(tmp_path: Path):
    registry = model_registry.load_registry()
    raw = _raw("2026-W39")
    champion_dir, challenger_root = tmp_path / "snapshots", tmp_path / "challengers"
    for spec, directory in ((registry["champion"], champion_dir), (_spec(), challenger_root / MODEL_ID / "snapshots")):
        predictions = bp.build_predictions(copy.deepcopy(raw), model_spec=spec, generated_at=BUILT.isoformat())
        snapshots.freeze(predictions, now=BUILT, directory=directory)
    report = me.capture(raw, now=BUILT, champion_dir=champion_dir, challenger_root=challenger_root,
                        output_dir=tmp_path / "capture", cfg=_eval_cfg(), registry=registry)
    assert {a["model_id"] for a in report["added"]} == {registry["champion"]["id"], MODEL_ID}
    rows = {}
    for model_id in (registry["champion"]["id"], MODEL_ID):
        path = next((tmp_path / "capture" / model_id).glob("*/*.json"))
        rows[model_id] = json.loads(path.read_text(encoding="utf-8"))
        assert rows[model_id]["fidelity"]["recomputed_matches_snapshot"] is True, model_id
    assert rows[MODEL_ID]["provenance"]["snapshot"]["race_performance_ref"]["version"] == "race-performance-v1"
    assert rows[MODEL_ID]["provenance"]["input_raw_sha256"] == rows[registry["champion"]["id"]]["provenance"]["input_raw_sha256"]
    # 同じ snapshot なら取り直さない
    again = me.capture(raw, now=BUILT + datetime.timedelta(minutes=5), champion_dir=champion_dir,
                       challenger_root=challenger_root, output_dir=tmp_path / "capture", cfg=_eval_cfg(),
                       registry=registry)
    assert again["added"] == []


def test_each_evaluation_keeps_its_own_champion_capture_in_the_same_second(tmp_path: Path, monkeypatch):
    # speed-v2 と race-performance-v1 の評価は、どちらも Champion を記録する。同じ秒に取っても
    # ファイル名（時刻_phase）が重なって片方が duplicate_time で捨てられないよう、フォルダを分ける
    eval_dir = tmp_path / "data" / "shadow" / "model_evaluation"
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(me, "EVAL_DIR", eval_dir)
    monkeypatch.setattr(me, "CAPTURE_DIR", eval_dir / "prerace")
    registry = model_registry.load_registry()
    raw = _raw("2026-W39")
    champion_dir = tmp_path / "snapshots"
    predictions = bp.build_predictions(copy.deepcopy(raw), model_spec=registry["champion"],
                                       generated_at=BUILT.isoformat())
    snapshots.freeze(predictions, now=BUILT, directory=champion_dir)
    champion_id = registry["champion"]["id"]
    for cfg in (me.load_config(), _eval_cfg()):
        report = me.capture(raw, now=BUILT, champion_dir=champion_dir, challenger_root=tmp_path / "challengers",
                            cfg=cfg, registry=registry)
        assert any(a["model_id"] == champion_id for a in report["added"])
        assert not any(s.get("reason") == "duplicate_time" for s in report["skipped"])
    speed = {p.relative_to(eval_dir / "prerace") for p in (eval_dir / "prerace" / champion_id).glob("*/*.json")}
    rps_dir = eval_dir / MODEL_ID / "prerace"
    rps = {p.relative_to(rps_dir) for p in (rps_dir / champion_id).glob("*/*.json")}
    assert speed and speed == rps  # 同じ名前のファイルが、それぞれの評価のフォルダに1つずつ
    assert me.capture_dir_for(me.load_config()) == me.CAPTURE_DIR
    with pytest.raises(ValueError, match="capture_dir"):
        me.capture_dir_for({**_eval_cfg(), "capture_dir": "data/challengers/x"})


def test_capture_without_rps_would_not_match_the_challenger_snapshot(tmp_path: Path):
    """capture の再計算に RPS を入れないと snapshot と一致しない（fidelity が意味を持つことの確認）。"""
    race = _raw("2026-W39")["races"][0]
    configs = bp.load_configs()
    from logic import speed_index
    base_times = speed_index.load_base_times()
    rates = bp._overall_rates({"races": [race]})
    predictions = bp.build_predictions({"races": [copy.deepcopy(race)]}, model_spec=_spec(), generated_at="X")
    snapshot_marks = predictions["races"][0]["marks"]
    _, with_rps = prerace_capture._recompute_horses(race, configs, base_times, rates, model_spec=_spec())
    _, without = prerace_capture._recompute_horses(race, configs, base_times, rates)
    assert prerace_capture._fidelity(with_rps, snapshot_marks)["recomputed_matches_snapshot"] is True
    assert prerace_capture._fidelity(without, snapshot_marks)["recomputed_matches_snapshot"] is False


def test_forward_boundary_uses_the_latest_of_the_three_registrations():
    cfg = _eval_cfg()
    assert cfg["prereg_effective_at"] == cfg["registered_at"] == "2026-10-09T00:26:18+09:00"
    challenger = {"registered_at": "2026-10-10T12:00:00+09:00"}
    assert me.forward_start(cfg, challenger).isoformat() == "2026-10-10T12:00:00+09:00"
    early = {**cfg, "registered_at": "2026-10-01T00:00:00+09:00"}
    assert me.forward_start(early, {"registered_at": "2026-10-02T00:00:00+09:00"}).isoformat() \
        == "2026-10-09T00:26:18+09:00"
    # 登録した Challenger（PR #29 のマージ時刻）がいちばん遅いので、境界はその時刻
    registry = model_registry.load_registry()
    _champion, challenger = me.model_specs(cfg, registry)
    assert challenger["registered_at"] == "2026-10-09T01:09:54+09:00"
    assert me.forward_start(cfg, challenger).isoformat() == "2026-10-09T01:09:54+09:00"
    # registered_at の無い Challenger は採点しない（capture は記録するだけ）
    unregistered = copy.deepcopy(registry)
    next(m for m in unregistered["challengers"] if m["id"] == MODEL_ID).pop("registered_at")
    with pytest.raises(ValueError, match="registered_at"):
        me.model_specs(cfg, unregistered)
    assert me.model_specs(cfg, unregistered, require_registered=False)[1]["id"] == MODEL_ID


def test_registration_record_in_the_document_matches_the_registry():
    # 文書 §1 の登録記録（手で書いた時刻ではなく、マージの事実）と models.json・forward の境界が食い違わない
    doc = (ROOT / "docs" / "research" / "RACE_PERFORMANCE_PREREG_V1.md").read_text(encoding="utf-8")
    registered = re.search(r"\| Challenger の登録日時 \| \*\*([0-9T:+-]+)\*\*", doc)
    boundary = re.search(r"\| forward の境界 \| \*\*([0-9T:+-]+)\*\*", doc)
    assert registered and boundary, "文書 §1 に Challenger の登録日時・forward の境界が無い"
    spec = _spec()
    assert spec["registered_at"] == registered.group(1)
    assert me.forward_start(_eval_cfg(), spec).isoformat() == boundary.group(1)


def test_pairing_rules_for_the_preregistered_evaluation():
    cfg = _eval_cfg()
    ok = {"fidelity": {"recomputed_matches_snapshot": True}, "provenance": {"input_raw_sha256": "a"}}
    assert me.capture_problem({"champion": ok, "challenger": ok}, cfg) is None
    assert me.capture_problem({"champion": ok, "challenger": None}, cfg) == "capture_missing"
    bad = {**ok, "fidelity": {"recomputed_matches_snapshot": False}}
    assert me.capture_problem({"champion": ok, "challenger": bad}, cfg) == "capture_fidelity_mismatch"
    other = {**ok, "provenance": {"input_raw_sha256": "b"}}
    assert me.capture_problem({"champion": ok, "challenger": other}, cfg) == "input_raw_mismatch"
    # speed-v2 の評価にはこの規則が無い（従来どおり capture が欠けても snapshot の値で比べる）
    assert me.capture_problem({"champion": ok, "challenger": None}, me.load_config()) is None

    spec = _spec()
    snap = {"frozen_at": "2026-10-18T09:00:00+09:00", "config_hash": "h", "model_id": MODEL_ID, "marks": [],
            "race_performance_ref": dict(spec["race_performance"])}
    champ = {**snap, "model_id": "win-v1-speed-guard"}
    post = datetime.datetime(2026, 10, 18, 15, 45, tzinfo=bp.JST)
    assert me.pairing_problem(champ, snap, post, spec, cfg) is None
    tampered = {**snap, "race_performance_ref": {**snap["race_performance_ref"], "config_sha256": "0" * 64}}
    assert me.pairing_problem(champ, tampered, post, spec, cfg) == "challenger_artifact_mismatch"
