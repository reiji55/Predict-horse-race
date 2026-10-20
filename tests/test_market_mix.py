"""research/market_mix.py の検証。登録の導出、格子と同点の決め方、bootstrap、判定の状態、一時 Git リポジトリでの通しの評価。"""
from __future__ import annotations

import datetime
import itertools
import json
import math
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from research import common
from research import market_audit
from research import market_baseline
from research import market_mix as mm

ROOT = Path(__file__).resolve().parent.parent
REF = (market_audit, market_baseline)


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    out = subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
    return out.stdout.decode().strip()


def _write(repo: Path, rel: str, payload) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _commit(repo: Path, msg: str, date: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg, date=date)
    return _git(repo, "rev-parse", "HEAD")


def _register(repo: Path, date: str) -> str:
    for rel in (mm.SPEC_REL, mm.CONFIG_REL):
        _write(repo, rel, (ROOT / rel).read_bytes())
    return _commit(repo, "register", date)


# ------------------------------------------------------------------ 固定値

def test_constants_match_registered_config() -> None:
    cfg = json.loads((ROOT / mm.CONFIG_REL).read_text(encoding="utf-8"))
    wm, trio = cfg["win_mix"], cfg["trio"]
    assert (min(mm.A_STEPS), max(mm.A_STEPS)) == (wm["a_grid"]["i_min"], wm["a_grid"]["i_max"])
    assert (min(mm.B_STEPS), max(mm.B_STEPS)) == (wm["b_grid"]["j_min"], wm["b_grid"]["j_max"])
    assert (min(mm.W_STEPS), max(mm.W_STEPS)) == (trio["w_grid"]["k_min"], trio["w_grid"]["k_max"])
    assert mm.EPS == cfg["numerics"]["eps"] and mm.TIE_TOL == cfg["numerics"]["tie_tolerance"]
    assert mm.MIN_TRAIN_RACES == cfg["walk_forward"]["min_train_races"]
    assert list(mm.VARIANT_PAYOUTS) == cfg["sensitivity"]["variants"]["payout"]
    assert len(mm.VARIANTS) == 18 and mm.PRIMARY_PAYOUT == cfg["inputs"]["trio_payout"]
    boot = cfg["evaluation"]["bootstrap"]
    assert boot["replicates"] == mm.BOOT_REPS and str(mm.BOOT_SEED) in boot["rng"]
    assert "250" in boot["interval"] and "9750" in boot["interval"]
    assert cfg["evaluation"]["checkpoints"]["at_cumulative"] == list(mm.CHECKPOINTS)
    assert "2027-06-30" in cfg["evaluation"]["decision_window"]["deadline"]
    assert "30 未満" in cfg["evaluation"]["decision"]["undecidable_if"]
    assert "10 未満" in cfg["evaluation"]["decision"]["undecidable_if"]


# ------------------------------------------------------------------ 格子と同点（§4）

def test_pick_ab_flat_objective_returns_market_only() -> None:
    acc = [0.0] * (len(mm.A_STEPS) * len(mm.B_STEPS))
    fit = mm.pick_ab(acc, 1)
    assert (fit["a"], fit["b"]) == (1.0, 0.0) and fit["tied_points"] == len(acc)


def test_pick_ab_tie_break_is_a_total_order() -> None:
    grid = [(i, j) for i in mm.A_STEPS for j in mm.B_STEPS]
    acc = [0.0 if (i, j) in {(40, 10), (40, -10), (60, 10), (60, -10)} else 1.0 for i, j in grid]
    fit = mm.pick_ab(acc, 1)
    # |b| も |a−1| も同じ → a が小さい → b が小さい
    assert (fit["a"], fit["b"]) == (0.8, -0.2)


def test_pick_ab_tolerance() -> None:
    grid = [(i, j) for i in mm.A_STEPS for j in mm.B_STEPS]
    within = [1.0 if (i, j) != (100, 50) else 1.0 - 5e-13 for i, j in grid]
    assert mm.pick_ab(within, 1)["tied_points"] == len(grid)
    outside = [1.0 if (i, j) != (100, 50) else 1.0 - 1e-9 for i, j in grid]
    fit = mm.pick_ab(outside, 1)
    assert (fit["a"], fit["b"], fit["tied_points"]) == (2.0, 1.0, 1)


