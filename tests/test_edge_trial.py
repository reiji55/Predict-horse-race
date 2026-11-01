"""research/edge_trial.py・research/edge_rules.py の検証（edge-v1、印の中の5組）。

登録の導出、登録コミットの版の券の決め方、1レースの計算、本線との比較、見る時点の判定と固定、一時 Git リポジトリでの通し。
"""
from __future__ import annotations

import datetime
import itertools
import json
import os
import random
import subprocess
from pathlib import Path

import pytest

from research import edge_rules
from research import edge_trial as et
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


def _register(repo: Path, date: str) -> str:
    """portfolio-shadow-v1（engine）と edge-v1 を、この順のコミットで登録する。"""
    for rel in (ps.SPEC_REL, ps.CONFIG_REL):
        _write(repo, rel, (ROOT / rel).read_bytes())
    _commit(repo, "register portfolio-shadow", date)
    for rel in (et.SPEC_REL, et.CONFIG_REL, et.RULE_REL, *et.LIVE_DEPENDENCIES):
        _write(repo, rel, (ROOT / rel).read_bytes())
    return _commit(repo, "register edge-v1", date)


# ------------------------------------------------------------------ 固定値

def test_constants_match_registered_config() -> None:
    cfg = json.loads((ROOT / et.CONFIG_REL).read_text(encoding="utf-8"))
    assert cfg["version"] == et.VERSION and cfg["spec"] == et.SPEC_REL
    rule = cfg["rule"]
    assert tuple(rule["mark_symbols"]) == et.MARK_SYMBOLS
    assert rule["min_marked"] == et.MIN_MARKED and rule["max_tickets"] == et.MAX_TICKETS
    assert rule["unit_yen"] == et.UNIT_YEN and rule["function"] == "marks_top_n"
    assert cfg["main"]["strategy"] == et.MAIN_STRATEGY
    dec = cfg["decision"]
    assert tuple(dec["checkpoints"]) == et.CHECKPOINTS and dec["result_wait_days"] == et.RESULT_WAIT_DAYS
    boot = dec["bootstrap"]
    assert (boot["seed"], boot["replicates"], boot["lower_rank"], boot["upper_rank"]) == (
        et.BOOT_SEED, et.BOOT_REPS, et.BOOT_LOWER_RANK, et.BOOT_UPPER_RANK)
    assert boot["level"] == 0.99 and et.BOOT_LOWER_RANK / et.BOOT_REPS == 0.005
    # 券の決め方のファイルは、標準ライブラリ以外を import しない（登録コミットの版だけで動くように）
    src = (ROOT / et.RULE_REL).read_text(encoding="utf-8")
    imports = {ln.split()[1] for ln in src.splitlines() if ln.startswith(("import ", "from "))}
    assert imports <= {"__future__", "itertools", "typing"}


# ------------------------------------------------------------------ 券の決め方（§2）

def test_marked_horses_keeps_marked_runners_only() -> None:
    marks = [{"mk": "◎", "num": 3}, {"mk": "○", "num": 1}, {"mk": "", "num": 2}, {"mk": "△", "num": 9},
             {"mk": "✕", "num": 5}, {"mk": "注", "num": 4}, {"mk": "▲", "num": "6"}]
    assert edge_rules.marked_horses(marks, [1, 2, 3, 4, 5, 6], et.MARK_SYMBOLS) == [1, 3, 5]
    assert edge_rules.marked_horses(None, [1, 2], et.MARK_SYMBOLS) == []


