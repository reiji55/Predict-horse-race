"""research/（shadow・observe-only の前向き検証基盤）のテスト。

最重要の不変条件:
- Champion の出力・snapshot・predictions.json を一切書き換えない
- 較正で「そのレース自身の結果」を温度の選択に使わない
- shadow は data/shadow/ にだけ書き、正式成績に混ぜない
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from logic import build_predictions, chappy, model_registry, snapshots
from research import calibration, chappy_shadow, common, decision_log, diversity, prerace_capture, rank_gap, settle

ROOT = Path(__file__).resolve().parent.parent
RAW_SAMPLE = ROOT / "docs" / "samples" / "raw.sample.json"
BASE_TIMES = ROOT / "tests" / "fixtures" / "base_times.sample.json"
JST = common.JST
CONFIG = common.load_config()
CHAPPY_CONFIG = chappy.load_config()


# ------------------------------------------------------------------ 合成snapshot

def _snapshot(race_id="20261004-nakayama-11", post_time="15:45", manual=False):
    """8頭立て。base順＝馬番順、人気は 1,2 が上位で 3〜5 は人気薄（順位ズレ候補）。"""
    odds = {1: 2.5, 2: 4.0, 3: 30.0, 4: 18.0, 5: 25.0, 6: 6.0, 7: 8.0, 8: 12.0}
    marks = [{"mk": "", "num": n, "name": f"H{n}", "odds": odds[n], "score": 80 - 2 * n,
              "top3_rank": n} for n in range(1, 9)]
    board = [{"num": n, "integrated": v, "role_scores": {}} for n, v in
             {1: .50, 2: .80, 3: .70, 4: .60, 5: .55, 6: .40, 7: .30, 8: .20}.items()]
    roles = {"win_anchor": {"num": 1}, "support": {"num": 2}, "top3_edge": {"num": 6}, "long_edge": {"num": 8}}
    auto_bets = [{k: b[k] for k in ("type", "horses", "amt")}
                 for b in chappy.build_portfolio(roles, CHAPPY_CONFIG)]
    chappy_card = ({"char": "chappy", "source": "manual_chat", "total": 1000,
                    "bets": [{"type": "ワイド", "horses": [1, 3], "amt": 1000}]}
                   if manual else
                   {"char": "chappy", "source": "signal_engine", "total": 1000, "bets": auto_bets})
    return {
        "race_id": race_id, "pre_race": True, "post_time": post_time,
        "frozen_at": "2026-10-04T14:40:00+09:00", "model_id": "win-v1-speed-guard",
        "config_hash": "abc", "marks": marks,
        "cards": [
            {"char": "kei", "bets": [{"type": "ワイド", "horses": [1, 2], "amt": 200},
                                     {"type": "ワイド", "horses": [1, 3], "amt": 300}]},
            {"char": "tetsu", "bets": [{"type": "馬連", "horses": [1, 2], "amt": 300},
                                       {"type": "ワイド", "horses": [1, 4], "amt": 200}]},
            {"char": "gen", "bets": [{"type": "3連複", "horses": [3, 4, 5], "amt": 300},
                                     {"type": "ワイド", "horses": [3, 4], "amt": 200}]},
            chappy_card,
        ],
        "chappy_decision": {"roles": {k: {"num": v["num"]} for k, v in roles.items()},
                            "signal_board": {"horses": board}},
    }


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ------------------------------------------------------------------ Champion不変

def test_shadow_config_is_not_part_of_champion_config_hash(tmp_path: Path):
    """shadow の設定を変えても Champion の config_hash は変わらない。"""
    assert "shadow_research.json" not in model_registry.HASH_CONFIGS
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    for name in model_registry.HASH_CONFIGS:
        (cfg_dir / name).write_bytes((ROOT / "config" / name).read_bytes())
    before = model_registry.config_hash(cfg_dir)
    (cfg_dir / "shadow_research.json").write_text('{"version":"changed"}', encoding="utf-8")
    assert model_registry.config_hash(cfg_dir) == before


def _build_sample_and_freeze(tmp_path: Path, now: datetime.datetime):
    raw = json.loads(RAW_SAMPLE.read_text(encoding="utf-8"))
    base_times = json.loads(BASE_TIMES.read_text(encoding="utf-8"))
    configs = build_predictions.load_configs()
    predictions = build_predictions.build_predictions(copy.deepcopy(raw), configs, base_times,
                                                      generated_at=now.isoformat())
    snap_dir = tmp_path / "snapshots"
    snapshots.freeze(predictions, now=now, directory=snap_dir)
    return raw, base_times, configs, predictions, snap_dir


def test_prerace_capture_reads_only_and_reproduces_champion_selection(tmp_path: Path):
    now = datetime.datetime(2026, 7, 5, 13, 0, tzinfo=JST)
    raw, base_times, configs, predictions, snap_dir = _build_sample_and_freeze(tmp_path, now)
    before = {p.name: p.read_bytes() for p in snap_dir.glob("*.json")}
    predictions_before = json.dumps(predictions, sort_keys=True, ensure_ascii=False)

    report = prerace_capture.capture(raw, now=now, snapshot_dir=snap_dir,
                                     output_dir=tmp_path / "prerace", configs=configs,
                                     base_times=base_times)

    assert report["added"] == ["20260705-kokura-11"]
    assert {p.name: p.read_bytes() for p in snap_dir.glob("*.json")} == before
    assert json.dumps(predictions, sort_keys=True, ensure_ascii=False) == predictions_before

    record = json.loads(next((tmp_path / "prerace").rglob("*.json")).read_text(encoding="utf-8"))
    assert record["fidelity"]["recomputed_matches_snapshot"] is True
    assert record["calibration_inputs"]["score_source"] == "prerace_full_precision"
    snapshot = json.loads(next(snap_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert record["provenance"]["snapshot"]["sha256"] == hashlib.sha256(
        next(snap_dir.glob("*.json")).read_bytes()).hexdigest()
    # 再計算したキャラ別選定順の先頭（軸）が、本番カードの先頭馬と一致する
    orders = record["diversity"]["sel_orders"]
    for card in snapshot["cards"]:
        if card["char"] in ("kei", "tetsu", "gen"):
            assert orders[card["char"]][0]["num"] == card["bets"][0]["horses"][0]


def test_prerace_capture_never_records_after_post_and_dedupes(tmp_path: Path):
    now = datetime.datetime(2026, 7, 5, 13, 0, tzinfo=JST)
    raw, base_times, configs, _, snap_dir = _build_sample_and_freeze(tmp_path, now)
    kwargs = dict(snapshot_dir=snap_dir, output_dir=tmp_path / "prerace",
                  configs=configs, base_times=base_times)

    after = prerace_capture.capture(raw, now=datetime.datetime(2026, 7, 5, 15, 35, tzinfo=JST), **kwargs)
    assert after["added"] == [] and after["skipped"][0]["reason"] == "already_posted"

    assert prerace_capture.capture(raw, now=now, **kwargs)["added"]
    again = prerace_capture.capture(raw, now=now + datetime.timedelta(minutes=5), **kwargs)
    assert again["added"] == [] and again["skipped"][0]["reason"] == "unchanged_snapshot"


# ------------------------------------------------------------------ 較正

def test_win_metrics_log_loss_and_brier():
    m = calibration.win_metrics({1: 0.5, 2: 0.3, 3: 0.2}, winner=2)
    assert m["log_loss"] == pytest.approx(-__import__("math").log(0.3), abs=1e-6)
    assert m["brier"] == pytest.approx(0.25 + 0.49 + 0.04, abs=1e-6)


def _inputs(scores, odds):
    return {"temperature_current": 10.0, "score_source": "test",
            "horses": [{"num": n, "score": scores[n], "odds": odds[n]} for n in scores]}


def test_entropy_match_temperature_uses_only_prerace_information():
    scores = {1: 85.0, 2: 80.0, 3: 72.0, 4: 70.0, 5: 65.0}
    odds = {1: 2.0, 2: 4.0, 3: 9.0, 4: 15.0, 5: 40.0}
    a = calibration.evaluate_race(_inputs(scores, odds), 1, [], CONFIG)
    b = calibration.evaluate_race(_inputs(scores, odds), 5, [], CONFIG)
    # 勝ち馬が違っても、選ばれる温度は同じ（結果を見ていない）
    assert a["models"]["p_entropy_match"]["temperature"] == b["models"]["p_entropy_match"]["temperature"]
    assert a["models"]["p_current"]["temperature"] == 10.0


def test_walk_forward_never_uses_the_race_itself():
    cfg = copy.deepcopy(CONFIG)
    cfg["calibration"]["walk_forward"]["min_history_races"] = 1
    scores = {1: 85.0, 2: 80.0, 3: 72.0, 4: 70.0, 5: 65.0}
    odds = {1: 2.0, 2: 4.0, 3: 9.0, 4: 15.0, 5: 40.0}
    first = calibration.evaluate_race(_inputs(scores, odds), 5, [], cfg)
    assert "p_walk_forward" not in first["models"]          # 過去が無ければ作らない
    assert first["walk_forward"]["history_races"] == 0

    # 同じレースの結果を「過去」として渡した場合と、別レースを渡した場合で温度が変わりうるが、
    # settle は発走時刻が厳密に前のレースしか渡さない（下の同時刻テスト参照）
    second = calibration.evaluate_race(_inputs(scores, odds), 1, [first["history_item"]], cfg)
    assert second["walk_forward"]["history_races"] == 1
    assert second["models"]["p_walk_forward"]["temperature"] in cfg["calibration"]["walk_forward"]["grid"]


def test_low_coverage_race_is_not_evaluated():
    scores = {n: 80.0 - n for n in range(1, 11)}
    odds = {n: (None if n > 7 else 3.0 + n) for n in range(1, 11)}   # 30%欠損
    out = calibration.evaluate_race(_inputs(scores, odds), 1, [], CONFIG)
    assert out["status"] == "insufficient_coverage"


# ------------------------------------------------------------------ 順位ズレ

def test_rank_gap_registration_is_rank_based_and_counts_against_market():
    snap = _snapshot()
    reg = rank_gap.register(snap["marks"], CONFIG)
    # base 1〜5 のうち人気6位以下は 3(30.0), 5(25.0), 4(18.0)
    assert [h["num"] for h in reg["horses"]] == [3, 4, 5]
    # 温度（スコアの尺度）を変えても登録は変わらない
    scaled = [{**m, "score": m["score"] * 3} for m in snap["marks"]]
    assert rank_gap.register(scaled, CONFIG)["horses"] == reg["horses"]

    ev = rank_gap.evaluate(reg, [3, 1, 2])
    assert ev["n"] == 3 and ev["observed_top3"] == 1
    assert 0 < ev["expected_top3"] < 3
    assert ev["observed_minus_expected"] == pytest.approx(1 - ev["expected_top3"], abs=1e-6)


# ------------------------------------------------------------------ Chappy shadow

def test_integrated_shadow_uses_same_pattern_with_integrated_top4():
    built = chappy_shadow.build(_snapshot(), CHAPPY_CONFIG, CONFIG)
    current = built["variants"]["current_auto"]
    shadow = built["variants"]["integrated_top4_same_pattern"]
    assert current["roles"] == {"win_anchor": 1, "support": 2, "top3_edge": 6, "long_edge": 8}
    assert current["source"] == "snapshot_card"
    # integrated 降順: 2(.80) 3(.70) 4(.60) 5(.55)
    assert shadow["roles"] == {"win_anchor": 2, "support": 3, "top3_edge": 4, "long_edge": 5}
    # 点構成と金額は現行と同じ（差は4頭の選び方だけ）
    assert [(b["type"], b["amt"]) for b in shadow["bets"]] == [(b["type"], b["amt"]) for b in current["bets"]]
    assert shadow["total"] == current["total"] == 1000


def test_current_auto_is_rebuilt_from_roles_when_manual_card_was_used():
    built = chappy_shadow.build(_snapshot(manual=True), CHAPPY_CONFIG, CONFIG)
    assert built["variants"]["current_auto"]["source"] == "rebuilt_from_roles"
    assert built["variants"]["current_auto"]["roles"]["win_anchor"] == 1


# ------------------------------------------------------------------ 多様性

def test_diversity_metrics_are_observation_only():
    sets = diversity.card_horse_sets(_snapshot()["cards"], ["kei", "tetsu", "gen"])
    orders = {"kei": [{"num": n} for n in (1, 2, 3, 4, 5)],
              "tetsu": [{"num": n} for n in (1, 2, 4, 3, 5)],
              "gen": [{"num": n} for n in (3, 4, 5, 1, 2)]}
    out = diversity.evaluate(orders, sets, CONFIG)
    pairs = {p["pair"]: p for p in out["pairs"]}
    assert pairs["kei-tetsu"]["axis_match"] is True
    assert pairs["kei-tetsu"]["jaccard"] == pytest.approx(2 / 4)
    assert pairs["kei-tetsu"]["sel_spearman"] == pytest.approx(0.9)
    assert pairs["kei-tetsu"]["redundancy_flag"] is False     # Jaccard 0.5 < 0.6
    assert pairs["kei-gen"]["axis_match"] is False


# ------------------------------------------------------------------ decision log

def _filled_log(snap_path: Path, snap: dict, created="2026-10-04T15:05:00+09:00"):
    log = decision_log.template(snap, snap_path, CONFIG, created_at=created)
    keep = {1, 2, 3, 4}
    for cand in log["candidates"]:
        cand["decision"] = "keep" if cand["num"] in keep else "drop"
        if cand["decision"] == "drop" and (cand["rank_gap"] or cand["in_fixed_cards"]):
            cand["reason_code"] = "insufficient_ability_evidence"
    log["bets"] = [
        {"type": "ワイド", "horses": [1, 2], "amt": 400, "hypothesis_tag": "win_anchor"},
        {"type": "ワイド", "horses": [1, 3], "amt": 300, "hypothesis_tag": "rank_gap"},
        {"type": "3連複", "horses": [1, 2, 4], "amt": 300, "hypothesis_tag": "ability_partner"},
    ]
    return log


def test_decision_log_template_lists_every_runner_with_snapshot_facts(tmp_path: Path):
    snap = _snapshot()
    path = _write(tmp_path / "snap.json", snap)
    log = decision_log.template(snap, path, CONFIG, created_at="2026-10-04T15:00:00+09:00")
    assert [c["num"] for c in log["candidates"]] == list(range(1, 9))
    by = {c["num"]: c for c in log["candidates"]}
    assert by[3]["rank_gap"] is True and by[3]["in_fixed_cards"] == ["kei", "gen"]
    assert by[1]["in_fixed_cards"] == ["kei", "tetsu"]
    assert log["snapshot_ref"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_decision_log_requires_reasons_for_dropping_protected_horses(tmp_path: Path):
    snap = _snapshot()
    path = _write(tmp_path / "snap.json", snap)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    log = _filled_log(path, snap)
    assert decision_log.validate(log, CONFIG, snap, sha) == []

    no_reason = copy.deepcopy(log)
    for cand in no_reason["candidates"]:
        if cand["num"] == 5:          # 順位ズレ・源さんが買っている馬
            cand["reason_code"] = None
    errors = decision_log.validate(no_reason, CONFIG, snap, sha)
    assert any("5番" in e and "reason_code" in e for e in errors)

    # 保護対象でない馬（8番: 順位ズレでも固定3人採用でもない）は理由なしで落としてよい
    assert all("8番" not in e for e in errors)


def test_decision_log_rejects_post_race_creation_and_inconsistent_facts(tmp_path: Path):
    snap = _snapshot()
    path = _write(tmp_path / "snap.json", snap)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()

    late = _filled_log(path, snap, created="2026-10-04T15:50:00+09:00")
    assert any("発走時刻以降" in e for e in decision_log.validate(late, CONFIG, snap, sha))

    tampered = _filled_log(path, snap)
    tampered["candidates"][2]["market_rank"] = 1
    assert any("market_rank" in e for e in decision_log.validate(tampered, CONFIG, snap, sha))

    wrong_total = _filled_log(path, snap)
    wrong_total["bets"][0]["amt"] = 500
    assert any("1000" in e for e in decision_log.validate(wrong_total, CONFIG, snap, sha))

    kept_unused = _filled_log(path, snap)
    kept_unused["bets"] = [b for b in kept_unused["bets"] if 4 not in b["horses"]] + [
        {"type": "ワイド", "horses": [2, 3], "amt": 300, "hypothesis_tag": "insurance"}]
    assert any("keep なのに" in e for e in decision_log.validate(kept_unused, CONFIG, snap, sha))


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest.mark.parametrize("commit_date,expected", [
    ("2026-10-04T15:10:00+09:00", True),    # 発走 15:45 より前にコミット
    ("2026-10-04T16:10:00+09:00", False),   # 発走後にコミット（記録の created_at は前でも不可）
])
def test_decision_log_prerace_is_verified_from_git_commit_time(tmp_path: Path, commit_date, expected):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    snap = _snapshot()
    snap_path = _write(repo / "data" / "snapshots" / f"{snap['race_id']}.json", snap)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "snapshot", date="2026-10-04T14:40:00+09:00")

    log = _filled_log(snap_path, snap)
    log["snapshot_ref"]["path"] = f"data/snapshots/{snap['race_id']}.json"
    log_path = _write(repo / "data" / "chappy_decisions" / snap["race_id"] / "20261004T150500.json", log)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "decision", date=commit_date)

    result = decision_log.verify(log_path, CONFIG, repo=repo)
    assert result["valid"] is True
    assert result["snapshot_version_found"] is True
    assert result["prerace_verified"] is expected


# ------------------------------------------------------------------ settle

def test_settle_writes_only_shadow_outputs_and_separates_phases(tmp_path: Path):
    cfg = copy.deepcopy(CONFIG)
    cfg["registered_at"] = "2026-10-01T00:00:00+09:00"
    snap_dir = tmp_path / "snapshots"
    old = _snapshot(race_id="20260927-nakayama-11")
    new = _snapshot(race_id="20261004-nakayama-11")
    manual = {**_snapshot(race_id="20261004-hanshin-11"), "evaluation_scope": "manual_chat"}
    for s in (old, new, manual):
        _write(snap_dir / f"{s['race_id']}.json", s)
    dividends = {"ワイド": [{"horses": [2, 3], "pay": 1200}, {"horses": [2, 4], "pay": 800},
                          {"horses": [3, 4], "pay": 2000}],
                 "馬連": [{"horses": [2, 3], "pay": 3000}],
                 "3連複": [{"horses": [2, 3, 4], "pay": 9000}]}
    race_results = {s["race_id"]: {"finish": [2, 3, 4, 1], "dividends": dividends}
                    for s in (old, new, manual)}

    summary = settle.settle(race_results, snapshot_dir=snap_dir, prerace_dir=tmp_path / "prerace",
                            decision_dir=tmp_path / "decisions", races_dir=tmp_path / "races",
                            summary_path=tmp_path / "summary.json", config=cfg,
                            chappy_config=CHAPPY_CONFIG, temperature_now=10.0, repo=tmp_path)

    assert summary["official_results_untouched"] is True
    assert summary["forward"]["races"] == 1 and summary["retrospective"]["races"] == 1
    written = sorted(p.name for p in (tmp_path / "races").glob("*.json"))
    assert written == ["20260927-nakayama-11.json", "20261004-nakayama-11.json"]   # 手動は対象外
    race = json.loads((tmp_path / "races" / "20261004-nakayama-11.json").read_text(encoding="utf-8"))
    assert race["phase"] == "forward" and race["evidence"] == "derived_from_snapshot"
    variants = race["experiments"]["chappy_shadow"]["variants"]
    assert variants["current_auto"]["payout"] == 0
    assert variants["integrated_top4_same_pattern"]["payout"] > 0
    for exp in ("calibration", "rank_gap", "chappy_shadow", "diversity"):
        assert race["experiments"][exp]["research_config_hash"] == common.config_hash(cfg)
        assert race["experiments"][exp]["version"] == cfg[exp]["version"]
    assert not (tmp_path / "results.json").exists()


def test_settle_walk_forward_excludes_races_at_the_same_post_time(tmp_path: Path):
    cfg = copy.deepcopy(CONFIG)
    cfg["calibration"]["walk_forward"]["min_history_races"] = 1
    snap_dir = tmp_path / "snapshots"
    a = _snapshot(race_id="20261004-hanshin-11", post_time="15:45")
    b = _snapshot(race_id="20261004-nakayama-11", post_time="15:45")
    c = _snapshot(race_id="20261011-nakayama-11", post_time="15:45")
    for s in (a, b, c):
        _write(snap_dir / f"{s['race_id']}.json", s)
    race_results = {s["race_id"]: {"finish": [1, 2, 3], "dividends": {}} for s in (a, b, c)}
    settle.settle(race_results, snapshot_dir=snap_dir, prerace_dir=tmp_path / "p",
                  decision_dir=tmp_path / "d", races_dir=tmp_path / "races",
                  summary_path=tmp_path / "s.json", config=cfg, chappy_config=CHAPPY_CONFIG,
                  temperature_now=10.0, repo=tmp_path)
    wf = {p.stem: json.loads(p.read_text(encoding="utf-8"))["experiments"]["calibration"]["walk_forward"]
          for p in (tmp_path / "races").glob("*.json")}
    assert wf["20261004-hanshin-11"]["history_races"] == 0
    assert wf["20261004-nakayama-11"]["history_races"] == 0     # 同時刻の他場は「過去」ではない
    assert wf["20261011-nakayama-11"]["history_races"] == 2


# ------------------------------------------------------------------ PR14 レビュー対応

def _repo_with_snapshot_history(tmp_path: Path):
    """snapshot v1 → log（v1参照）→ snapshot v2（発走前再実行でオッズ更新）の順にコミットしたリポジトリ。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    v1 = _snapshot()
    snap_path = _write(repo / "data" / "snapshots" / f"{v1['race_id']}.json", v1)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "snapshot v1", date="2026-10-04T13:00:00+09:00")

    log = _filled_log(snap_path, v1)            # v1 の事実と SHA-256 で作る
    log_path = _write(repo / "data" / "chappy_decisions" / v1["race_id"] / "20261004T150500.json", log)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "decision", date="2026-10-04T15:06:00+09:00")

    v2 = copy.deepcopy(v1)
    v2["frozen_at"] = "2026-10-04T15:20:00+09:00"
    for mark in v2["marks"]:
        if mark["num"] == 3:
            mark["odds"] = 3.0                  # 人気が急上昇 → 3番は順位ズレでなくなる
    _write(snap_path, v2)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "snapshot v2", date="2026-10-04T15:21:00+09:00")
    return repo, log_path, log, v1, v2


