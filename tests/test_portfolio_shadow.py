"""research/portfolio_shadow.py の検証。登録の導出、判定の手順、区間、記録の不変性、一時 Git リポジトリでの通し。"""
from __future__ import annotations

import datetime
import itertools
import json
import os
import random
import subprocess
from pathlib import Path

import pytest

from research import market_audit, market_baseline, portfolio
from research import portfolio_shadow as ps

ROOT = Path(__file__).resolve().parent.parent


def _ref() -> dict:
    return {"ma": market_audit, "mb": market_baseline, "pf": portfolio,
            "portfolio": portfolio.load_config(), "baseline": market_baseline.load_config()}


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True).stdout.decode().strip()


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


def _register(repo: Path, date: str, with_reference: bool = False) -> str:
    rels = [ps.SPEC_REL, ps.CONFIG_REL]
    if with_reference:
        rels += [*ps.REFERENCE_CODE.values(), *ps.REFERENCE_CONFIG.values()]
    for rel in rels:
        _write(repo, rel, (ROOT / rel).read_bytes())
    return _commit(repo, "register", date)


# ------------------------------------------------------------------ 固定値

def test_constants_match_registered_config() -> None:
    cfg = json.loads((ROOT / ps.CONFIG_REL).read_text(encoding="utf-8"))
    assert list(ps.STRATEGIES) == cfg["strategies"]["evaluated"] and ps.PRIMARY == cfg["strategies"]["primary"]
    assert list(ps.RECORDED_ONLY) == cfg["strategies"]["recorded_only"]
    assert list(ps.CHECKPOINTS) == cfg["window"]["checkpoints"] and ps.DECISION_RACES == cfg["window"]["decision_races"]
    assert ps.DEADLINE.isoformat() == cfg["window"]["deadline"] and ps.RESULT_WAIT_DAYS == cfg["window"]["result_wait_days"]
    boot = cfg["bootstrap"]
    assert (ps.BOOT_SEED, ps.BOOT_REPS, ps.BOOT_LOWER_RANK, ps.BOOT_UPPER_RANK) == \
        (boot["seed"], boot["replicates"], boot["lower_rank"], boot["upper_rank"])
    dec = cfg["decision"]
    assert (ps.MIN_DECISION_RACES, ps.MIN_DECISION_DAYS, ps.CALIBRATION_TOLERANCE) == \
        (dec["min_races"], dec["min_days"], dec["calibration_tolerance"])
    # 評価する組み方は、凍結する portfolio_v1 の組み方にある
    assert set(ps.STRATEGIES) | set(ps.RECORDED_ONLY) == set(portfolio.load_config()["strategies"])


# ------------------------------------------------------------------ 判定の手順（§5）

def test_bootstrap_follows_registered_procedure() -> None:
    days = [[0.8, -0.2], [-0.1], [-0.3, 0.7, -0.2]]
    got = ps.bootstrap(days)
    rng = random.Random(20261021)
    stats = []
    for _ in range(10000):
        picks = [days[rng.randrange(3)] for _ in range(3)]
        stats.append(sum(sum(d) for d in picks) / sum(len(d) for d in picks))
    stats.sort()
    assert got["lower"] == stats[249] and got["upper"] == stats[9749]


@pytest.mark.parametrize("races,days,c,lower,upper,want", [
    (29, 12, 0.0, -0.1, 0.1, "undecidable"),
    (40, 9, 0.0, -0.1, 0.1, "undecidable"),
    (40, 12, -0.12, -0.2, -0.01, "over"),
    (40, 12, 0.12, 0.01, 0.2, "under"),
    (40, 12, 0.04, -0.03, 0.11, "calibrated"),
    (40, 12, -0.06, -0.13, 0.01, "inconclusive"),
])
def test_decide(races, days, c, lower, upper, want) -> None:
    report = {"primary_races": races, "primary_C": c, "bootstrap": {"n_day": days, "lower": lower, "upper": upper}}
    assert ps.decide(report) == want


# ------------------------------------------------------------------ 区間（§5.1）

def _row(race_id: str, hit: bool = False, p: float = 0.2, status: str = "evaluated", **extra) -> dict:
    strat = {"status": "chosen", "proven_optimal": True, "tickets": [], "p_hit": p, "p_hit_given_x_out": 0.05,
             "p_hit_variants": {"min": p - 0.01, "max": p + 0.01}, "stake": 500,
             "realized": {"hit": hit, "payout": 3000 if hit else 0} if status == "evaluated" else None}
    return {"race_id": race_id, "race_day": race_id[:8], "role": "eligible", "status": status,
            "x": {"horse": 1, "p_top3": 0.6}, "x_in_top3": True, "scenario_frame_v1": [],
            "strategies": {name: dict(strat) for name in (*ps.STRATEGIES, *ps.RECORDED_ONLY)}, **extra}