def test_marks_top_n_orders_by_q_then_horse_numbers() -> None:
    dist = {t: 0.01 for t in itertools.combinations(range(1, 9), 3)}
    dist[(2, 5, 7)] = 0.30
    dist[(1, 2, 5)] = 0.20
    dist[(5, 7, 8)] = 0.25   # 8 は印が無いので候補にならない
    got = edge_rules.marks_top_n(dist, [7, 5, 2, 1, 3, 4], 5, 3)
    assert got[:2] == [(2, 5, 7), (1, 2, 5)]
    assert got[2:] == [(1, 2, 3), (1, 2, 4), (1, 2, 7)]   # 同じ q は馬番の辞書順
    assert edge_rules.marks_top_n(dist, [1, 2, 3, 4], 5, 3) == [(1, 2, 3), (1, 2, 4), (1, 3, 4), (2, 3, 4)]
    assert edge_rules.marks_top_n(dist, [1, 2], 5, 3) == []


def test_load_rule_runs_the_registered_source(monkeypatch: pytest.MonkeyPatch) -> None:
    src = (ROOT / et.RULE_REL).read_bytes().replace(b"return candidates[:n]", b"return candidates[:1]")
    monkeypatch.setattr(ps, "_git_bytes", lambda rev, rel, repo: src if (rev, rel) == ("abc", et.RULE_REL) else None)
    rule = et.load_rule(ROOT, "abc")
    dist = {t: 0.1 for t in itertools.combinations(range(1, 7), 3)}
    assert rule.marks_top_n(dist, range(1, 7), 5, 3) == [(1, 2, 3)]   # 今の版ではなく、渡した版で動く
    assert rule.__registered_source__ == f"abc:{et.RULE_REL}"
    with pytest.raises(FileNotFoundError):
        et.load_rule(ROOT, "missing")


# ------------------------------------------------------------------ 本線との比較（§5）

def _row(day: str, k: int, hit1: bool, p1: float, hit0: bool, p0: float, pay1: int = 1000, pay0: int = 1000,
         usable: bool = True, action: str = "bet") -> dict:
    return {"race_id": f"{day}-r{k:02d}-11", "race_day": day, "role": "eligible", "status": "evaluated",
            "action": action, "tickets": [[1, 2, 3]] if action == "bet" else [], "p_pre": p1,
            "stake": 500 if action == "bet" else 0, "pinned": {"commit": "c"}, "result_used": {"top3": [1, 2, 3]},
            "realized": {"hit": hit1, "payout": pay1 if hit1 else 0, "dividend_found": True},
            "main": {"usable": usable, "same_pinned": True, "tickets": [[1, 2, 3]], "p_pre": p0, "stake": 500,
                     "hit": hit0, "payout": pay0 if hit0 else 0, "dividend_found": True}}


def test_report_counts_common_races_and_d() -> None:
    rows = [_row("20261107", 1, True, 0.2, False, 0.3, pay1=3000), _row("20261107", 2, False, 0.1, True, 0.2),
            _row("20261108", 1, False, 0.0, False, 0.2, action="pass"), _row("20261108", 2, True, 0.2, True, 0.1, usable=False)]
    rep = et.report(rows)
    assert rep["evaluated"] == 4 and rep["pass_races"] == 1
    assert rep["v1"]["H"] == 2 and rep["v1"]["E"] == pytest.approx(0.5) and rep["v1"]["stake"] == 1500
    c = rep["common"]
    assert c["races"] == 3 and c["n_day"] == 2 and rep["not_common"] == ["20261108-r02-11"]
    d = [(1 - 0.2) - (0 - 0.3), (0 - 0.1) - (1 - 0.2), (0 - 0.0) - (0 - 0.2)]
    assert c["mean_d"] == pytest.approx(sum(d) / 3)
    assert c["v1"]["roi"] == pytest.approx(3000 / 1000) and c["v0"]["roi"] == pytest.approx(1000 / 1500)
    assert c["hit_overlap"] == {"both": 0, "v1_only": 1, "v0_only": 1, "neither": 1}
    assert c["v1"]["H_over_E"] == pytest.approx(1 / 0.3)


