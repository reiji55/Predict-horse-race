"""research/portfolio.py の検証。制約付きの組み方が総当たりと一致すること、条件付き確率と払戻の計算、Git に固定した入力での通し。"""
from __future__ import annotations

import itertools
import json
import os
import random
import subprocess
from pathlib import Path

import pytest

from research import portfolio as pf


def _dist(n_horses: int, seed: int) -> dict[tuple[int, int, int], float]:
    rng = random.Random(seed)
    trios = list(itertools.combinations(range(1, n_horses + 1), 3))
    raw = {t: rng.random() ** 3 for t in trios}
    total = sum(raw.values())
    return {t: v / total for t, v in raw.items()}


def _brute(dist, x, n, cap, min_out):
    best = None
    for combo in itertools.combinations(dist, n):
        exp = {}
        for t in combo:
            for h in t:
                exp[h] = exp.get(h, 0) + 1
        if cap is not None and max(exp.values()) > cap:
            continue
        if sum(1 for t in combo if x not in t) < min_out:
            continue
        s = sum(dist[t] for t in combo)
        best = s if best is None or s > best else best
    return best


@pytest.mark.parametrize("seed", range(3))
@pytest.mark.parametrize("cap,min_out", [(None, 0), (None, 2), (3, 0), (2, 0), (3, 3)])
def test_choose_matches_brute_force(seed: int, cap, min_out) -> None:
    dist = _dist(7, seed)
    x = 1
    got = pf.choose(dist, x, 5, cap, min_out)
    want = _brute(dist, x, 5, cap, min_out)
    if want is None:
        assert got["status"] == "infeasible"
        return
    assert got["status"] == "chosen" and got["proven_optimal"] is True
    assert sum(dist[tuple(t)] for t in got["tickets"]) == pytest.approx(want, abs=1e-12)
    exp = {}
    for t in got["tickets"]:
        for h in t:
            exp[h] = exp.get(h, 0) + 1
    assert cap is None or max(exp.values()) <= cap
    assert sum(1 for t in got["tickets"] if x not in t) >= min_out
    assert len({tuple(t) for t in got["tickets"]}) == 5


def test_infeasible_when_field_too_small() -> None:
    dist = _dist(4, 1)          # 4 組しか無い
    assert pf.choose(dist, 1, 5, None, 0)["status"] == "infeasible"


def test_node_limit_reports_not_proven() -> None:
    got = pf.choose(_dist(12, 3), 1, 5, 2, 2, node_limit=3)
    assert got["proven_optimal"] is False and got["status"] == "search_incomplete"
    some = pf.choose(_dist(8, 0), 1, 5, 2, 3, node_limit=6)
    assert some["status"] == "chosen" and some["proven_optimal"] is False


def test_exposure_cap_3_implies_two_without_x() -> None:
    """5点で露出上限3なら、X を含まない組は必ず2点以上（cap3_x_out_2 と exposure_cap_3 は同じ組になる）。"""
    for seed in range(5):
        dist = _dist(9, seed)
        a = pf.choose(dist, 1, 5, 3, 0)
        b = pf.choose(dist, 1, 5, 3, 2)
        assert a["tickets"] == b["tickets"]
        assert sum(1 for t in a["tickets"] if 1 not in t) >= 2


