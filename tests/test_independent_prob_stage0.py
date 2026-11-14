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


def test_freeze_after_its_commit_is_not_verified(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    frozen = "2026-11-14T13:00:00+09:00"
    snap, race = _snap(frozen=frozen), _race()
    _commit(repo, {SNAP_REL: snap, RAW_REL: _raw(race)}, "2026-11-14T12:55:00+09:00")     # コミットが凍結より前
    prov = s0.provenance(_rec(snap, race, frozen=frozen), "main", repo)
    assert prov["checks"]["snapshot_version"] == "mismatch" and prov["audit_status"] == "mismatch"
    same = _snap(frozen="2026-11-14T12:56:00+09:00", tag="same")
    _commit(repo, {SNAP_REL: same}, "2026-11-14T12:56:00+09:00")                         # 同じ秒は可
    assert s0.provenance(_rec(same, race, frozen="2026-11-14T12:56:00+09:00"), "main", repo)["audit_status"] == "verified"


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
              "observed_at": "2026-11-14T11:31:00+09:00", "odds": {"1": 2.0}}
    after = {**before, "source_time_jst": "2026-11-14T12:05:00+09:00", "observed_at": "2026-11-14T13:00:00+09:00"}
    markets = {m: dict(before if phase in ("pre", "close") else after) for m in s0.OL_MARKETS}
    d = {"race_key": key, "phase": phase, "status": "ok", "post_at": post, "markets": markets}
    if phase == "post2":
        d["final_check"] = {"verified": True, "basis": "stable_after_close",
                            "markets": {m: {"verified": True} for m in s0.OL_MARKETS}}
    for k, v in over.items():
        d[k] = v
    return gzip.compress(json.dumps(d).encode())


def _gz(d: dict) -> bytes:
    return gzip.compress(json.dumps(d).encode())


def _doc(key: str, phase: str) -> dict:
    return json.loads(gzip.decompress(_ol_file(key, phase)))


def _ol_repo(tmp_path: Path, keys: list[str], edit) -> Path:
    repo = _repo(tmp_path)
    rows = [{"race_key": k, "date": "2026-11-14", "post_time": "12:00"} for k in keys]
    files: dict[str, bytes | None] = {"20261114/races/090000.json": json.dumps(
        {"check": {"status": "ok", "races_by_venue": {"東京": len(keys) + 1}}, "races": rows}).encode()}
    for k in keys:
        for ph in s0.PHASES:
            files[f"20261114/{k}/{ph}.json.gz"] = _ol_file(k, ph)
    edit(files, rows)
    files["20261114/races/090000.json"] = json.dumps(
        {"check": {"status": "ok", "races_by_venue": {"東京": len(keys) + 1}}, "races": rows}).encode()
    _commit(repo, {k: v for k, v in files.items() if v is not None}, "2026-11-14T20:00:00+09:00")
    return repo


def test_odds_live_clean_race_is_audited_ok(tmp_path: Path) -> None:
    repo = _ol_repo(tmp_path, ["20261114-tokyo-01"], lambda f, r: None)
    out = s0.odds_live_check("main", ["20261114"], {}, repo)["20261114"]
    assert out["problems"] == [] and out["race_list"]["not_in_list"] == 1
    assert out["phase_audit"] == {ph: {"ok": 1} for ph in s0.PHASES}
    fc = out["races_detail"][0]["phases"]["post2"]["final_check"]
    assert fc["recomputed"] == {m: True for m in s0.OL_MARKETS}