def test_unknown_dividend_is_left_out_of_the_return_only() -> None:
    r = _row("20261107", 1, True, 0.2, False, 0.3)
    r["realized"].update(payout=0, dividend_found=False)
    rep = et.report([r])
    assert rep["v1"]["H"] == 1 and rep["v1"]["stake"] == 0 and rep["v1"]["races_payout_unknown"] == 1


def test_bootstrap_follows_registered_procedure() -> None:
    days = [[0.1, -0.2], [0.3], [-0.1, 0.0, 0.2]]
    got = et.bootstrap(days)
    rng = random.Random(et.BOOT_SEED)
    stats = []
    for _ in range(et.BOOT_REPS):
        s = c = 0
        for _ in range(len(days)):
            i = rng.randrange(len(days))
            s += sum(days[i])
            c += len(days[i])
        stats.append(s / c)
    stats.sort()
    assert got["lower"] == stats[49] and got["upper"] == stats[9949] and got["level"] == 0.99
    assert et.bootstrap([]) == {"n_day": 0, "lower": None, "upper": None}


def _season(n_days: int, per_day: int, v1_hits: bool) -> list[dict]:
    rows, start = [], datetime.date(2026, 11, 7)
    for k in range(n_days):
        day = (start + datetime.timedelta(days=7 * k)).strftime("%Y%m%d")
        for j in range(per_day):
            rows.append(_row(day, j, v1_hits and j == 0, 0.1, False, 0.2, pay1=3000))
    return rows


def test_checkpoint_waits_then_judges_once_reached() -> None:
    rows = _season(15, 4, v1_hits=True)            # 60 レースは15開催日目に初めて達する
    end_day = rows[-1]["race_day"]
    end = datetime.date(int(end_day[:4]), int(end_day[4:6]), int(end_day[6:]))
    early = et.checkpoint_state(rows, end + datetime.timedelta(days=13))
    assert early["reached"] == {"60": end_day} and early["checkpoints"]["60"]["status"] == "awaiting_results"
    st = et.checkpoint_state(rows, end + datetime.timedelta(days=14))
    cp = st["checkpoints"]["60"]
    assert cp["status"] == "final" and len(cp["races"]) == 60
    # 毎開催日に v1 だけが1回当たる → d の平均は (1/4)(1−0.1+0.2) + (3/4)(−0.1+0.2) > 0、回収率も上
    assert cp["bootstrap"]["lower"] > 0 and cp["criterion_met"] is True
    assert set(st["checkpoints"]) == {"60"}


def test_checkpoint_is_not_met_without_higher_return() -> None:
    # d の区間は 0 を超えるが、回収率は本線の方が高い → 基準を満たさない
    rows, start = [], datetime.date(2026, 11, 7)
    for k in range(15):
        day = (start + datetime.timedelta(days=7 * k)).strftime("%Y%m%d")
        for j in range(4):
            rows.append(_row(day, j, j == 0, 0.1, j == 1, 0.9, pay1=100, pay0=5000))
    st = et.checkpoint_state(rows, datetime.date(2027, 6, 1))
    cp = st["checkpoints"]["60"]
    assert cp["bootstrap"]["lower"] > 0 and cp["report"]["v1"]["roi"] < cp["report"]["v0"]["roi"]
    assert cp["criterion_met"] is False


def test_both_missing_everything_counts_lower_p_as_better() -> None:
    # d は「見込みより何回多く当たったか」の差。どちらも外し続けると、見込みの低い方の d が上になる（仕様どおり）
    rows = _season(15, 4, v1_hits=False)
    st = et.checkpoint_state(rows, datetime.date(2027, 6, 1))
    assert st["checkpoints"]["60"]["report"]["mean_d"] == pytest.approx(0.1)


def test_error_race_blocks_the_checkpoint_boundary() -> None:
    rows = _season(15, 4, v1_hits=True)
    rows.append({"race_id": "20261114-zz-11", "race_day": "20261114", "role": "eligible", "status": "error"})
    st = et.checkpoint_state(rows, datetime.date(2027, 6, 1))
    assert st["reached"] == {} and st["resolved_through"] == "20261107" and st["unresolved_days"] == ["20261114"]