def test_evaluate_conditional_probabilities_and_payout() -> None:
    dist = _dist(6, 7)
    x = 2
    tickets = [[1, 2, 3], [2, 4, 5], [1, 3, 4], [3, 5, 6], [1, 2, 6]]
    odds = {tuple(t): 10.0 + i for i, t in enumerate(tickets)}
    result = {"finish": [4, 1, 3, 2, 5, 6],
              "dividends": {"3連複": [{"horses": [1, 3, 4], "pay": 1230}]}}
    out = pf.evaluate(tickets, dist, x, odds, {"v": dist}, result, 100)
    p_x = sum(p for t, p in dist.items() if x in t)
    p_in = sum(dist[tuple(t)] for t in tickets if x in t)
    p_out = sum(dist[tuple(t)] for t in tickets if x not in t)
    assert out["p_hit"] == pytest.approx(p_in + p_out)
    assert out["p_hit_given_x_in"] == pytest.approx(p_in / p_x)
    assert out["p_hit_given_x_out"] == pytest.approx(p_out / (1 - p_x))
    assert out["tickets_without_x"] == 2 and out["max_exposure"] == {"horse": 1, "tickets": 3}
    assert [t["branch"] for t in out["tickets"]] == ["primary_X_in", "primary_X_in", "alternate_X_out",
                                                     "alternate_X_out", "primary_X_in"]
    assert out["realized"] == {"top3": [1, 3, 4], "hit": True, "payout": 1230, "dividend_found": True}
    assert out["expected_return_market"] == pytest.approx(
        sum(dist[tuple(t)] * odds[tuple(t)] for t in tickets) / 5)
    # 未収録の組を含むと期待回収は出さない
    partial = {k: v for k, v in odds.items() if k != (3, 5, 6)}
    assert pf.evaluate(tickets, dist, x, partial, {}, None, 100)["expected_return_market"] is None


# ------------------------------------------------------------------ Git に固定した入力での通し

RACE = "20991011-tokyo-11"
WEEK = "2099-W41"
WIN = {1: 2.0, 2: 4.0, 3: 6.0, 4: 10.0, 5: 20.0, 6: 40.0, 7: 60.0}


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _race(trio: dict[str, float]) -> dict:
    meta = {"source_time": "2099-10-11 13:40:00", "observed_at": "2099-10-11T14:09:00+09:00"}
    return {"id": RACE, "post_time": "15:45", "entries": [{"num": n, "win_odds": o} for n, o in WIN.items()],
            "combo_odds": {"3連複": trio}, "odds_market_meta": {"markets": {"sanrenpuku": meta, "win": meta}}}


def _commit(repo: Path, race: dict, results: dict | None, record: dict | None = None) -> None:
    (repo / "raw").mkdir(exist_ok=True)
    (repo / "data/snapshots").mkdir(parents=True, exist_ok=True)
    (repo / f"raw/{WEEK}.json").write_text(json.dumps({"races": [race]}), encoding="utf-8")
    (repo / f"data/snapshots/{RACE}.json").write_text(json.dumps({
        "race_id": RACE, "week_id": WEEK, "frozen_at": "2099-10-11T14:10:00+09:00", "pre_race": True,
        "post_time": "15:45", "marks": [], "cards": []}), encoding="utf-8")
    if record is not None:
        path = repo / f"{pf.ma.SCENARIO_FRAME_DIR}/{RACE}/20991011T123000.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pre", date="2099-10-11T14:10:30+09:00")
    if results is not None:
        (repo / "data/race_results.json").write_text(json.dumps(results), encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "results", date="2099-10-11T17:00:00+09:00")


TRIO = {"-".join(map(str, t)): 4.0 + 2.5 * k for k, t in enumerate(itertools.combinations(range(1, 6), 3))}