def test_log_referencing_overwritten_snapshot_is_checked_against_that_version(tmp_path: Path):
    repo, log_path, log, v1, v2 = _repo_with_snapshot_history(tmp_path)
    # 前提: 現行（v2）では3番の事実が変わっている
    assert decision_log.snapshot_facts(v2, CONFIG)[3]["rank_gap"] is False
    assert decision_log.snapshot_facts(v1, CONFIG)[3]["rank_gap"] is True

    result = decision_log.verify(log_path, CONFIG, repo=repo)
    assert result["errors"] == []
    assert result["facts_checked_against"] == "git_history"
    assert result["snapshot_version_found"] is True
    assert result["prerace_verified"] is True


def test_log_with_tampered_v1_facts_fails_even_when_snapshot_was_overwritten(tmp_path: Path):
    repo, log_path, log, v1, v2 = _repo_with_snapshot_history(tmp_path)
    tampered = copy.deepcopy(log)
    for cand in tampered["candidates"]:
        if cand["num"] == 3:
            cand["market_rank"] = 1             # v1 の事実（人気8位）と違う
            cand["rank_gap"] = False            # 保護対象から外して理由を書かずに済ませる改ざん
    _write(log_path, tampered)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "tamper", date="2026-10-04T15:22:00+09:00")

    result = decision_log.verify(log_path, CONFIG, repo=repo)
    assert result["facts_checked_against"] == "git_history"
    assert any("3番" in e and "market_rank" in e for e in result["errors"])
    assert any("3番" in e and "rank_gap" in e for e in result["errors"])
    assert result["valid"] is False and result["prerace_verified"] is False