def test_checkpoint_record_detects_changes() -> None:
    rows = _season(15, 4, v1_hits=True)
    st = et.checkpoint_state(rows, datetime.date(2027, 6, 1))
    reg = {"commit": "c", "config_sha256": "s"}
    a = et.checkpoint_record(st["checkpoints"]["60"], rows, reg)
    rows[0]["main"]["hit"] = True
    st2 = et.checkpoint_state(rows, datetime.date(2027, 6, 1))
    b = et.checkpoint_record(st2["checkpoints"]["60"], rows, reg)
    assert a["digest"] != b["digest"] and a["inputs_digest"] != b["inputs_digest"]


# ------------------------------------------------------------------ 一時 Git リポジトリでの通し

WIN = {1: 2.0, 2: 4.0, 3: 6.0, 4: 10.0, 5: 20.0, 6: 40.0, 7: 60.0}
# 3-4-5 を最も低いオッズにして、印の中の5組の先頭に来るようにする
TRIO_ODDS = {**{"-".join(map(str, t)): 4.0 + 2.5 * k for k, t in enumerate(itertools.combinations(range(1, 6), 3))},
             "3-4-5": 3.5}
MARKS = [{"mk": "◎", "num": 3}, {"mk": "○", "num": 4}, {"mk": "▲", "num": 5}, {"mk": "△", "num": 6},
         {"mk": "△", "num": 7}, {"mk": "✕", "num": 1}, {"mk": "", "num": 2}]


def _race(race_id: str, iso: str) -> dict:
    meta = {"source_time": f"{iso} 13:40:00", "observed_at": f"{iso}T14:09:00+09:00"}
    return {"id": race_id, "post_time": "15:45", "entries": [{"num": n, "win_odds": o} for n, o in WIN.items()],
            "combo_odds": {"3連複": TRIO_ODDS}, "odds_market_meta": {"markets": {"sanrenpuku": meta, "win": meta}}}


def _main_record(rid: str, pinned: str, hit: bool) -> dict:
    return {"race_id": rid, "status": "evaluated", "pinned": {"commit": pinned},
            "strategies": {"x_out_2": {"status": "chosen", "proven_optimal": True, "tickets": [[1, 2, 3]],
                                       "p_hit": 0.3, "stake": 500,
                                       "realized": {"hit": hit, "payout": 650 if hit else 0, "dividend_found": True}}}}


def _race_day(repo: Path, day: str, venues: dict[str, dict | None], results: dict, marks=MARKS) -> None:
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    week = f"W{day}"
    _write(repo, f"raw/{week}.json", {"races": [_race(f"{day}-{v}-11", iso) for v in venues]})
    for v in venues:
        rid = f"{day}-{v}-11"
        _write(repo, f"data/snapshots/{rid}.json", {"race_id": rid, "week_id": week, "pre_race": True,
                                                    "frozen_at": f"{iso}T14:10:00+09:00", "post_time": "15:45",
                                                    "model_id": "m1", "marks": marks, "cards": []})
    pinned = _commit(repo, f"pre {day}", f"{iso}T14:10:30+09:00")
    for v, res in venues.items():
        if res is not None:
            results[f"{day}-{v}-11"] = res
            top3 = sorted(res["finish"][:3])
            _write(repo, f"{et.MAIN_RACES_REL}/{day}-{v}-11.json", _main_record(f"{day}-{v}-11", pinned, top3 == [1, 2, 3]))
    _write(repo, et.RESULTS_REL, results)
    _commit(repo, f"post {day}", f"{iso}T17:00:00+09:00")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    results: dict = {}
    _race_day(tmp_path, "20261101", {"tokyo": {"finish": [1, 2, 3, 4, 5, 6, 7]}}, results)
    _register(tmp_path, "2026-11-05T12:00:00+09:00")
    dead = {"finish": [3, 4, 5, 1], "dividends": {"3連複": [{"horses": [3, 4, 5], "pay": 1}, {"horses": [1, 3, 4], "pay": 1}]}}
    _race_day(tmp_path, "20261107", {"tokyo": {"finish": [3, 4, 5, 1, 2, 6, 7],
                                               "dividends": {"3連複": [{"horses": [3, 4, 5], "pay": 1230}]}},
                                     "kyoto": {"finish": [1, 2, 3, 4, 5, 6, 7],
                                               "dividends": {"3連複": [{"horses": [1, 2, 3], "pay": 650}]}},
                                     "niigata": None, "hanshin": dead}, results)
    return tmp_path