def test_pick_w_prefers_market_on_ties_and_model_when_better() -> None:
    assert mm.pick_w([0.1, 0.2], [0.1, 0.2])["w"] == 1.0
    assert mm.pick_w([0.5, 0.5], [0.01, 0.01])["w"] == 0.0


def test_ab_terms_match_direct_log_loss() -> None:
    p_mkt = {1: 0.5, 2: 0.3, 3: 0.15, 4: 0.05}
    p_model = {1: 0.2, 2: 0.4, 3: 0.3, 4: 0.1}
    race = {"x": [math.log(p_mkt[n]) for n in p_mkt], "y": {"primary": [math.log(p_model[n]) for n in p_mkt]},
            "w_index": 1}
    terms = mm.race_ab_terms(race, "primary")
    n_b = len(mm.B_STEPS)
    for i, j in [(0, -100), (50, 0), (75, 25), (150, 100)]:
        pm = mm.p_mix(race["x"], race["y"]["primary"], i / 50, j / 50)
        assert terms[i * n_b + (j - min(mm.B_STEPS))] == pytest.approx(-math.log(pm[1]), abs=1e-12)
    assert sum(mm.p_mix(race["x"], race["y"]["primary"], 1.0, 0.0)) == pytest.approx(1.0)
    assert mm.p_mix(race["x"], race["y"]["primary"], 1.0, 0.0)[0] == pytest.approx(0.5)


# ------------------------------------------------------------------ bootstrap（§6.3）

def test_bootstrap_follows_registered_procedure() -> None:
    days = [[-0.2, 0.1], [0.3], [-0.5, -0.1, 0.0]]
    got = mm.bootstrap(days)
    rng = random.Random(20261019)
    stats = []
    for _ in range(10000):
        picks = [days[rng.randrange(3)] for _ in range(3)]
        stats.append(sum(sum(d) for d in picks) / sum(len(d) for d in picks))
    stats.sort()
    assert got["lower"] == stats[249] and got["upper"] == stats[9749] and got["n_day"] == 3
    one = mm.bootstrap([[-0.1, -0.3]])
    assert one["lower"] == one["upper"] == pytest.approx(-0.2)


# ------------------------------------------------------------------ 判定の状態（§6.2・§6.4）

def _rows(days: list[str], per_day: int, d: float = -0.1, variant_d: float = -0.1, status: str = "evaluated"):
    rows = []
    for day in days:
        for k in range(per_day):
            rows.append({"race_id": f"{day}-r{k}", "race_day": day, "status": status, "_counted": True,
                         "p_model": {"source": {"model_id": "m", "config_hash": "c"}},
                         "evaluation": {"D": d, "D_variants": {mm.variant_key(*v): variant_d for v in mm.VARIANTS},
                                        "secondary_diffs": {"win_logloss": 0.0, "top3_brier": 0.0,
                                                            "T1_vs_harville_mkt": 0.0}}})
    return rows


def _days(n: int, start=datetime.date(2026, 11, 1)) -> list[str]:
    return [(start + datetime.timedelta(days=7 * k)).strftime("%Y%m%d") for k in range(n)]


def test_checkpoint_is_report_only() -> None:
    days = _days(4)
    state = mm.decision_state(_rows(days, 8), days, datetime.date(2026, 12, 31))
    assert state["status"] == "checkpoint_30" and state["checkpoints"]["30"]["report_only"] is True
    assert state["checkpoints"]["30"]["end_day"] == days[3]


def test_decision_waits_for_results_then_adopts() -> None:
    days = _days(12)
    rows = _rows(days, 9)        # 9 × 12 = 108、100 に届くのは 12 日目
    end = mm._date(days[11])
    waiting = mm.decision_state([dict(r) for r in rows], days, end + datetime.timedelta(days=13))
    assert waiting["status"] == "awaiting_results" and waiting["window"]["end_day"] == days[11]
    state = mm.decision_state([dict(r) for r in rows], days, end + datetime.timedelta(days=14))
    assert state["status"] == "adopted" and state["window"]["evaluated"] == 108


