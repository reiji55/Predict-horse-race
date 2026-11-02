"""research/chat_tickets.py の検証（チャットで出した本線の参考券の記録、chat-ticket-v1）。

記録の形と規約、一時 Git リポジトリでの make → コミット → audit の通し（発走前のコミット・変えていない・
決め方どおり・本線の記録との比較・確かめた結果の台帳）。
"""
from __future__ import annotations

import datetime
import itertools
import json
import os
import subprocess
from pathlib import Path

import pytest

from research import chat_tickets as ct
from research import common, market_audit, market_baseline, portfolio
from research import portfolio_shadow as ps

ROOT = Path(__file__).resolve().parent.parent
RID = "20261107-tokyo-11"
WIN = {1: 2.0, 2: 4.0, 3: 6.0, 4: 10.0, 5: 20.0, 6: 40.0, 7: 60.0}
TRIO_ODDS = {"-".join(map(str, t)): 4.0 + 1.5 * k for k, t in enumerate(itertools.combinations(range(1, 8), 3))}


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
    path.write_bytes(payload) if isinstance(payload, bytes) else path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _commit(repo: Path, msg: str, date: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg, date=date)
    return _git(repo, "rev-parse", "HEAD")


def _jst(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """portfolio-shadow-v1 を登録し、11/7 東京の発走前の版（14:10 凍結、15:45 発走）をコミットしたリポジトリ。"""
    _git(tmp_path, "init", "-q", "-b", "main")
    for rel in (ps.SPEC_REL, ps.CONFIG_REL):
        _write(tmp_path, rel, (ROOT / rel).read_bytes())
    _commit(tmp_path, "register portfolio-shadow", "2026-11-05T12:00:00+09:00")
    meta = {"source_time": "2026-11-07 13:40:00", "observed_at": "2026-11-07T14:09:00+09:00"}
    race = {"id": RID, "post_time": "15:45", "entries": [{"num": n, "win_odds": o} for n, o in WIN.items()],
            "combo_odds": {"3連複": TRIO_ODDS}, "odds_market_meta": {"markets": {"sanrenpuku": meta, "win": meta}}}
    _write(tmp_path, "raw/W20261107.json", {"races": [race]})
    _write(tmp_path, f"data/snapshots/{RID}.json", {"race_id": RID, "week_id": "W20261107", "pre_race": True,
                                                    "frozen_at": "2026-11-07T14:10:00+09:00", "post_time": "15:45",
                                                    "model_id": "m1", "marks": [], "cards": []})
    _commit(tmp_path, "pre 20261107", "2026-11-07T14:10:30+09:00")
    return tmp_path


def _make(repo: Path, at: str = "2026-11-07T14:20:00+09:00") -> tuple[str, dict]:
    return ct.make(RID, repo, rev="HEAD", now=_jst(at), reference=_ref())


def _result(repo: Path, finish: list[int], pay: int, date: str = "2026-11-07T17:00:00+09:00") -> None:
    top3 = sorted(finish[:3])
    _write(repo, ct.RESULTS_REL, {RID: {"finish": finish, "dividends": {"3連複": [{"horses": top3, "pay": pay}]}}})
    _commit(repo, "result", date)


# ------------------------------------------------------------------ make

def test_make_records_the_main_tickets_from_the_given_version(repo: Path) -> None:
    rel, rec = _make(repo)
    assert rel == f"data/chat_tickets/{RID}/20261107T142000.json" and (repo / rel).exists()
    assert ct.validate(rec, rel) == []
    assert rec["action"] == "bet" and len(rec["tickets"]) == 5 and rec["stake"] == 500
    assert rec["pinned"]["commit"] == _git(repo, "rev-parse", "HEAD") and rec["post_at"] == "2026-11-07T15:45:00+09:00"
    assert rec["computed_from"]["engine_commit"] == ps.registration(repo)["commit"]
    # 同じ engine・契約の portfolio_race と同じ券（x_out_2：X を含まない組が2点以上）
    base = portfolio.portfolio_race(RID, None, portfolio.load_config(), market_baseline.load_config(), repo)
    assert rec["tickets"] == sorted(sorted(t["horses"]) for t in base["strategies"]["x_out_2"]["tickets"])
    assert sum(1 for t in rec["tickets"] if rec["x"]["horse"] not in t) >= 2
    assert _git(repo, "worktree", "list").count("\n") == 0   # 一時の作業ツリーは残さない


def test_make_refuses_after_post(repo: Path) -> None:
    with pytest.raises(ValueError):
        _make(repo, "2026-11-07T15:45:00+09:00")
    assert not (repo / ct.RECORDS_REL).exists()


def test_make_passes_when_the_race_is_excluded(repo: Path) -> None:
    raw = json.loads((repo / "raw/W20261107.json").read_text(encoding="utf-8"))
    raw["races"][0]["combo_odds"] = {}
    _write(repo, "raw/W20261107.json", raw)
    _write(repo, f"data/snapshots/{RID}.json", {**json.loads((repo / f"data/snapshots/{RID}.json").read_text()),
                                                "frozen_at": "2026-11-07T14:12:00+09:00"})
    _commit(repo, "pre again without trio odds", "2026-11-07T14:12:30+09:00")
    rel, rec = _make(repo)
    assert rec["action"] == "pass" and rec["tickets"] == [] and rec["stake"] == 0
    assert rec["reason"] == "excluded:no_trio_odds" and ct.validate(rec, rel) == []


# ------------------------------------------------------------------ 形と規約

def _valid() -> tuple[str, dict]:
    rec = {"schema": ct.RECORD_SCHEMA, "race_id": RID, "version": "edge-v0", "strategy": "x_out_2",
           "created_at": "2026-11-07T14:20:00+09:00",
           "computed_from": {"rev": "a" * 40, "engine": ps.VERSION, "engine_commit": "b" * 40},
           "pinned": {"commit": "c" * 40, "frozen_at": "2026-11-07T14:10:00+09:00"},
           "post_at": "2026-11-07T15:45:00+09:00", "action": "bet", "tickets": [[1, 2, 3], [2, 3, 4]],
           "unit_yen": 100, "stake": 200, "p_hit": 0.2, "used_for_prediction": False,
           "official_results_untouched": True, "note": "n"}
    return f"data/chat_tickets/{RID}/20261107T142000.json", rec


@pytest.mark.parametrize("change, problem", [
    ({"created_at": "2026-11-07T15:50:00+09:00"}, "created_after_post"),
    ({"tickets": [[2, 3, 4], [1, 2, 3]]}, "tickets"),            # 並びを決めておく
    ({"tickets": [[1, 1, 3], [2, 3, 4]]}, "tickets"),
    ({"tickets": [[1, 2, 3]] * 2}, "tickets"),
    ({"stake": 500}, "stake"),
    ({"p_hit": 1.5}, "p_hit"),
    ({"action": "pass"}, "pass_needs_reason_and_no_tickets"),
    ({"note": "see https://example.com/?t=x"}, "url"),
    ({"used_for_prediction": True}, "flags"),
    ({"version": "edge-v1"}, "version_or_strategy"),
    ({"extra": 1}, "unknown:extra"),
    ({"computed_from": {"rev": "short", "engine": ps.VERSION, "engine_commit": "b" * 40}}, "computed_from"),
])
def test_validate_rejects(change: dict, problem: str) -> None:
    rel, rec = _valid()
    assert ct.validate(rec, rel) == []
    assert problem in ct.validate({**rec, **change}, rel)


def test_validate_checks_the_path() -> None:
    rel, rec = _valid()
    assert "path" in ct.validate(rec, f"data/chat_tickets/{RID}/20261107T142001.json")
    assert "path" in ct.validate(rec, "data/chat_tickets/20261107-kyoto-11/20261107T142000.json")
    assert ct.validate([], rel) == ["not_an_object"]


def test_validate_all_reports_failures(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rel, rec = _make(repo)
    assert ct.validate_all(repo) == 0
    _write(repo, rel, {**rec, "stake": 100})
    assert ct.validate_all(repo) == 1 and "NG " in capsys.readouterr().out


# ------------------------------------------------------------------ 全履歴での確認

def test_audit_end_to_end(repo: Path) -> None:
    rel, rec = _make(repo)
    _commit(repo, "chat ticket", "2026-11-07T14:25:00+09:00")
    top3 = rec["tickets"][0]
    _result(repo, [*top3, *(h for h in WIN if h not in top3)], 1230)
    out = repo / "out"
    s = ct.audit(repo, out, reference=_ref(), as_of=datetime.date(2026, 11, 8))
    assert s["warnings"] == [] and s["records"] == 1 and s["valid_records"] == 1
    assert s["evaluated"] == 1 and s["hits"] == 1 and s["stake"] == 500 and s["payout"] == 1230
    race = json.loads((out / f"races/{RID}.json").read_text(encoding="utf-8"))
    assert race["shown"] == rel and race["records"][0]["rule_match"] is True
    assert race["realized"] == {"top3": top3, "hit": True, "payout": 1230, "dividend_found": True}
    verified = json.loads((out / "verified.json").read_text(encoding="utf-8"))
    assert verified[rel]["rule_match"] is True and verified[rel]["rev"] == rec["computed_from"]["rev"]


def test_audit_reuses_the_verified_ledger(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make(repo)
    _commit(repo, "chat ticket", "2026-11-07T14:25:00+09:00")
    out = repo / "out"
    assert ct.audit(repo, out, reference=_ref())["warnings"] == []
    monkeypatch.setattr(ct, "compute", lambda *a, **k: (_ for _ in ()).throw(AssertionError("計算し直さない")))
    s = ct.audit(repo, out, reference=_ref())
    assert s["warnings"] == [] and s["valid_records"] == 1 and s["pending_result"] == [RID]


def test_audit_flags_a_record_committed_after_post(repo: Path) -> None:
    _make(repo)
    _commit(repo, "late chat ticket", "2026-11-07T15:50:00+09:00")
    s = ct.audit(repo, repo / "out", reference=_ref())
    assert "invalid_records" in s["warnings"]
    assert "committed_after_post" in s["invalid_records"][0]["problems"]


def test_audit_flags_a_record_changed_after_commit(repo: Path) -> None:
    rel, rec = _make(repo)
    _commit(repo, "chat ticket", "2026-11-07T14:25:00+09:00")
    _write(repo, rel, {**rec, "note": "edited"})
    _commit(repo, "edit", "2026-11-07T14:30:00+09:00")
    s = ct.audit(repo, repo / "out", reference=_ref())
    assert "modified_after_commit" in s["invalid_records"][0]["problems"]


def test_audit_flags_tickets_that_do_not_follow_the_rule(repo: Path) -> None:
    rel, rec = _make(repo)
    others = [list(t) for t in itertools.combinations(range(1, 8), 3) if list(t) not in rec["tickets"]]
    _write(repo, rel, {**rec, "tickets": sorted([rec["tickets"][0], *others[:4]])})
    _commit(repo, "hand-made ticket", "2026-11-07T14:25:00+09:00")
    s = ct.audit(repo, repo / "out", reference=_ref())
    assert "rule_mismatch" in s["invalid_records"][0]["problems"]
    race = json.loads((repo / f"out/races/{RID}.json").read_text(encoding="utf-8"))
    assert race["status"] == "no_valid_record"


def test_audit_compares_with_the_main_record(repo: Path) -> None:
    rel, rec = _make(repo)
    _commit(repo, "chat ticket", "2026-11-07T14:25:00+09:00")
    main = {"race_id": RID, "status": "evaluated", "pinned": rec["pinned"],
            "strategies": {"x_out_2": {"status": "chosen", "tickets": rec["tickets"]}}}
    _write(repo, f"{ct.SHADOW_RACES_REL}/{RID}.json", main)
    _commit(repo, "shadow", "2026-11-07T17:10:00+09:00")
    s = ct.audit(repo, repo / "out", reference=_ref())
    assert s["warnings"] == [] and s["same_tickets_as_main"] == 1 and s["same_pinned_as_main"] == 1
    main["strategies"]["x_out_2"]["tickets"] = [[1, 2, 3]]
    _write(repo, f"{ct.SHADOW_RACES_REL}/{RID}.json", main)
    _commit(repo, "shadow differs", "2026-11-07T17:20:00+09:00")
    assert "differs_from_main_with_same_pinned" in ct.audit(repo, repo / "out", reference=_ref())["warnings"]


def test_audit_uses_the_last_valid_record_before_post(repo: Path) -> None:
    _make(repo, "2026-11-07T14:20:00+09:00")
    _commit(repo, "first", "2026-11-07T14:21:00+09:00")
    rel2, _ = _make(repo, "2026-11-07T15:00:00+09:00")
    _commit(repo, "second", "2026-11-07T15:01:00+09:00")
    ct.audit(repo, repo / "out", reference=_ref())
    race = json.loads((repo / f"out/races/{RID}.json").read_text(encoding="utf-8"))
    assert race["shown"] == rel2 and len(race["records"]) == 2


def test_audit_without_engine_registration(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _commit(tmp_path, "init", "2026-11-01T10:00:00+09:00")
    s = ct.audit(tmp_path, tmp_path / "out")
    assert s["status"] == "registration_not_found" and s["warnings"] == ["engine_registration:registration_not_found"]


def test_repository_records_are_valid() -> None:
    assert ct.validate_all(ROOT) == 0
    assert common.ROOT == ROOT