def test_registration_uses_the_first_main_commit(repo: Path) -> None:
    reg = et.registration(repo)
    assert reg["status"] == "ok" and reg["registered_at"] == "2026-11-05T12:00:00+09:00"
    assert reg["engine"]["status"] == "ok" and reg["rule_module"]["current_differs"] is False
    (repo / et.RULE_REL).write_text("# changed later\n", encoding="utf-8")
    (repo / "research/portfolio_shadow.py").write_text("# changed later\n", encoding="utf-8")
    _commit(repo, "later", "2026-11-06T10:00:00+09:00")
    reg = et.registration(repo)
    assert reg["status"] == "ok" and reg["rule_module"]["current_differs"] is True   # 登録コミットの版を使う
    assert reg["live_dependency_changed"] == ["research/portfolio_shadow.py"]
    (repo / et.SPEC_REL).write_text("changed", encoding="utf-8")
    assert et.registration(repo)["status"] == "spec_changed"


def test_registration_not_found_writes_only_the_status(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _write(tmp_path, "README", b"x")
    _commit(tmp_path, "init", "2026-11-01T10:00:00+09:00")
    out = tmp_path / "out"
    summary = et.run(tmp_path, out, datetime.date(2026, 11, 8))
    assert summary["status"] == "registration_not_found" and summary["warnings"] == ["registration_not_found"]
    assert not (out / "races").exists()


def test_end_to_end(repo: Path) -> None:
    out = repo / "out"
    summary = et.run(repo, out, datetime.date(2026, 11, 8), reference=_ref())
    assert summary["status"] == "collecting" and summary["warnings"] == [] and summary["errors"] == []
    assert summary["eligible"] == 4 and summary["evaluated"] == 2
    assert summary["pending_result"] == ["20261107-niigata-11"]
    assert summary["excluded"] == {"dead_heat_top3": ["20261107-hanshin-11"]}
    before = json.loads((out / "races/20261101-tokyo-11.json").read_text(encoding="utf-8"))
    assert before["role"] == "before_registration" and "tickets" not in before
    tokyo = json.loads((out / "races/20261107-tokyo-11.json").read_text(encoding="utf-8"))
    assert tokyo["marked"] == [1, 3, 4, 5, 6, 7] and tokyo["action"] == "bet" and len(tokyo["tickets"]) == 5
    assert tokyo["pinned"]["model_id"] == "m1" and tokyo["main"]["same_pinned"] is True
    assert all(set(t) <= {1, 3, 4, 5, 6, 7} for t in tokyo["tickets"]) and tokyo["tickets"][0] == [3, 4, 5]
    assert tokyo["realized"] == {"top3": [3, 4, 5], "hit": True, "payout": 1230, "dividend_found": True}
    kyoto = json.loads((out / "races/20261107-kyoto-11.json").read_text(encoding="utf-8"))
    assert kyoto["realized"]["hit"] is False and kyoto["main"]["hit"] is True   # 2番は印が無いので 1-2-3 は買えない
    rep = summary["to_date"]
    assert rep["common"]["races"] == 2 and rep["common"]["hit_overlap"] == {"both": 0, "v1_only": 1, "v0_only": 1,
                                                                           "neither": 0}
    assert rep["v1"]["payout"] == 1230 and rep["common"]["v0"]["payout"] == 650
    assert not (out / "checkpoints.json").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, check=True)
    assert all(line[3:].startswith("out/") for line in status.stdout.decode().splitlines())