def test_end_to_end_strategies_and_v1_record(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    record = {"bets": [{"type": "3連複", "horses": h, "amt": 100}
                       for h in ([1, 2, 3], [1, 2, 4], [1, 3, 4], [2, 3, 4], [5, 6, 7])]}
    results = {RACE: {"finish": [1, 3, 2, 4, 5, 6, 7], "dividends": {"3連複": [{"horses": [1, 2, 3], "pay": 650}]}}}
    _commit(tmp_path, _race(TRIO), results, record)
    summary = pf.run(tmp_path, tmp_path / "out")
    assert summary["used"] == 1 and summary["excluded"] == {} and summary["not_proven_optimal"] == []
    row = json.loads((tmp_path / "out/races" / f"{RACE}.json").read_text(encoding="utf-8"))
    assert row["status"] == "evaluated" and row["pace"] == "pace_unknown" and row["x"]["horse"] == 1
    top = row["strategies"]["top_prob"]
    assert top["status"] == "chosen" and top["n_tickets"] == 5 and top["stake"] == 500
    assert top["p_hit_variants"]["min"] <= top["p_hit"] <= top["p_hit_variants"]["max"]
    capped = row["strategies"]["exposure_cap_3"]
    assert capped["max_exposure"]["tickets"] <= 3 and capped["tickets_without_x"] >= 2
    assert capped["p_hit"] <= top["p_hit"] + 1e-12
    v1 = row["scenario_frame_v1"][0]
    assert v1["n_tickets"] == 5 and v1["realized"]["hit"] is True and v1["realized"]["payout"] == 650
    # 一時リポジトリの記録は正式な v1 として確かめられないので、有効扱いにしない
    assert v1["official_v1_valid"] is False
    assert summary["scenario_frame_v1"]["official_v1_valid_records"]["races"] == 0
    assert summary["strategies"]["top_prob"]["realized_counterfactual"]["stake"] == 500


def test_unknown_timing_and_missing_odds_are_excluded(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    race = _race(TRIO)
    race["entries"].append({"num": 8, "win_odds": None})
    _commit(tmp_path, race, None)
    summary = pf.run(tmp_path, tmp_path / "out")
    assert summary["excluded"] == {"runner_without_win_odds_unconfirmed": [RACE]}


# ------------------------------------------------------------------ PR #44 レビュー：失敗の見え方

RACE2 = "20991011-kyoto-11"


def _two_races(repo: Path) -> None:
    races = [_race(TRIO), {**_race(TRIO), "id": RACE2}]
    (repo / "raw").mkdir(exist_ok=True)
    (repo / "data/snapshots").mkdir(parents=True, exist_ok=True)
    (repo / f"raw/{WEEK}.json").write_text(json.dumps({"races": races}), encoding="utf-8")
    for rid in (RACE, RACE2):
        (repo / f"data/snapshots/{rid}.json").write_text(json.dumps({
            "race_id": rid, "week_id": WEEK, "frozen_at": "2099-10-11T14:10:00+09:00", "pre_race": True,
            "post_time": "15:45", "marks": [], "cards": []}), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pre", date="2099-10-11T14:10:30+09:00")


def test_race_error_is_reported_and_exit_code_is_nonzero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _two_races(tmp_path)
    real = pf.ma.prerace_versions

    def broken(race_id, repo):
        if race_id == RACE2:
            raise RuntimeError("boom")
        return real(race_id, repo)

    monkeypatch.setattr(pf.ma, "prerace_versions", broken)
    out = tmp_path / "out"
    assert pf.main(["--repo", str(tmp_path), "--out", str(out)]) == 1
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["errors"] == [{"race_id": RACE2, "error_type": "RuntimeError"}]
    assert summary["used"] == 1 and summary["excluded"] == {"RuntimeError": [RACE2]}
    row = json.loads((out / "races" / f"{RACE2}.json").read_text(encoding="utf-8"))
    assert row["status"] == "error" and "boom" not in json.dumps(row)
    # 書くのは出力先だけ（予想・snapshot・Champion などリポジトリの中身は変えない）
    status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, check=True)
    assert all(line[3:].startswith("out/") for line in status.stdout.decode().splitlines())

    monkeypatch.setattr(pf.ma, "prerace_versions", real)
    assert pf.main(["--repo", str(tmp_path), "--out", str(out)]) == 0


def test_not_proven_optimal_is_kept_out_of_comparisons(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, _race(TRIO), None)
    config = pf.load_config()
    config["search"]["node_limit"] = 3
    summary = pf.run(tmp_path, tmp_path / "out", config=config)
    assert summary["not_proven_optimal"] == [RACE]
    assert all(v == [] for v in summary["infeasible"].values())     # 打ち切りは「満たせない」ではない
    assert all(block["races"] == 0 for block in summary["strategies"].values())
    assert all(c["races"] == 0 for c in summary["constraint_cost_vs_top_prob"].values())
