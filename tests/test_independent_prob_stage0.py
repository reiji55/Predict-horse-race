"""research/independent_prob_stage0.py の検証（Issue #57 Stage 0 監査）。

出所の照合（main の first-parent・生バイトの SHA-256・raw・馬の集合・単勝の時点）、全頭の表、
odds-live の欠け・未検証を成功にしないこと、払戻の照合のまとめ（ChatGPT レビュー PR #59）。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from logic import race_performance as rp
from research import independent_prob_stage0 as s0

RID = "20261114-tokyo-11"          # 発走 15:40（JST）
SNAP_REL = f"data/snapshots/{RID}.json"
RAW_REL = "raw/2026-W46.json"


# ------------------------------------------------------------------ Git の作業場

def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = dict(os.environ)
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "root", when="2026-11-14T08:00:00+09:00")
    return tmp_path


def _commit(repo: Path, files: dict[str, bytes | None], when: str) -> str:
    for rel, data in files.items():
        path = repo / rel
        if data is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "c", when=when)
    return _git(repo, "rev-parse", "HEAD")


def _snap(frozen: str = "2026-11-14T12:50:00+09:00", marks=(1, 2, 3), tag: str = "") -> bytes:
    return json.dumps({"race_id": RID, "pre_race": True, "frozen_at": frozen, "week_id": "2026-W46",
                       "post_time": "15:40", "marks": [{"num": n} for n in marks], "tag": tag}).encode()


def _race(win: dict | None = None, nums=(1, 2, 3, 4)) -> dict:
    entries = [{"num": n, "win_odds": 5.0, "jockey_stats": {"w": 1} if n != 2 else None,
                "trainer_stats": {"w": 1}, "past_runs": [{}]} for n in nums]
    meta = win if win is not None else {"source_time": "2026-11-14 12:40:00", "observed_at": "2026-11-14T12:45:00+09:00"}
    return {"id": RID, "entries": entries, "odds_market_meta": {"markets": {"win": meta}}}


def _raw(race: dict) -> bytes:
    return json.dumps({"races": [race]}).encode()


def _rec(snap: bytes, race: dict | None, frozen: str = "2026-11-14T12:50:00+09:00", nums=(1, 2, 3)) -> dict:
    return {"race_id": RID, "post_time": "15:40", "status": "evaluated", "frozen_at": frozen,
            "probability": {"status": "evaluated", "market": {"status": "available", "log_loss": 1.0},
                            "models": {"champion": {"log_loss": 1.1}},
                            "horses": [{"num": n, "y": int(n == 1), "p_champion": 0.3, "p_q_market": 0.3} for n in nums]},
            "models": {"champion": {"snapshot": {"sha256": hashlib.sha256(snap).hexdigest(), "frozen_at": frozen}},
                       "challenger": {"race_performance": {"input_raw_hash": rp.canonical_sha256(race) if race else None}}}}


# ------------------------------------------------------------------ 1. 出所の照合

def test_verified_race_and_every_raw_horse_is_listed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    snap, race = _snap(), _race()
    _commit(repo, {SNAP_REL: snap, RAW_REL: _raw(race)}, "2026-11-14T12:55:00+09:00")
    _commit(repo, {SNAP_REL: _snap(tag="after")}, "2026-11-14T16:00:00+09:00")     # 発走後の更新は見ない
    rec = _rec(snap, race)
    prov = s0.provenance(rec, "main", repo)
    assert prov["audit_status"] == "verified" and set(prov["checks"].values()) == {"verified"}
    row, horses = s0.race_tables(rec, prov, {"finish": [1, 3, 2, 4]}, "x", "e.json")
    assert [h["num"] for h in horses] == [1, 2, 3, 4] and row["unscored"] == [4]
    h4 = horses[3]
    assert h4["in_raw"] and not h4["in_probability"] and h4["not_evaluated_reason"] == "not_in_snapshot_marks"
    assert h4["p_champion"] is None and h4["q_market"] is None and h4["y"] is None and h4["in_result_finish"]
    assert row["missing"]["jockey_or_trainer"] == [2]
    assert row["source"]["snapshot"]["sha256"] == hashlib.sha256(snap).hexdigest()


def test_digest_mismatch_and_evaluating_an_older_version(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    old, new, race = _snap(tag="old"), _snap(tag="new"), _race()
    _commit(repo, {SNAP_REL: old, RAW_REL: _raw(race)}, "2026-11-14T10:00:00+09:00")
    _commit(repo, {SNAP_REL: new}, "2026-11-14T13:00:00+09:00")
    prov = s0.provenance(_rec(old, race), "main", repo)        # 評価が古い版を見ていた
    assert prov["checks"]["snapshot_digest"] == "mismatch" and "evaluated_other_version" in prov["issues"]
    assert prov["audit_status"] == "mismatch"
    prov = s0.provenance(_rec(b"{}", race), "main", repo)
    assert prov["checks"]["snapshot_digest"] == "mismatch" and "evaluated_unknown_version" in prov["issues"]


def test_missing_raw_and_frozen_at_disagreement_are_not_verified(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    snap = _snap()
    _commit(repo, {SNAP_REL: snap}, "2026-11-14T12:55:00+09:00")
    prov = s0.provenance(_rec(snap, _race()), "main", repo)
    assert prov["checks"]["raw"] == "unknown" and prov["checks"]["horse_set"] == "unknown"
    assert prov["checks"]["input_raw"] == "unknown" and prov["audit_status"] == "unverified"
    prov = s0.provenance(_rec(snap, _race(), frozen="2026-11-14T11:00:00+09:00"), "main", repo)
    assert prov["checks"]["frozen_at"] == "mismatch"


def test_no_prerace_commit_and_branch_merged_after_post(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    snap, race = _snap(), _race()
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, {SNAP_REL: snap, RAW_REL: _raw(race)}, "2026-11-14T12:55:00+09:00")   # ブランチでは発走前
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "m", "side", when="2026-11-14T16:00:00+09:00")   # main は発走後
    prov = s0.provenance(_rec(snap, race), "main", repo)
    assert "no_prerace_commit" in prov["issues"] and prov["checks"]["snapshot_version"] == "unknown"
    assert prov["audit_status"] == "unverified"


def test_same_time_commits_follow_first_parent_order_and_reversed_times_are_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    a, b, race = _snap(tag="a"), _snap(tag="b"), _race()
    _commit(repo, {SNAP_REL: a, RAW_REL: _raw(race)}, "2026-11-14T13:00:00+09:00")
    _commit(repo, {SNAP_REL: b}, "2026-11-14T13:00:00+09:00")
    prov = s0.provenance(_rec(b, race), "main", repo)
    assert prov["audit_status"] == "verified"
    _commit(repo, {SNAP_REL: _snap(tag="c")}, "2026-11-14T12:00:00+09:00")     # 時刻が前に戻る
    prov = s0.provenance(_rec(b, race), "main", repo)
    assert prov["checks"]["commit_order"] == "unknown" and "commit_time_not_monotonic" in prov["issues"]


def test_win_odds_timing_is_three_valued(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    snap = _snap()
    unknown = _race(win={"source_time": "2026-11-14 12:40:00"})                       # observed_at が無い
    _commit(repo, {SNAP_REL: snap, RAW_REL: _raw(unknown)}, "2026-11-14T12:55:00+09:00")
    assert s0.provenance(_rec(snap, unknown), "main", repo)["checks"]["win_odds_timing"] == "unknown"
    reversed_ = _race(win={"source_time": "2026-11-14 12:50:00", "observed_at": "2026-11-14T12:45:00+09:00"})
    # raw だけを変えたコミットは見ない（raw は snapshot の版と同じコミットで読む）
    _commit(repo, {RAW_REL: _raw(reversed_)}, "2026-11-14T12:56:00+09:00")
    assert s0.provenance(_rec(snap, unknown), "main", repo)["checks"]["win_odds_timing"] == "unknown"
    snap2 = _snap(tag="2")
    _commit(repo, {SNAP_REL: snap2}, "2026-11-14T12:57:00+09:00")
    assert s0.provenance(_rec(snap2, reversed_), "main", repo)["checks"]["win_odds_timing"] == "mismatch"


# ------------------------------------------------------------------ 2. 層別

def _h(race_id: str, p: float, q: float | None, y: int, jockey: bool = True) -> dict:
    return {"race_id": race_id, "p_champion": p, "q_market": q, "y": y, "in_probability": True,
            "has_jockey_stats": jockey}


def test_diagnostics_separates_as_recorded_and_audited() -> None:
    races = [{"race_id": "a", "flags": [], "audit_status": "verified", "log_loss": {"champion": 2.0, "market": 1.5}},
             {"race_id": "b", "flags": ["leading_stats_partial"], "audit_status": "unverified",
              "log_loss": {"champion": 1.0, "market": 2.0}},
             {"race_id": "c", "flags": [], "audit_status": "verified", "log_loss": {"champion": 1.0, "market": None}}]
    horses = [_h("a", 0.5, 0.2, 1), _h("a", 0.5, 0.8, 0, jockey=False), _h("b", 0.3, 0.3, 1),
              {**_h("b", 0.0, None, 0), "in_probability": False, "p_champion": None}]
    rec = s0.diagnostics(horses, races, "as_recorded")
    assert rec["races"] == 2 and rec["horses"] == 3          # 市場の無いレース・評価の無い馬は数えない
    assert rec["mean_log_loss"] == {"champion": 1.5, "market": 1.75, "champion_minus_market": -0.25}
    bins = {b["bin"]: (b["horses"], b["actual_wins"]) for b in rec["p_over_q_bins"]}
    assert bins["0.5–0.8"] == (1, 0) and bins["2.0–∞"] == (1, 1)
    assert rec["temperature_sensitivity"]["k=1.0"] == round((-__import__("math").log(0.5) - __import__("math").log(1.0)) / 2, 6)
    audited = s0.diagnostics(horses, races, "audited")
    assert audited["races"] == 1 and audited["horses"] == 2
    assert s0.diagnostics(horses, races, "as_recorded", {"leading_stats_partial"})["races"] == 1


# ------------------------------------------------------------------ 3. odds-live

def _ol_file(key: str, phase: str, **over) -> bytes:
    post = "2026-11-14T12:00:00+09:00"
    before = {"status": "ok", "rows": 5, "source_time_jst": "2026-11-14T11:30:00+09:00",
              "observed_at": "2026-11-14T11:31:00+09:00"}
    after = {**before, "source_time_jst": "2026-11-14T12:05:00+09:00", "observed_at": "2026-11-14T13:00:00+09:00"}
    markets = {m: dict(before if phase in ("pre", "close") else after) for m in s0.OL_MARKETS}
    d = {"race_key": key, "phase": phase, "status": "ok", "post_at": post, "markets": markets}
    if phase == "post2":
        d["final_check"] = {"verified": True, "basis": "stable_after_close",
                            "markets": {m: {"verified": True} for m in s0.OL_MARKETS}}
    for k, v in over.items():
        d[k] = v
    return gzip.compress(json.dumps(d).encode())


def test_odds_live_reports_gaps_and_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    keys = ["20261114-tokyo-01", "20261114-tokyo-02", "20261114-tokyo-03"]
    files: dict[str, bytes | None] = {"20261114/races/090000.json": json.dumps(
        {"check": {"status": "ok", "races_by_venue": {"東京": 4}}, "races": [{"race_key": k} for k in keys]}).encode()}
    for k in keys:
        for ph in s0.PHASES:
            files[f"20261114/{k}/{ph}.json.gz"] = _ol_file(k, ph)
    files[f"20261114/{keys[1]}/close.json.gz"] = None
    del files[f"20261114/{keys[1]}/close.json.gz"]                                            # 時点の欠け
    pre2 = json.loads(gzip.decompress(_ol_file(keys[2], "pre")))
    pre2["markets"]["win"]["source_time_jst"] = None                                         # 時点が不明
    del pre2["markets"]["wide"]                                                              # 券種の欠け
    files[f"20261114/{keys[2]}/pre.json.gz"] = gzip.compress(json.dumps(pre2).encode())
    p2 = json.loads(gzip.decompress(_ol_file(keys[0], "post2")))
    p2["final_check"]["markets"]["wide"]["verified"] = False                                 # 総合は True のまま
    files[f"20261114/{keys[0]}/post2.json.gz"] = gzip.compress(json.dumps(p2).encode())
    files[f"20261114/{keys[0]}/close.json.gz"] = _ol_file(keys[0], "close", status="partial", reason="x")
    _commit(repo, files, "2026-11-14T20:00:00+09:00")
    out = s0.odds_live_check("main", ["20261114"], {}, repo)["20261114"]
    probs = set(out["problems"])
    assert f"{keys[1]}:close:missing_file" in probs
    assert f"{keys[2]}:pre:win:timing_unknown" in probs and f"{keys[2]}:pre:wide:market_missing" in probs
    assert f"{keys[0]}:post2:wide:final_check_not_verified" in probs
    assert f"{keys[0]}:post2:final_check_overall_disagrees" in probs
    assert f"{keys[0]}:close:partial:x" in probs
    assert out["race_list"]["not_in_list"] == 1 and out["phase_state"]["close"] == {"missing_file": 1, "partial:x": 1, "ok": 1}
    with pytest.raises(s0.GitError):
        s0.odds_live_check("no-such-ref", ["20261114"], {}, repo)


def test_payout_match_prefers_mismatch_and_separates_unknown() -> None:
    d = {"markets": {"sanrenpuku": {"status": "ok", "odds": {"2-5-9": 25.9}},
                     "umaren": {"status": "ok", "odds": {"2-9": 5.8}},
                     "wide": {"status": "ok", "odds": {"2-9": [2.4, 2.6], "5-9": [7.0, 7.4]}}}}
    res = {"dividends": {"3連複": [{"horses": [9, 2, 5], "pay": 2590}],
                         "馬連": [{"horses": [2, 9], "pay": 590}],
                         "ワイド": [{"horses": [2, 9], "pay": 250}, {"horses": [5, 9], "pay": 670},
                                  {"horses": [1, 2], "pay": 9990}]}}
    out = s0._payout_match(d, res)
    assert out["3連複"]["match"] == "match" and out["3連複"]["rows"][0]["check"] == "match"
    assert out["馬連"]["match"] == "mismatch"
    # ワイド：幅の外 1 と表に無い組 1 があっても、不一致を優先して隠さない
    assert out["ワイド"]["match"] == "mismatch"
    assert out["ワイド"]["counts"] == {"within_range": 1, "outside_range": 1, "combo_not_listed": 1}
    partial = s0._payout_match({"markets": {"wide": {"status": "ok", "odds": {"2-9": [2.4, 2.6]}}}},
                               {"dividends": {"ワイド": [{"horses": [2, 9], "pay": 250}, {"horses": [1, 2], "pay": 9990}]}})
    assert partial["ワイド"]["match"] == "partially_unknown"
    unlisted = s0._payout_match({"markets": {"sanrenpuku": {"status": "ok", "odds": {}}}},
                                {"dividends": {"3連複": [{"horses": [6, 12, 15], "pay": 1635170}]}})
    assert unlisted["3連複"]["match"] == "unknown" and unlisted["3連複"]["rows"][0]["check"] == "combo_not_listed"
    failed = s0._payout_match({"markets": {"sanrenpuku": {"status": "failed"}}},
                              {"dividends": {"3連複": [{"horses": [1, 2, 3], "pay": 1000}]}})
    assert failed["3連複"]["rows"][0]["check"] == "price_unavailable" and failed["3連複"]["match"] == "unknown"