def _days(n: int, start=datetime.date(2026, 11, 1)) -> list[str]:
    return [(start + datetime.timedelta(days=7 * k)).strftime("%Y%m%d") for k in range(n)]


def _rows(days: list[str], per_day: int, hit_every: int = 5, p: float = 0.2) -> list[dict]:
    out, k = [], 0
    for day in days:
        for j in range(per_day):
            out.append(_row(f"{day}-r{j}", hit=(k % hit_every == 0), p=p))
            k += 1
    return out


def test_checkpoint_then_decision_after_wait() -> None:
    days = _days(12)
    rows = _rows(days, 9)
    state = ps.decision_state([dict(r) for r in rows[:36]], datetime.date(2027, 1, 30))
    assert state["status"] == "checkpoint_30" and state["checkpoints"]["30"]["report_only"] is True
    end = ps._date(days[11])
    waiting = ps.decision_state([dict(r) for r in rows], end + datetime.timedelta(days=13))
    assert waiting["status"] == "awaiting_results" and waiting["window"]["end_day"] == days[11]
    final = ps.decision_state([dict(r) for r in rows], end + datetime.timedelta(days=14))
    assert final["window"]["primary_races"] == 108
    # 的中率 1/5 = 予測 0.2 なので、校正は取れている側
    assert final["status"] == "calibrated" and abs(final["window"]["primary_C"]) < 0.01


def test_error_race_blocks_the_boundary() -> None:
    days = _days(4)
    rows = _rows(days, 10)
    rows.append({"race_id": f"{days[1]}-err", "race_day": days[1], "role": "eligible", "status": "error"})
    state = ps.decision_state(rows, datetime.date(2027, 1, 1))
    assert state["reached"] == {} and state["resolved_through"] == days[0]


def test_deadline_window_and_unavailable_results() -> None:
    days = _days(12, start=datetime.date(2027, 4, 1))
    rows = _rows(days, 3) + [_row(f"{days[-1]}-late", status="pending_result")]
    state = ps.decision_state(rows, datetime.date(2027, 7, 31))
    assert state["window"]["end_day"] == days[-1] and state["window"]["primary_races"] == 36
    assert any(r.get("reason") == "result_unavailable" for r in rows)


def test_not_proven_and_infeasible_are_kept_out_of_the_strategy() -> None:
    rows = _rows(_days(1), 3)
    rows[0]["strategies"]["x_out_2"]["proven_optimal"] = False
    rows[1]["strategies"]["x_out_2"]["status"] = "infeasible"
    rep = ps.window_report(rows, with_bootstrap=False)["strategies"]["x_out_2"]
    assert rep["races"] == 1
    assert rep["excluded"] == {"not_proven_optimal": [rows[0]["race_id"]], "infeasible": [rows[1]["race_id"]]}


# ------------------------------------------------------------------ 記録の不変性（§6）

REG = {"commit": "c" * 40, "config_sha256": "s" * 64}


def test_decision_record_detects_changes() -> None:
    days = _days(12)
    rows = _rows(days, 9)
    state = ps.decision_state([dict(r) for r in rows], datetime.date(2027, 6, 1))
    fixed = ps.decision_record(state, rows, REG)
    assert ps.compare_decision(fixed, ps.decision_record(state, rows, REG))["matches"] is True
    changed = [dict(r) for r in rows]
    changed[0] = {**changed[0], "strategies": {k: {**v, "p_hit": 0.25} for k, v in changed[0]["strategies"].items()}}
    state2 = ps.decision_state(changed, datetime.date(2027, 6, 1))
    check = ps.compare_decision(fixed, ps.decision_record(state2, changed, REG))
    assert check["matches"] is False and check["decision_output_changed"] is True


def test_ledger_keeps_first_boundary(tmp_path: Path) -> None:
    days = _days(4)
    first = ps.decision_state(_rows(days, 10), datetime.date(2027, 1, 1))
    assert ps.update_ledger(first, tmp_path, datetime.date(2027, 1, 1), write=True) == []
    moved = ps.decision_state(_rows(days, 10)[:-15], datetime.date(2027, 1, 2))
    assert ps.update_ledger(moved, tmp_path, datetime.date(2027, 1, 2), write=True) == [30]
    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["boundaries"]["30"]["end_day"] == days[2] and len(ledger["events"]) == 1
    ps.update_ledger(moved, tmp_path, datetime.date(2027, 1, 3), write=True)
    assert len(json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))["events"]) == 1


