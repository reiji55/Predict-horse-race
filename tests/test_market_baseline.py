"""research/market_baseline.py の検証。確率分布の性質（合計 1・未収録組も正）と、時点・結果による除外を確かめる。"""
from __future__ import annotations

import itertools
import json
import math
import os
import subprocess
from pathlib import Path

import pytest

from research import market_baseline as mb

RACE = "20991011-tokyo-11"
WEEK = "2099-W41"


def _race(win: dict[int, float], trio: dict[str, float], source="2099-10-11 13:40:00",
          observed="2099-10-11T14:09:00+09:00") -> dict:
    meta = {"source_time": source, "observed_at": observed}
    return {"id": RACE, "post_time": "15:45", "odds_updated_at": source,
            "entries": [{"num": n, "win_odds": o} for n, o in win.items()],
            "combo_odds": {"3連複": trio},
            "odds_market_meta": {"markets": {"sanrenpuku": dict(meta), "win": dict(meta)}}}


WIN = {1: 2.0, 2: 4.0, 3: 6.0, 4: 10.0, 5: 20.0, 6: 40.0}


def test_harville_is_a_distribution_and_matches_win_probs() -> None:
    p = mb.win_probs(_race(WIN, {}))
    assert sum(p.values()) == pytest.approx(1.0)
    dist = mb.harville_trio(p)
    assert len(dist) == math.comb(6, 3)
    assert sum(dist.values()) == pytest.approx(1.0)
    assert sum(mb.top3_marginals(dist).values()) == pytest.approx(3.0)
    # 本命ほど3着内の確率が高い
    marg = mb.top3_marginals(dist)
    assert marg[1] > marg[2] > marg[3] > marg[6]


def test_discounted_harville_with_unit_lambdas_is_plain_harville() -> None:
    p = mb.win_probs(_race(WIN, {}))
    plain, unit = mb.harville_trio(p), mb.harville_trio(p, (1.0, 1.0))
    assert all(plain[t] == pytest.approx(unit[t]) for t in plain)
    flat = mb.harville_trio(p, (0.81, 0.65))
    assert sum(flat.values()) == pytest.approx(1.0)
    # λ < 1 は2・3着を平らにするので、人気薄の3着内確率が上がる
    assert mb.top3_marginals(flat)[6] > mb.top3_marginals(plain)[6]


def test_market_trio_fills_unlisted_with_positive_capped_mass() -> None:
    trio = {"1-2-3": 4.0, "1-2-4": 6.0, "1-2-5": 12.0, "1-3-4": 9.0, "2-3-4": 15.0, "1-3-5": 20.0}
    race = _race(WIN, trio)
    p = mb.win_probs(race)
    dist, info = mb.market_trio(race, sorted(p), 0.75, mb.harville_trio(p))
    assert sum(dist.values()) == pytest.approx(1.0)
    assert len(dist) == math.comb(6, 3)
    assert info["listed"] == len(trio) and info["unlisted"] == math.comb(6, 3) - len(trio)
    unlisted = [t for t in dist if "-".join(map(str, t)) not in trio]
    cap = info["unlisted_cap_per_trio"]
    assert all(0 < dist[t] <= cap + 1e-9 for t in unlisted)
    # 収録組どうしの比はオッズの逆数の比のまま
    assert dist[(1, 2, 3)] / dist[(1, 2, 4)] == pytest.approx(6.0 / 4.0)


def test_market_trio_negative_residual_keeps_unlisted_nonzero() -> None:
    # 収録組だけで Σ(0.75/odds) が 1 を超える（払戻率の仮定より市場の合計が大きい）
    trio = {"-".join(map(str, t)): 2.0 for t in itertools.combinations(range(1, 6), 3)}
    race = _race(WIN, trio)
    p = mb.win_probs(race)
    dist, info = mb.market_trio(race, sorted(p), 0.75, mb.harville_trio(p))
    assert info["method"] == "residual_negative_used_shape_mass"
    assert sum(dist.values()) == pytest.approx(1.0)
    assert all(dist[t] > 0 for t in dist if 6 in t)


def test_runner_without_win_odds_is_excluded_from_distribution() -> None:
    win = {**WIN, 7: None}
    race = _race(win, {"1-2-3": 4.0, "1-2-7": 8.0})
    p = mb.win_probs(race)
    assert 7 not in p
    dist, info = mb.market_trio(race, sorted(p), 0.75, mb.harville_trio(p))
    assert all(7 not in t for t in dist)
    assert info["dropped_rows_with_runner_without_odds"] == 1


def test_metrics_are_proper_on_actual_trio() -> None:
    p = mb.win_probs(_race(WIN, {}))
    dist = mb.harville_trio(p)
    m = mb.metrics(dist, (1, 2, 3), sorted(p), 1e-12)
    assert m["trio_logloss"] == pytest.approx(-math.log(dist[(1, 2, 3)]), abs=1e-5)
    assert m["actual_trio_rank"] == 1 and m["top5_hit"] is True


# ------------------------------------------------------------------ Git に固定した入力での除外と評価

def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _commit_race(repo: Path, race: dict, results: dict | None) -> None:
    (repo / "raw").mkdir(exist_ok=True)
    (repo / "data/snapshots").mkdir(parents=True, exist_ok=True)
    (repo / f"raw/{WEEK}.json").write_text(json.dumps({"races": [race]}), encoding="utf-8")
    (repo / f"data/snapshots/{RACE}.json").write_text(json.dumps({
        "race_id": RACE, "week_id": WEEK, "frozen_at": "2099-10-11T14:10:00+09:00", "pre_race": True,
        "post_time": "15:45", "marks": [], "cards": []}), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pre", date="2099-10-11T14:10:30+09:00")
    if results is not None:
        (repo / "data/race_results.json").write_text(json.dumps(results), encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "results", date="2099-10-11T17:00:00+09:00")


TRIO = {"1-2-3": 4.0, "1-2-4": 6.0, "1-3-4": 9.0, "2-3-4": 15.0, "1-2-5": 12.0}


def test_evaluated_race_has_all_models(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit_race(tmp_path, _race(WIN, TRIO), {RACE: {"finish": [2, 1, 4, 3, 5, 6]}})
    summary = mb.run(tmp_path, tmp_path / "out")
    assert summary["evaluated"] == 1 and summary["significance"] == "not_assessed"
    row = json.loads((tmp_path / "out/races" / f"{RACE}.json").read_text(encoding="utf-8"))
    assert row["top3"] == [1, 2, 4] and row["winner"] == 2
    assert set(row["models"]) == set(mb.MODELS)
    assert all(abs(v - 1.0) < 1e-6 for v in row["distribution_sums"].values())


def test_unknown_timing_is_excluded_not_treated_as_prerace(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit_race(tmp_path, _race(WIN, TRIO, source="", observed=""), {RACE: {"finish": [2, 1, 4]}})
    summary = mb.run(tmp_path, tmp_path / "out")
    assert summary["evaluated"] == 0
    assert summary["excluded"] == {"odds_timing_not_pre_race_verified": [RACE]}


def test_race_without_result_is_pending(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit_race(tmp_path, _race(WIN, TRIO), None)
    summary = mb.run(tmp_path, tmp_path / "out")
    assert summary["pending_result"] == [RACE]