def test_too_few_marked_horses_is_a_pass(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _register(tmp_path, "2026-11-05T12:00:00+09:00")
    results: dict = {}
    _race_day(tmp_path, "20261107", {"tokyo": {"finish": [1, 2, 3, 4, 5, 6, 7],
                                               "dividends": {"3連複": [{"horses": [1, 2, 3], "pay": 650}]}}},
              results, marks=[{"mk": "◎", "num": 1}, {"mk": "○", "num": 2}])
    summary = et.run(tmp_path, tmp_path / "out", datetime.date(2026, 11, 8), reference=_ref())
    row = json.loads((tmp_path / "out/races/20261107-tokyo-11.json").read_text(encoding="utf-8"))
    assert row["status"] == "evaluated" and row["action"] == "pass" and row["stake"] == 0 and row["p_pre"] == 0
    assert summary["to_date"]["pass_races"] == 1 and summary["to_date"]["common"]["races"] == 1


def test_error_race_is_reported_and_exit_code_is_nonzero(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = et.trial_race

    def broken(race_id, *args, **kwargs):
        if race_id == "20261107-kyoto-11":
            raise RuntimeError("boom")
        return real(race_id, *args, **kwargs)

    monkeypatch.setattr(et, "trial_race", broken)
    monkeypatch.setattr(ps, "load_reference", lambda repo, commit: _ref())
    out = repo / "out"
    assert et.main(["--repo", str(repo), "--out", str(out)]) == 1
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["errors"] == [{"race_id": "20261107-kyoto-11", "error_type": "RuntimeError"}]
    assert summary["checkpoints"]["unresolved_days"] == ["20261107"]


def test_fixed_checkpoint_is_not_overwritten(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(et, "CHECKPOINTS", (2, 4))
    out = repo / "out"
    s1 = et.run(repo, out, datetime.date(2026, 11, 30), reference=_ref())
    fixed = json.loads((out / "checkpoints.json").read_text(encoding="utf-8"))
    assert list(fixed) == ["2"] and fixed["2"]["decided_on"] == "2026-11-30" and s1["checkpoints_fixed"] == ["2"]
    # 同じ入力なら一致し、上書きもしない
    s2 = et.run(repo, out, datetime.date(2026, 12, 1), reference=_ref())
    assert s2["warnings"] == [] and json.loads((out / "checkpoints.json").read_text(encoding="utf-8")) == fixed
    # 本線の記録が後から変わると、固定した判定と食い違う警告になる（固定は書き換えない）
    rel = f"{et.MAIN_RACES_REL}/20261107-kyoto-11.json"
    main = json.loads((repo / rel).read_text(encoding="utf-8"))
    main["strategies"]["x_out_2"]["realized"]["hit"] = False
    _write(repo, rel, main)
    _commit(repo, "late change", "2026-12-01T10:00:00+09:00")
    s3 = et.run(repo, out, datetime.date(2026, 12, 2), reference=_ref())
    assert s3["warnings"] == ["checkpoint_recomputed_differs_from_fixed"] and s3["checkpoints_mismatched"] == ["2"]
    assert json.loads((out / "checkpoints.json").read_text(encoding="utf-8")) == fixed


def test_simulation_cannot_write_to_the_live_output() -> None:
    with pytest.raises(ValueError):
        et.run(simulate=True)


def test_repository_registration_is_consistent_once_registered() -> None:
    reg = et.registration(ROOT)
    if reg["status"] == "registration_not_found":
        pytest.skip("まだ main に登録されていない（この PR のマージで登録される）")
    assert reg["status"] == "ok", reg["status"]