# ------------------------------------------------------------------ 登録（§1）

def test_registration_and_registered_reference(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _write(tmp_path, "README", b"x")
    _commit(tmp_path, "init", "2026-11-01T10:00:00+09:00")
    _git(tmp_path, "checkout", "-q", "-b", "feat")
    _register(tmp_path, "2026-11-02T10:00:00+09:00", with_reference=True)
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--no-ff", "feat", "-m", "merge", date="2026-11-03T21:30:00+09:00")
    merge = _git(tmp_path, "rev-parse", "HEAD")
    reg = ps.registration(tmp_path)
    assert reg["status"] == "ok" and reg["commit"] == merge and reg["registered_at"] == "2026-11-03T21:30:00+09:00"
    # 後で組み方の設定を変えても、登録コミットの版を読む
    cfg = json.loads((tmp_path / "config/portfolio_v1.json").read_text(encoding="utf-8"))
    cfg["strategies"]["top_prob"]["max_exposure"] = 1
    _write(tmp_path, "config/portfolio_v1.json", cfg)
    _commit(tmp_path, "change", "2026-11-04T10:00:00+09:00")
    ref = ps.load_reference(tmp_path, reg["commit"])
    assert ref["portfolio"]["strategies"]["top_prob"]["max_exposure"] is None
    assert ref["pf"].ma is ref["ma"] and ref["pf"].mb is ref["mb"] and ref["mb"].ma is ref["ma"]
    assert ps.registration(tmp_path)["reference_files"]["config/portfolio_v1.json"]["identical"] is False
    (tmp_path / ps.SPEC_REL).write_text("changed", encoding="utf-8")
    assert ps.registration(tmp_path)["status"] == "spec_changed"


# ------------------------------------------------------------------ 通し

WIN = {1: 2.0, 2: 4.0, 3: 6.0, 4: 10.0, 5: 20.0, 6: 40.0, 7: 60.0}
TRIO = {"-".join(map(str, t)): 4.0 + 2.5 * k for k, t in enumerate(itertools.combinations(range(1, 6), 3))}


def _race(race_id: str, iso: str) -> dict:
    meta = {"source_time": f"{iso} 13:40:00", "observed_at": f"{iso}T14:09:00+09:00"}
    return {"id": race_id, "post_time": "15:45", "entries": [{"num": n, "win_odds": o} for n, o in WIN.items()],
            "combo_odds": {"3連複": TRIO}, "odds_market_meta": {"markets": {"sanrenpuku": meta, "win": meta}}}


def _race_day(repo: Path, day: str, venues: dict[str, dict | None], results: dict) -> None:
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    week = f"W{day}"
    _write(repo, f"raw/{week}.json", {"races": [_race(f"{day}-{v}-11", iso) for v in venues]})
    for v in venues:
        rid = f"{day}-{v}-11"
        _write(repo, f"data/snapshots/{rid}.json", {"race_id": rid, "week_id": week, "pre_race": True,
                                                    "frozen_at": f"{iso}T14:10:00+09:00", "post_time": "15:45",
                                                    "marks": [], "cards": []})
    _commit(repo, f"pre {day}", f"{iso}T14:10:30+09:00")
    for v, res in venues.items():
        if res is not None:
            results[f"{day}-{v}-11"] = res
    _write(repo, ps.RESULTS_REL, results)
    _commit(repo, f"post {day}", f"{iso}T17:00:00+09:00")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    results: dict = {}
    _race_day(tmp_path, "20261101", {"tokyo": {"finish": [1, 2, 3, 4, 5, 6, 7]}}, results)
    _register(tmp_path, "2026-11-05T12:00:00+09:00")
    dead = {"finish": [1, 2, 3, 4], "dividends": {"3連複": [{"horses": [1, 2, 3], "pay": 1}, {"horses": [1, 2, 4], "pay": 1}]}}
    _race_day(tmp_path, "20261107", {"tokyo": {"finish": [1, 2, 3, 4, 5, 6, 7],
                                               "dividends": {"3連複": [{"horses": [1, 2, 3], "pay": 650}]}},
                                     "kyoto": {"finish": [6, 7, 5, 1, 2, 3, 4]},
                                     "niigata": None, "hanshin": dead}, results)
    return tmp_path


def test_end_to_end(repo: Path) -> None:
    out = repo / "out"
    summary = ps.run(repo, out, datetime.date(2026, 11, 8), reference=_ref())
    assert summary["status"] == "collecting" and summary["warnings"] == [] and summary["errors"] == []
    assert summary["eligible"] == 4 and summary["evaluated"] == 2
    assert summary["pending_result"] == ["20261107-niigata-11"]
    assert summary["excluded"] == {"dead_heat_top3": ["20261107-hanshin-11"]}
    assert summary["decision"]["counted_races"] == 4      # 結果待ちと結果の契約で外れたものも数える
    before = json.loads((out / "races/20261101-tokyo-11.json").read_text(encoding="utf-8"))
    assert before["role"] == "before_registration"
    tokyo = json.loads((out / "races/20261107-tokyo-11.json").read_text(encoding="utf-8"))
    top = tokyo["strategies"]["top_prob"]
    assert tokyo["status"] == "evaluated" and tokyo["x_in_top3"] is True and len(top["tickets"]) == 5
    assert top["realized"]["hit"] is True and top["realized"]["payout"] == 650
    rep = summary["to_date"]["strategies"]["top_prob"]
    assert rep["races"] == 2 and rep["hits"] == 1
    p = [json.loads((out / f"races/20261107-{v}-11.json").read_text(encoding="utf-8"))["strategies"]["top_prob"]["p_hit"]
         for v in ("tokyo", "kyoto")]
    assert rep["C"] == pytest.approx((1 - p[0] + 0 - p[1]) / 2, abs=1e-5)
    assert tokyo["strategies"]["cap3_x_out_2"]["evaluated"] is False


def test_error_race_is_reported_and_exit_code_is_nonzero(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ref = _ref()
    real = ref["pf"].portfolio_race

    def broken(race_id, *args, **kwargs):
        if race_id == "20261107-kyoto-11":
            raise RuntimeError("boom")
        return real(race_id, *args, **kwargs)

    monkeypatch.setattr(ref["pf"], "portfolio_race", broken)
    monkeypatch.setattr(ps, "load_reference", lambda repo, commit: ref)
    out = repo / "out"
    assert ps.main(["--repo", str(repo), "--out", str(out)]) == 1
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["errors"] == [{"race_id": "20261107-kyoto-11", "error_type": "RuntimeError"}]
    assert summary["decision"]["unresolved_days"] == ["20261107"] and summary["decision"]["resolved_through"] is None
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, check=True)
    assert all(line[3:].startswith("out/") for line in status.stdout.decode().splitlines())


def test_simulation_cannot_write_to_the_live_output() -> None:
    with pytest.raises(ValueError):
        ps.run(ROOT, ps.OUT_DIR, datetime.date(2027, 1, 1), reference=_ref(), simulate=True)
    with pytest.raises(SystemExit):
        ps.main(["--as-of", "2027-01-01"])


def test_fixed_decision_is_not_overwritten(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ps, "DECISION_RACES", 3)
    monkeypatch.setattr(ps, "MIN_DECISION_RACES", 1)
    monkeypatch.setattr(ps, "MIN_DECISION_DAYS", 1)
    out = repo / "out"
    as_of = datetime.date(2026, 12, 31)
    sim = ps.run(repo, out, as_of, reference=_ref(), simulate=True)
    assert sim["as_of_mode"] == "simulation_on_current_inputs" and not (out / "decision.json").exists()
    first = ps.run(repo, out, as_of, reference=_ref())
    text = (out / "decision.json").read_text(encoding="utf-8")
    fixed = json.loads(text)
    assert first["warnings"] == [] and first["decision_check"]["matches"] is True
    assert set(fixed["inputs"]) == {"20261107-tokyo-11", "20261107-kyoto-11", "20261107-niigata-11", "20261107-hanshin-11"}
    assert fixed["inputs"]["20261107-niigata-11"]["reason"] == "result_unavailable"
    # 結果が後から届いても decision.json は上書きせず、不一致を警告にする
    results = json.loads((repo / ps.RESULTS_REL).read_text(encoding="utf-8"))
    results["20261107-niigata-11"] = {"finish": [1, 2, 3, 4, 5, 6, 7]}
    _write(repo, ps.RESULTS_REL, results)
    _commit(repo, "late result", "2027-01-02T10:00:00+09:00")
    again = ps.run(repo, out, as_of, reference=_ref())
    assert (out / "decision.json").read_text(encoding="utf-8") == text
    assert again["decision_check"]["matches"] is False and "decision_recomputed_differs_from_fixed" in again["warnings"]