def test_log_referencing_unknown_snapshot_version_is_not_verified(tmp_path: Path):
    repo, log_path, log, v1, v2 = _repo_with_snapshot_history(tmp_path)
    forged = copy.deepcopy(log)
    forged["snapshot_ref"]["sha256"] = "0" * 64
    _write(log_path, forged)
    result = decision_log.verify(log_path, CONFIG, repo=repo)
    assert result["snapshot_version_found"] is False
    assert result["facts_checked_against"] is None
    assert result["prerace_verified"] is False


def test_fidelity_fails_closed_when_only_odds_changed(tmp_path: Path):
    """スコアは同じでもオッズが違えば、固定3人の sel/value は変わりうる。選定順は採点に使わない。"""
    now = datetime.datetime(2026, 7, 5, 13, 0, tzinfo=JST)
    raw, base_times, configs, _, snap_dir = _build_sample_and_freeze(tmp_path, now)
    updated = copy.deepcopy(raw)
    entry = updated["races"][0]["entries"][0]
    entry["win_odds"] = round(float(entry["win_odds"]) * 3 + 1, 1)

    prerace_capture.capture(updated, now=now, snapshot_dir=snap_dir, output_dir=tmp_path / "prerace",
                            configs=configs, base_times=base_times)
    record = json.loads(next((tmp_path / "prerace").rglob("*.json")).read_text(encoding="utf-8"))
    fidelity = record["fidelity"]
    assert fidelity["recomputed_matches_snapshot"] is False
    assert fidelity["mismatches"] and all(m["fields"] == ["odds"] for m in fidelity["mismatches"])
    assert record["diversity"]["sel_orders"] is None
    assert record["calibration_inputs"]["score_source"] == "snapshot_marks_rounded"