def test_odds_live_problem_phases_are_not_counted_as_audited_ok(tmp_path: Path) -> None:
    keys = [f"20261114-tokyo-0{i}" for i in range(1, 8)]

    def edit(files, rows):
        del files[f"20261114/{keys[1]}/close.json.gz"]                                       # 時点の欠け
        pre = _doc(keys[2], "pre")
        pre["markets"]["win"]["source_time_jst"] = None                                    # 時点が不明
        del pre["markets"]["wide"]                                                         # 券種の欠け
        files[f"20261114/{keys[2]}/pre.json.gz"] = _gz(pre)
        p2 = _doc(keys[0], "post2")
        p2["final_check"]["markets"]["wide"]["verified"] = False                          # 総合 True・券種 False
        files[f"20261114/{keys[0]}/post2.json.gz"] = _gz(p2)
        files[f"20261114/{keys[0]}/close.json.gz"] = _ol_file(keys[0], "close", status="partial", reason="x")
        p2 = _doc(keys[3], "post2")
        p2["final_check"]["verified"] = False                                              # 総合 False・券種 True
        del p2["final_check"]["basis"]                                                     # basis 不明
        files[f"20261114/{keys[3]}/post2.json.gz"] = _gz(p2)
        files[f"20261114/{keys[4]}/pre.json.gz"] = _ol_file(keys[5], "close")              # 別レース・別時点の中身
        p1 = _doc(keys[5], "post1")
        p1["markets"]["win"]["observed_at"] = "2026-11-14T11:50:00+09:00"                  # 発走前の post 観測
        p1["markets"]["umaren"]["odds"] = {"1": 9.9}                                       # post2 と表が違う
        files[f"20261114/{keys[5]}/post1.json.gz"] = _gz(p1)
        rows[6]["post_time"] = ""                                                          # 発走時刻が分からない

    repo = _ol_repo(tmp_path, keys, edit)
    out = s0.odds_live_check("main", ["20261114"], {}, repo)["20261114"]
    probs = set(out["problems"])
    assert f"{keys[1]}:close:missing_file" in probs
    assert f"{keys[2]}:pre:win:timing_unknown" in probs and f"{keys[2]}:pre:wide:market_missing" in probs
    assert f"{keys[0]}:post2:wide:final_check_not_verified" in probs
    assert f"{keys[0]}:post2:final_check_overall_disagrees" in probs
    assert f"{keys[0]}:close:partial:x" in probs
    assert f"{keys[3]}:post2:final_check_overall_disagrees" in probs
    assert f"{keys[3]}:post2:final_check_basis_unknown" in probs
    assert {f"{keys[4]}:pre:race_key_mismatch", f"{keys[4]}:pre:phase_mismatch"} <= probs
    assert f"{keys[5]}:post1:win:timing_observed_before_post" in probs
    assert f"{keys[5]}:post2:umaren:final_check_recomputed_differs" in probs
    assert f"{keys[6]}:pre:post_time_unknown" in probs
    # 収集時は ok でも、点検で問題があった時点は audit の ok に数えない
    assert out["phase_collection"]["pre"] == {"ok": 7}
    assert out["phase_audit"]["pre"] == {"ok": 4, "problem": 3}
    assert out["phase_audit"]["post2"]["ok"] == 3
    assert out["phase_audit"]["close"] == {"missing_file": 1, "problem": 2, "ok": 4}


def test_git_errors_stop_but_missing_paths_are_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert s0._git(["show", "HEAD:no/such/file.json"], repo, ok_missing=True) is None
    with pytest.raises(s0.GitError):
        s0._git(["show", "no-such-ref:x.json"], repo, ok_missing=True)
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


# ------------------------------------------------------------------ 4. run と出力先

def _pinned_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    (tmp_path / "r").mkdir()
    repo = _repo(tmp_path / "r")
    snap, race = _snap(), _race()
    rec = _rec(snap, race)
    keys = ["20261114-tokyo-01"]
    files = {SNAP_REL: snap, RAW_REL: _raw(race), f"{s0.EVAL_DIR_REL}/{RID}.json": json.dumps(rec).encode(),
             s0.RESULTS_REL: b"{}",
             "20261114/races/090000.json": json.dumps({"check": {"status": "ok", "races_by_venue": {"東京": 1}},
                                                       "races": [{"race_key": keys[0], "date": "2026-11-14",
                                                                  "post_time": "12:00"}]}).encode()}
    for ph in s0.PHASES:
        files[f"20261114/{keys[0]}/{ph}.json.gz"] = _ol_file(keys[0], ph)
    sha = _commit(repo, files, "2026-11-14T12:55:00+09:00")
    monkeypatch.setattr(s0, "RUN", {"run_id": "stage0-r1", "main": sha, "odds_live": sha, "from": "20261114",
                                    "to": "20261114", "odds_live_days": ["20261114"]})
    out = tmp_path / "out" / "stage0"
    monkeypatch.setattr(s0, "OUT_DIR", out)
    monkeypatch.setattr(s0.common, "ROOT", repo)
    return repo, out


def test_pinned_run_reproduces_bytes_and_other_runs_never_overwrite_it(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    repo, out = _pinned_repo(tmp_path, monkeypatch)
    assert s0.main([]) == 0
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    assert json.loads(before["summary.json"])["run_id"] == "stage0-r1"
    assert s0.main([]) == 0                                                                 # 同じ入力の再実行は同じバイト列
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    assert s0.main(["--from", "20261113"]) == 2                                             # 別の run には run_id が要る
    assert s0.main(["--from", "20261113", "--run-id", "stage0-r1"]) == 2                    # r1 の名前は使えない
    assert s0.main(["--from", "20261113", "--run-id", "stage0-r2", "--out", str(out)]) == 2  # r1 の場所には書けない
    assert s0.main(["--from", "20261113", "--run-id", "stage0-r2"]) == 0
    other = out.parent / "stage0-r2"
    assert json.loads((other / "summary.json").read_text())["run_id"] == "stage0-r2"
    assert s0.main(["--from", "20261112", "--run-id", "stage0-r2"]) == 2                    # 同じ run_id の違う入力
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before                         # r1 はそのまま