def test_one_positive_variant_blocks_adoption() -> None:
    days = _days(12)
    rows = _rows(days, 9)
    key = mm.variant_key(0.725, "uniform", False)
    for r in rows:
        r["evaluation"]["D_variants"][key] = 0.05
    state = mm.decision_state(rows, days, datetime.date(2027, 6, 1))
    assert state["status"] == "not_adopted"


def test_few_days_is_undecidable_even_if_significant() -> None:
    days = _days(5)
    state = mm.decision_state(_rows(days, 20), days, datetime.date(2027, 6, 1))
    assert state["status"] == "undecidable" and state["window"]["n_day"] == 5


def test_deadline_closes_the_window_and_pending_become_unavailable() -> None:
    days = _days(12, start=datetime.date(2027, 4, 1))
    rows = _rows(days, 3) + _rows(days[-1:], 1, status="pending_result")
    state = mm.decision_state(rows, days, datetime.date(2027, 7, 31))
    assert state["status"] == "adopted" and state["window"]["end_day"] == days[-1]
    assert state["window"]["evaluated"] == 36
    assert any(r.get("reason") == "result_unavailable" for r in rows)


# ------------------------------------------------------------------ 登録（§1）

def test_registration_is_the_first_parent_merge_commit(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _write(tmp_path, "README", b"x")
    _commit(tmp_path, "init", "2026-11-01T10:00:00+09:00")
    _git(tmp_path, "checkout", "-q", "-b", "feat")
    _register(tmp_path, "2026-11-02T10:00:00+09:00")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--no-ff", "feat", "-m", "merge", date="2026-11-03T21:30:00+09:00")
    merge = _git(tmp_path, "rev-parse", "HEAD")
    reg = mm.registration(tmp_path)
    assert reg["status"] == "ok" and reg["commit"] == merge
    assert reg["registered_at"] == "2026-11-03T21:30:00+09:00"
    assert reg["config_sha256"] == common.sha256_file(ROOT / mm.CONFIG_REL)
    (tmp_path / mm.SPEC_REL).write_text("changed", encoding="utf-8")
    assert mm.registration(tmp_path)["status"] == "spec_changed"


def test_reference_code_is_loaded_from_the_registration_commit(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    for rel in mm.REFERENCE_CODE.values():
        _write(tmp_path, rel, (ROOT / rel).read_bytes())
    reg = _register(tmp_path, "2026-11-02T10:00:00+09:00")
    broken = (ROOT / "research/market_baseline.py").read_text(encoding="utf-8").replace(
        "def harville_trio(", "def _old(", 1) + "\n\ndef harville_trio(*a, **k):\n    raise RuntimeError\n"
    _write(tmp_path, "research/market_baseline.py", broken.encode("utf-8"))
    _commit(tmp_path, "change reference", "2026-11-05T10:00:00+09:00")
    ma, mb = mm.load_reference(tmp_path, reg)
    p = {1: 0.5, 2: 0.3, 3: 0.2, 4: 0.1}
    assert mb.harville_trio(p) == market_baseline.harville_trio(p)
    assert mb.ma is ma and ma.__file__.startswith(reg)
    info = mm.registration(tmp_path)["reference_code"]["research/market_baseline.py"]
    assert info["identical"] is False


def test_spec_changed_stops_before_evaluation(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _register(tmp_path, "2026-11-02T10:00:00+09:00")
    (tmp_path / mm.CONFIG_REL).write_text("{}", encoding="utf-8")
    summary = mm.run(tmp_path, tmp_path / "out", datetime.date(2026, 11, 20), reference=REF)
    assert summary["status"] == "spec_changed"
    assert not (tmp_path / "out/races").exists()


# ------------------------------------------------------------------ 通しの評価

RUNNERS = list(range(1, 7))


def _raw_race(race_id: str, day_iso: str, seed: int) -> dict:
    odds = {n: 2.0 + ((seed + n) % 6) * 2.5 for n in RUNNERS}
    trios = list(itertools.combinations(RUNNERS, 3))[: 8 + seed % 5]
    trio_odds = {"-".join(map(str, t)): 5.0 + 3.0 * k for k, t in enumerate(trios)}
    meta = {"source_time": f"{day_iso} 13:40:00", "observed_at": f"{day_iso}T14:09:00+09:00"}
    return {"id": race_id, "post_time": "15:45", "entries": [{"num": n, "win_odds": o} for n, o in odds.items()],
            "combo_odds": {"3連複": trio_odds}, "odds_market_meta": {"markets": {"sanrenpuku": meta, "win": meta}}}


def _p_model_doc(race_id: str, seed: int) -> dict:
    raw = {n: 1.0 + ((seed * 7 + n * 3) % 5) for n in RUNNERS}
    tot = sum(raw.values())
    return {"race_id": race_id, "status": "evaluated", "phase": "forward", "config_hash": "cfg",
            "frozen_at": "x", "evaluation_config_hash": "e",
            "models": {"champion": {"model_id": "champ", "capture": {"matched": True, "fidelity": True},
                                    "snapshot": {"sha256": "s"}}},
            "probability": {"horses": [{"num": n, "p_champion": raw[n] / tot, "p_challenger": (raw[n] + 1) / (tot + 6)}
                                       for n in RUNNERS]}}


def _race_day(repo: Path, day: str, races: list[tuple[str, int, dict | None, bool]], results: dict) -> None:
    """races: (venue, seed, result or None, p_model あり)。発走前のコミットと発走後のコミットを作る。"""
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    week = f"W{day}"
    _write(repo, f"raw/{week}.json", {"races": [_raw_race(f"{day}-{v}-11", iso, s) for v, s, _, _ in races]})
    for v, _, _, _ in races:
        rid = f"{day}-{v}-11"
        _write(repo, f"data/snapshots/{rid}.json", {"race_id": rid, "week_id": week, "frozen_at": f"{iso}T14:10:00+09:00",
                                                    "pre_race": True, "post_time": "15:45", "marks": [], "cards": []})
    _commit(repo, f"pre {day}", f"{iso}T14:10:30+09:00")
    for v, s, res, has_p in races:
        rid = f"{day}-{v}-11"
        if res is not None:
            results[rid] = res
        if has_p and res is not None:
            _write(repo, f"{mm.RPS_RACES_DIR}/{rid}.json", _p_model_doc(rid, s))
    _write(repo, mm.RESULTS_REL, results)
    _commit(repo, f"post {day}", f"{iso}T17:00:00+09:00")


def _finish(seed: int) -> dict:
    order = sorted(RUNNERS, key=lambda n: (seed * 5 + n * 11) % 7)
    return {"finish": order}


@pytest.fixture()
def mixed_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    results: dict = {}
    seed = 0
    for day in ("20261101", "20261102", "20261107", "20261108"):
        races = []
        for v in ("tokyo", "kyoto", "niigata"):
            seed += 1
            races.append((v, seed, _finish(seed), True))
        _race_day(tmp_path, day, races, results)
    _register(tmp_path, "2026-11-10T12:00:00+09:00")
    dead_heat = {**_finish(40), "dividends": {"3連複": [{"horses": [1, 2, 3], "pay": 1}, {"horses": [1, 2, 4], "pay": 1}]}}
    _race_day(tmp_path, "20261114", [("tokyo", 20, _finish(20), True), ("kyoto", 21, None, True),
                                     ("niigata", 22, _finish(22), False)], results)
    _race_day(tmp_path, "20261115", [("tokyo", 23, dead_heat, True)], results)
    return tmp_path


def test_end_to_end_walk_forward_and_contract(mixed_repo: Path) -> None:
    out = mixed_repo / "out"
    summary = mm.run(mixed_repo, out, datetime.date(2026, 11, 20), reference=REF)
    assert summary["registration"]["registered_at"] == "2026-11-10T12:00:00+09:00"
    assert summary["status"] == "collecting" and summary["evaluated"] == 1
    assert summary["training_pool"] == 13
    assert summary["pending_result"] == ["20261114-kyoto-11"]
    assert summary["excluded"] == {"p_model_file_missing": ["20261114-niigata-11"],
                                   "dead_heat_top3": ["20261115-tokyo-11"]}
    # 区間の数え方：発走前の契約を確かめたもの（評価・結果の契約で外れたもの）だけを数える。
    # 結果待ちで p_model の個票がまだ無いレースは未確認として数えず、その開催日から先の境界を決めない
    assert summary["decision"]["counted_races"] == 2
    assert summary["eligibility_unverified"] == ["20261114-kyoto-11"]
    assert summary["decision"]["resolved_through"] is None
    days = json.loads((out / "days.json").read_text(encoding="utf-8"))
    assert days["primary"]["20261114"]["n_train"] == 12 and days["primary"]["20261115"]["n_train"] == 13
    assert "20261114-tokyo-11" not in days["primary"]["20261114"]["train_races"]
    row = json.loads((out / "races/20261114-tokyo-11.json").read_text(encoding="utf-8"))
    ev = row["evaluation"]
    assert row["status"] == "evaluated" and row["role"] == "evaluation"
    assert ev["w"] in [k / 20 for k in mm.W_STEPS]
    assert ev["a"] * 50 == pytest.approx(round(ev["a"] * 50)) and ev["b"] * 50 == pytest.approx(round(ev["b"] * 50))
    assert ev["D"] == pytest.approx(ev["trio_logloss"]["T2"] - ev["trio_logloss"]["market_trio"], abs=2e-6)
    assert set(ev["D_variants"]) == {mm.variant_key(*v) for v in mm.VARIANTS}
    assert ev["D_variants"][mm.PRIMARY_VARIANT] == pytest.approx(ev["D"], abs=1e-6)
    assert row["secondary_evaluation"]["D"] is not None
    assert row["p_model"]["source"]["blob_sha"] and row["p_model"]["source"]["capture"] == {"matched": True, "fidelity": True}
    train = json.loads((out / "races/20261101-tokyo-11.json").read_text(encoding="utf-8"))
    assert train["status"] == "training_only" and "evaluation" not in train
    assert not (out / "decision.json").exists()


def test_insufficient_training_is_not_evaluated(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    results: dict = {}
    _race_day(tmp_path, "20261101", [(v, k + 1, _finish(k + 1), True) for k, v in enumerate(("tokyo", "kyoto"))], results)
    _register(tmp_path, "2026-11-02T12:00:00+09:00")
    _race_day(tmp_path, "20261107", [("tokyo", 9, _finish(9), True)], results)
    summary = mm.run(tmp_path, tmp_path / "out", datetime.date(2026, 11, 20), reference=REF)
    assert summary["evaluated"] == 0
    assert summary["excluded"] == {"insufficient_training": ["20261107-tokyo-11"]}
    assert summary["decision"]["counted_races"] == 0


def test_p_model_source_change_is_logged(mixed_repo: Path) -> None:
    out = mixed_repo / "out"
    mm.run(mixed_repo, out, datetime.date(2026, 11, 20), reference=REF)
    rel = f"{mm.RPS_RACES_DIR}/20261101-tokyo-11.json"
    doc = json.loads((mixed_repo / rel).read_text(encoding="utf-8"))
    doc["probability"]["horses"][0]["p_champion"] += 0.01
    _write(mixed_repo, rel, doc)
    _commit(mixed_repo, "rewrite p_model", "2026-11-21T10:00:00+09:00")
    summary = mm.run(mixed_repo, out, datetime.date(2026, 11, 21), reference=REF)
    assert summary["p_model_source_changed"] == ["20261101-tokyo-11"]
    log = json.loads((out / "p_model_source_changes.json").read_text(encoding="utf-8"))
    assert log[-1]["values"]["p_champion"]["changed_horses"] == ["1"]
    assert log[-1]["capture_now"] == {"matched": True, "fidelity": True}


# ------------------------------------------------------------------ PR #43 レビュー P0-1：結果待ちでも p_model の契約を確かめる

def _write_p_model(repo: Path, race_id: str, seed: int, **change) -> None:
    doc = _p_model_doc(race_id, seed)
    if change.get("matched") is False:
        doc["models"]["champion"]["capture"]["matched"] = False
    if change.get("drop_horse"):
        doc["probability"]["horses"] = doc["probability"]["horses"][:-1]
    _write(repo, f"{mm.RPS_RACES_DIR}/{race_id}.json", doc)


def test_pending_races_are_counted_only_after_p_model_contract(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    results: dict = {}
    seed = 0
    for day in ("20261101", "20261102", "20261107", "20261108"):
        races = []
        for v in ("tokyo", "kyoto", "niigata"):
            seed += 1
            races.append((v, seed, _finish(seed), True))
        _race_day(tmp_path, day, races, results)
    _register(tmp_path, "2026-11-10T12:00:00+09:00")
    venues = ("tokyo", "kyoto", "niigata", "hanshin")
    _race_day(tmp_path, "20261114", [(v, 30 + k, None, False) for k, v in enumerate(venues)], results)
    # 結果より前に個票がある場合：契約に合う・capture 不一致・馬の欠け。hanshin は個票がまだ無い
    _write_p_model(tmp_path, "20261114-tokyo-11", 30)
    _write_p_model(tmp_path, "20261114-kyoto-11", 31, matched=False)
    _write_p_model(tmp_path, "20261114-niigata-11", 32, drop_horse=True)
    _commit(tmp_path, "p_model before results", "2026-11-14T18:00:00+09:00")
    before = mm.run(tmp_path, tmp_path / "out", datetime.date(2026, 11, 15), reference=REF)
    assert before["pending_result"] == ["20261114-hanshin-11", "20261114-tokyo-11"]
    assert before["eligibility_unverified"] == ["20261114-hanshin-11"]
    assert before["excluded"] == {"p_model_capture_not_verified": ["20261114-kyoto-11"],
                                  "p_model_missing_runner": ["20261114-niigata-11"]}
    assert before["decision"]["counted_races"] == 1 and before["decision"]["resolved_through"] is None

    # 結果と残りの個票が入った後：数え方は発走前の条件と同じで、結果の中身で増減しない
    for k, v in enumerate(venues):
        results[f"20261114-{v}-11"] = _finish(30 + k)
    _write(tmp_path, mm.RESULTS_REL, results)
    _write_p_model(tmp_path, "20261114-hanshin-11", 33)
    _commit(tmp_path, "results", "2026-11-14T19:00:00+09:00")
    after = mm.run(tmp_path, tmp_path / "out", datetime.date(2026, 11, 15), reference=REF)
    assert after["eligibility_unverified"] == []
    assert after["excluded"] == before["excluded"]
    assert after["decision"]["counted_races"] == 2 and after["decision"]["resolved_through"] == "20261114"
    assert after["evaluated"] == 2


def test_unverified_race_times_out_after_the_wait(mixed_repo: Path) -> None:
    summary = mm.run(mixed_repo, mixed_repo / "out", datetime.date(2026, 11, 28), reference=REF)
    assert summary["eligibility_unverified"] == []
    assert summary["excluded"]["eligibility_unverifiable"] == ["20261114-kyoto-11"]
    assert summary["decision"]["resolved_through"] == "20261115"


def test_boundary_does_not_depend_on_arrival_order() -> None:
    days = _days(4)
    base = _rows(days[:1], 10) + _rows(days[1:2], 9) + _rows(days[2:], 10)
    late = {"race_id": f"{days[1]}-late", "race_day": days[1], "status": "pending_result"}

    waiting = mm.decision_state([*map(dict, base), {**late, "_unresolved": True}], days, datetime.date(2026, 11, 30))
    assert waiting["reached"] == {} and waiting["resolved_through"] == days[0]
    # 確かめられた後の境界：数えるなら 3 日目、数えないなら 4 日目。未確認の間に 3 日目と決めてしまわない
    counted = mm.decision_state([*map(dict, base), {**_rows(days[1:2], 1)[0], "race_id": late["race_id"]}],
                                days, datetime.date(2026, 11, 30))
    assert counted["reached"][30] == days[2]
    failed = mm.decision_state([*map(dict, base), {**late, "status": "excluded"}], days, datetime.date(2026, 11, 30))
    assert failed["reached"][30] == days[3]


def test_deadline_waits_for_unresolved_days() -> None:
    days = _days(12, start=datetime.date(2027, 4, 1))
    rows = _rows(days, 3) + [{"race_id": f"{days[-1]}-late", "race_day": days[-1], "status": "pending_result",
                              "_unresolved": True}]
    state = mm.decision_state(rows, days, datetime.date(2027, 7, 2))
    assert state["status"] == "awaiting_eligibility"


# ------------------------------------------------------------------ PR #43 レビュー P0-2：確定した判定の不変性

REG = {"commit": "c" * 40, "config_sha256": "s" * 64}


def _final_state(rows):
    days = sorted({r["race_day"] for r in rows})
    return mm.decision_state(rows, days, datetime.date(2027, 6, 1))


def test_decision_check_detects_output_change_with_same_status() -> None:
    days = _days(12)
    rows = _rows(days, 9, d=0.1, variant_d=0.1)
    for k, r in enumerate(rows):
        r["p_model"]["source"]["blob_sha"] = f"blob{k}"
        r["pinned"] = {"commit": "p"}
    state = _final_state(rows)
    assert state["status"] == "not_adopted"
    fixed = mm.decision_record(state, rows, REG)
    assert mm.compare_decision(fixed, mm.decision_record(_final_state(rows), rows, REG))["matches"] is True

    changed = [{**r, "evaluation": {**r["evaluation"], "D": 0.2}} if r["race_id"] == rows[0]["race_id"] else r
               for r in rows]
    state2 = _final_state(changed)
    assert state2["status"] == "not_adopted"
    check = mm.compare_decision(fixed, mm.decision_record(state2, changed, REG))
    assert check["matches"] is False and check["decision_output_changed"] is True
    assert check["status_changed"] is False and check["decision_input_changed"] is False

    blob = [{**r, "p_model": {"source": {**r["p_model"]["source"], "blob_sha": "other"}}}
            if r["race_id"] == rows[1]["race_id"] else r for r in rows]
    check = mm.compare_decision(fixed, mm.decision_record(_final_state(blob), blob, REG))
    assert check["matches"] is False and check["changed_races"] == [rows[1]["race_id"]]


def test_fixed_decision_is_not_overwritten(mixed_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mm, "DECISION_RACES", 2)
    monkeypatch.setattr(mm, "MIN_DECISION_RACES", 1)
    monkeypatch.setattr(mm, "MIN_DECISION_DAYS", 1)
    out = mixed_repo / "out"
    as_of = datetime.date(2026, 12, 31)
    sim = mm.run(mixed_repo, out, as_of, reference=REF, simulate=True)
    assert sim["as_of_mode"] == "simulation_on_current_inputs" and not (out / "decision.json").exists()
    first = mm.run(mixed_repo, out, as_of, reference=REF)
    fixed_text = (out / "decision.json").read_text(encoding="utf-8")
    fixed = json.loads(fixed_text)
    assert fixed["races"] == ["20261114-tokyo-11", "20261115-tokyo-11"] and first["warnings"] == []
    assert first["decision_check"]["matches"] is True

    rel = f"{mm.RPS_RACES_DIR}/20261114-tokyo-11.json"
    doc = json.loads((mixed_repo / rel).read_text(encoding="utf-8"))
    doc["probability"]["horses"][0]["p_champion"] *= 1.5
    _write(mixed_repo, rel, doc)
    _commit(mixed_repo, "rewrite p_model in window", "2027-01-02T10:00:00+09:00")
    again = mm.run(mixed_repo, out, as_of, reference=REF)
    assert (out / "decision.json").read_text(encoding="utf-8") == fixed_text
    assert again["decision_check"]["matches"] is False and again["decision_check"]["decision_input_changed"] is True
    assert again["p_model_source_changed_in_decision_window"] == ["20261114-tokyo-11"]
    assert set(again["warnings"]) == {"decision_recomputed_differs_from_fixed", "p_model_source_changed_in_decision_window"}
    monkeypatch.setattr(mm, "load_reference", lambda repo, commit: REF)
    assert mm.main(["--repo", str(mixed_repo), "--out", str(out)]) == 1
