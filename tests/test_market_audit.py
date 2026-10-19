"""research/market_audit.py の検証。一時的な Git リポジトリで、発走前の版への固定と発走後オッズの検出を確かめる。"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from research import market_audit as ma

RACE = "20991011-tokyo-11"
WEEK = "2099-W41"


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _write(repo: Path, rel: str, payload) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _raw(source_time: str, observed_at: str, trio: dict[str, float]) -> dict:
    return {"races": [{
        "id": RACE, "post_time": "15:45", "odds_updated_at": source_time,
        "entries": [{"num": n, "win_odds": 3.0 + n} for n in range(1, 6)],
        "combo_odds": {"3連複": trio},
        "odds_market_meta": {"markets": {"sanrenpuku": {"source_time": source_time, "observed_at": observed_at}}},
    }]}


def _snapshot(frozen_at: str, cards: list[dict]) -> dict:
    return {"race_id": RACE, "week_id": WEEK, "frozen_at": frozen_at, "pre_race": True,
            "post_time": "15:45", "marks": [], "cards": cards}


PRE_TRIO = {"1-2-3": 5.0, "1-2-4": 7.5, "1-3-4": 10.0, "2-3-4": 15.0, "1-2-5": 30.0}
POST_TRIO = {"1-2-3": 4.0, "1-2-4": 9.0}
CARDS = [
    {"char": "gen", "bets": [{"type": "3連複", "horses": [1, 2, 3], "amt": 100},
                             {"type": "3連複", "horses": [1, 2, 4], "amt": 100},
                             {"type": "3連複", "horses": [1, 3, 4], "amt": 100},
                             {"type": "ワイド", "horses": [1, 2], "amt": 200}]},
    {"char": "tetsu", "bets": [{"type": "3連複", "horses": [1, 2, 3], "amt": 100},
                               {"type": "3連複", "horses": [3, 4, 5], "amt": 100}]},
]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path
    _git(repo, "init", "-q", "-b", "main")
    # 発走前の1回目（12:30）
    _write(repo, f"raw/{WEEK}.json", _raw("2099-10-11 12:20:00", "2099-10-11T12:29:00+09:00", PRE_TRIO))
    _write(repo, f"data/snapshots/{RACE}.json", _snapshot("2099-10-11T12:30:00+09:00", CARDS))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pre1", date="2099-10-11T12:30:30+09:00")
    # 発走前の2回目（14:10）。こちらが最終の発走前の版
    _write(repo, f"raw/{WEEK}.json", _raw("2099-10-11 13:40:00", "2099-10-11T14:09:00+09:00", PRE_TRIO))
    _write(repo, f"data/snapshots/{RACE}.json", _snapshot("2099-10-11T14:10:00+09:00", CARDS))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pre2", date="2099-10-11T14:10:30+09:00")
    # scenario-frame の記録（1回目の版を見たもの）
    _write(repo, f"{ma.SCENARIO_FRAME_DIR}/{RACE}/20991011T150000.json", {
        "snapshot_ref": {"frozen_at": "2099-10-11T12:30:00+09:00"}, "action": "bet",
        "bets": [{"type": "3連複", "horses": [1, 2, 3], "amt": 100},
                 {"type": "3連複", "horses": [2, 3, 5], "amt": 100}]})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "record", date="2099-10-11T15:00:30+09:00")
    # 発走後：raw だけ書き換わる（結果取り込み・再実行）
    _write(repo, f"raw/{WEEK}.json", _raw("2099-10-11 15:53:00", "2099-10-11T17:00:00+09:00", POST_TRIO))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "post", date="2099-10-11T17:00:30+09:00")
    return repo


def test_pins_raw_to_last_prerace_snapshot_commit(repo: Path) -> None:
    out = ma.audit_race(RACE, repo)
    assert out["status"] == "audited"
    assert out["prerace_snapshot_versions"] == 2
    assert out["pinned"]["frozen_at"] == "2099-10-11T14:10:00+09:00"
    market = out["market"]
    # 発走後に書き換わった raw ではなく、凍結と同じコミットの raw を読んでいる
    assert market["trio_combos_listed"] == len(PRE_TRIO)
    assert market["trio_source_time"] == "2099-10-11T13:40:00+09:00"
    assert market["post_race_odds"] is False
    assert market["minutes_before_post"] == 125.0
    assert market["trio_combos_total"] == 10
    assert market["trio_inverse_odds_sum"] == pytest.approx(sum(1 / v for v in PRE_TRIO.values()), abs=1e-4)
    # 現在の main（HEAD）の raw には発走後のオッズが入っていることを検出する
    assert out["head_raw"]["post_race_odds"] is True
    assert out["head_raw"]["same_as_pinned"] is False


def test_market_x_is_horse_with_most_listed_mass(repo: Path) -> None:
    out = ma.audit_race(RACE, repo)
    # 1 番は 1-2-3, 1-2-4, 1-3-4, 1-2-5 に入る → 収録組の質量が最大
    assert out["market"]["x"]["horse"] in (1, 2)
    mass = ma.top3_mass(ma.trio_probs(ma.raw_race(out["pinned"]["commit"], WEEK, RACE, repo)))
    assert out["market"]["x"]["horse"] == max(sorted(mass), key=lambda h: mass[h])


def test_card_concentration_and_tickets_left(repo: Path) -> None:
    gen = ma.audit_race(RACE, repo)["cards"]["gen"]
    assert gen["exposure"]["1"] == 4
    assert gen["max_exposure_share"] == 1.0
    assert gen["if_axis_out"] == {"horse": 1, "tickets_containing": 4, "tickets_left": 0, "stake_left": 0}
    assert gen["trio_hit_prob_status"] == "complete"
    assert gen["trio_hit_prob_approx"] == pytest.approx(0.75 / 5 + 0.75 / 7.5 + 0.75 / 10, abs=1e-4)


def test_unlisted_combo_is_unknown_not_zero(repo: Path) -> None:
    tetsu = ma.audit_race(RACE, repo)["cards"]["tetsu"]
    unlisted = [t for t in tetsu["tickets"] if t["horses"] == [3, 4, 5]][0]
    assert unlisted["p_mkt_approx"] is None and unlisted["market_status"] == "not_listed"
    # 未収録の組を含むときは和を出さない（0 とみなさない）
    assert tetsu["trio_hit_prob_approx"] is None
    assert tetsu["trio_hit_prob_status"] == "has_unlisted_combo"


def test_scenario_record_uses_the_version_it_saw(repo: Path) -> None:
    rows = ma.audit_race(RACE, repo)["scenario_frame"]
    assert len(rows) == 1 and rows[0]["status"] == "audited"
    assert rows[0]["snapshot_frozen_at"] == "2099-10-11T12:30:00+09:00"
    assert rows[0]["market_post_race_odds"] is False
    assert rows[0]["if_axis_out"]["tickets_left"] == 0


def test_snapshot_committed_after_post_is_not_audited(tmp_path: Path) -> None:
    repo = tmp_path
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, f"raw/{WEEK}.json", _raw("2099-10-11 13:40:00", "2099-10-11T14:09:00+09:00", PRE_TRIO))
    _write(repo, f"data/snapshots/{RACE}.json", _snapshot("2099-10-11T14:10:00+09:00", CARDS))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "backfill", date="2099-10-12T09:00:00+09:00")
    out = ma.audit_race(RACE, repo)
    assert out["status"] == "no_prerace_commit"
    assert out["post_race_snapshot_commits"] == 1


def test_run_writes_only_under_out_dir(repo: Path, tmp_path_factory) -> None:
    out_dir = tmp_path_factory.mktemp("out")
    summary = ma.run(repo, out_dir)
    assert summary["audited"] == 1
    assert summary["head_raw_post_race_odds"] == [RACE]
    assert summary["pinned_post_race_odds"] == []
    assert (out_dir / "races" / f"{RACE}.json").exists()
    assert (out_dir / "summary.json").exists()
    assert (out_dir / "paired_win_logloss.json").exists()
    assert summary["concentration"]["gen"]["tickets_left_if_axis_out_counts"] == {"0": 1}
    text = (out_dir / "races" / f"{RACE}.json").read_text(encoding="utf-8")
    assert "http" not in text  # URL を写さない
