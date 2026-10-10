"""チャット実験（chat-experiment-record-v1）の記録・検証・採点。

すべて一時ディレクトリの合成データで確かめる。リポジトリの実データ（data/chat_experiments/）は読まない
（記録の形が崩れていても、予想パイプラインの事前テストを止めないため。実データの検証は CI の別ステップ）。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from logic import model_registry
from research import chat_experiment as ce

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ce.load_config()
EXP = "scenario-frame"
RACE = "20261011-tokyo-11"          # 発走 15:45
KYOTO = "20261011-kyoto-11"         # 発走 15:35
RULE = f"data/chat_experiments/{EXP}/rule_v1.md"
SNAP = f"data/snapshots/{RACE}.json"
RULE_TEXT = """# scenario-frame rule v1

## 4. 枠（シナリオ）の定義
| ラベル | 入れる条件 |
|---|---|
| 先行残り | 前走で4角3番手以内 |
| 差し届く | 上がり3F最速が2回以上 |
"""
# 1-2-3 着。ワイドは 1-2 / 1-3 / 2-3、馬連は 1-2 だけ
RESULT = {
    "finish": [1, 2, 3, 4, 5, 6, 7, 8],
    "dividends": {
        "ワイド": [{"horses": [1, 2], "pay": 300}, {"horses": [1, 3], "pay": 450},
                   {"horses": [2, 3], "pay": 900}],
        "馬連": [{"horses": [1, 2], "pay": 800}],
        "3連複": [{"horses": [1, 2, 3], "pay": 2500}],
    },
}


def _snapshot(race_id=RACE, frozen_at="2026-10-11T13:20:00+09:00", gen_bets=None, post_time=None):
    gen = gen_bets or [{"type": "ワイド", "horses": [1, 3], "amt": 300},
                       {"type": "ワイド", "horses": [4, 5], "amt": 200}]
    return {
        "race_id": race_id, "pre_race": True, "frozen_at": frozen_at,
        "post_time": post_time or ("15:35" if race_id == KYOTO else "15:45"),
        "model_id": "win-v1-speed-guard", "config_hash": "abc",
        "marks": [{"num": n, "name": f"H{n}", "odds": float(2 * n), "score": 80 - n} for n in range(1, 9)],
        "cards": [
            {"char": "kei", "bets": [{"type": "ワイド", "horses": [1, 2], "amt": 500}]},
            {"char": "tetsu", "action": "pass", "bets": []},
            {"char": "gen", "bets": gen},
            {"char": "chappy", "bets": [{"type": "馬連", "horses": [1, 2], "amt": 1000}]},
        ],
    }


def _record(race_id=RACE, version="v1", frozen_at="2026-10-11T13:20:00+09:00",
            created_at="2026-10-11T14:00:00+09:00"):
    frames = {1: ["先行残り"], 2: ["差し届く"], 3: ["先行残り", "差し届く"]}
    return {
        "schema_version": "chat-experiment-record-v1",
        "experiment_id": EXP,
        "rule_ref": {"path": f"data/chat_experiments/{EXP}/rule_{version}.md", "version": version},
        "race_id": race_id,
        "created_at": created_at,
        "author": "ChatGPT",
        "snapshot_ref": {"path": f"data/snapshots/{race_id}.json", "frozen_at": frozen_at},
        "baseline_char": "gen",
        "budget": 500,
        "action": "bet",
        "pass_reason": None,
        "candidates": [{"num": n, "frames": frames.get(n, []),
                        "evidence": "EVIDENCE-TEXT" if n in frames else ""} for n in range(1, 9)],
        "bets": [
            {"type": "ワイド", "horses": [1, 3], "amt": 200, "frame_tag": "先行残り"},
            {"type": "ワイド", "horses": [1, 2], "amt": 200, "frame_tag": "先行残り"},
            {"type": "馬連", "horses": [2, 3], "amt": 100, "frame_tag": "差し届く"},
        ],
        "summary": "SUMMARY-TEXT",
    }


def _record_rel(stamp="20261011T140000", race=RACE):
    return f"data/chat_experiments/{EXP}/races/{race}/{stamp}.json"


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    return path


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _commit(repo: Path, files: dict[str, object], date: str) -> None:
    for rel, payload in files.items():
        _write(repo / rel, payload)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c", date=date)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """ルール v1（09:00）と snapshot（frozen 13:20、コミット 13:21）を登録済みのリポジトリ。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, {RULE: RULE_TEXT}, "2026-10-11T09:00:00+09:00")
    _commit(repo, {SNAP: _snapshot()}, "2026-10-11T13:21:00+09:00")
    return repo


# ------------------------------------------------------------------ 構造と規約

def test_a_record_that_follows_the_rule_passes():
    assert ce.validate(_record(), CONFIG, _snapshot(), RULE_TEXT) == []
    terse = _record()
    for cand in terse["candidates"]:
        if not cand["frames"]:
            del cand["evidence"]                                   # 枠に入れない馬は根拠を省いてよい
    assert ce.validate(terse, CONFIG, _snapshot(), RULE_TEXT) == []
    passed = _record()
    passed.update(action="pass", bets=[], pass_reason="2歳戦で全頭の出走が2回以下")
    assert ce.validate(passed, CONFIG, _snapshot(), RULE_TEXT) == []


@pytest.mark.parametrize("mutate,expected", [
    (lambda r: r["bets"][0].update(amt=300), "budget 500 円と一致しません"),
    (lambda r: r["bets"][0].update(amt=150), "100 円単位"),
    (lambda r: r["bets"][2].update(horses=[2, 3, 4]), "馬番の数が券種と合わない"),
    (lambda r: r["bets"][2].update(horses=[2, 5]), "5番はどの枠にも入っていません"),
    (lambda r: r["bets"][2].update(horses=[2, 9]), "9番が candidates にありません"),
    (lambda r: r["bets"][0].update(frame_tag="穴狙い"), "frame_tag がルールに書かれていません"),
    (lambda r: r["candidates"][3].update(frames=["穴狙い"], evidence="x"), "ルールに書かれていない枠のラベルがあります（4番）"),
    (lambda r: r["candidates"][0].update(evidence=""), "1番: 枠に入れた馬は evidence"),
    (lambda r: r["candidates"].pop(), "snapshot の出走馬が candidates にありません"),
    (lambda r: r["candidates"].append({"num": 9, "frames": [], "evidence": ""}), "snapshot に無い馬番"),
    (lambda r: r["candidates"].append({"num": 1, "frames": [], "evidence": ""}), "1番が candidates に重複"),
    (lambda r: r.update(action="pass"), "bets を空にします"),
    (lambda r: r.update(action="pass", bets=[]), "pass_reason"),
    (lambda r: r.update(baseline_char="otori"), "baseline_char は"),
    (lambda r: r.update(budget=1500), "budget は 100 円単位・1000 円以下"),
    (lambda r: r.update(created_at="2026-10-11T15:50:00+09:00"), "created_at が発走時刻以降"),
    (lambda r: r["rule_ref"].update(path="rules/v1.md"), "rule_ref.path は"),
    (lambda r: r["snapshot_ref"].update(frozen_at="13時20分"), "snapshot_ref.frozen_at"),
    (lambda r: r.update(race_id="tokyo-11"), "race_id は"),
    (lambda r: r["candidates"][0].update(evidence="https://example.com/race?id=1 を参照"), "URL"),
])
def test_a_record_that_breaks_the_rule_is_rejected(mutate, expected):
    record = _record()
    mutate(record)
    errors = ce.validate(record, CONFIG, _snapshot(), RULE_TEXT)
    assert any(expected in e for e in errors), errors


def test_error_messages_do_not_copy_free_text_from_the_record():
    # エラー文言は採点の出力（ワークフローがコミットする公開ファイル）に残るので、記録の自由記述を写さない
    record = _record()
    record["candidates"][0]["evidence"] = "https://user:pw@example.com/a?token=SECRET1#frag"
    record["candidates"][3].update(frames=["SECRET2"], evidence="SECRET3")
    record["bets"][0]["frame_tag"] = "SECRET4"
    record["bets"][1]["type"] = "SECRET5"
    record["baseline_char"] = "SECRET6"
    errors = ce.validate(record, CONFIG, _snapshot(), RULE_TEXT)
    assert len(errors) >= 5
    assert "SECRET" not in json.dumps(errors, ensure_ascii=False)


def test_only_urls_with_a_query_fragment_or_userinfo_are_flagged():
    assert ce.tokened_url_count({"evidence": "https://db.example.com/horse/123/ の成績"}) == 0
    assert ce.tokened_url_count({"evidence": ["https://example.com/x?id=1"]}) == 1
    assert ce.tokened_url_count({"evidence": "http://example.com/a#top"}) == 1
    assert ce.tokened_url_count({"evidence": "https://u:p@example.com/"}) == 1


def test_ci_validation_checks_the_layout_and_the_rule_file_without_git(tmp_path: Path):
    root = tmp_path / "work"
    _write(root / RULE, RULE_TEXT)
    _write(root / SNAP, _snapshot())
    path = _write(root / _record_rel(), _record())
    assert ce.validate_file(path, CONFIG, repo=root) == ([], True)
    # 浅い clone で過去の版が無いときは、出走馬の網羅の照合だけを省く（最終判定は全履歴の verify・採点）
    stale = _write(root / _record_rel("20261011T141000"), _record(frozen_at="2026-10-11T12:00:00+09:00"))
    assert ce.validate_file(stale, CONFIG, repo=root) == ([], False)
    misplaced = _write(root / _record_rel(race=KYOTO), _record())
    assert any("記録の置き場所は" in e for e in ce.validate_file(misplaced, CONFIG, repo=root)[0])
    (root / RULE).unlink()
    assert any("ルールファイル" in e for e in ce.validate_file(path, CONFIG, repo=root)[0])


# ------------------------------------------------------------------ Git の記録で発走前性を検証

def test_a_record_committed_before_post_is_verified(repo: Path):
    _commit(repo, {_record_rel(): _record()}, "2026-10-11T14:00:00+09:00")
    result = ce.verify(repo / _record_rel(), CONFIG, repo=repo)
    assert result["errors"] == []
    assert result["prerace_verified"] is True
    assert result["post_at"] == "2026-10-11T15:45:00+09:00"
    assert result["rule_registered_at"] == "2026-10-11T09:00:00+09:00"
    assert result["snapshot_committed_at"] == "2026-10-11T13:21:00+09:00"


def test_the_snapshot_version_is_found_by_frozen_at_after_it_is_overwritten(repo: Path):
    # 同じ時刻の別表記（UTC）でも同じ版。発走前の再実行で上書きされても、Git 履歴から見た版を探す
    _commit(repo, {_record_rel(): _record(frozen_at="2026-10-11T04:20:00+00:00")}, "2026-10-11T14:00:00+09:00")
    later = _snapshot(frozen_at="2026-10-11T15:00:00+09:00",
                      gen_bets=[{"type": "ワイド", "horses": [7, 8], "amt": 500}])
    _commit(repo, {SNAP: later}, "2026-10-11T15:01:00+09:00")
    result = ce.verify(repo / _record_rel(), CONFIG, repo=repo)
    assert result["prerace_verified"] is True
    assert result["snapshot_frozen_at"] == "2026-10-11T13:20:00+09:00"


def _committed_after_post(repo: Path) -> str:
    _commit(repo, {_record_rel(): _record()}, "2026-10-11T15:50:00+09:00")
    return _record_rel()


def _modified_after_post(repo: Path) -> str:
    _commit(repo, {_record_rel(): _record()}, "2026-10-11T14:00:00+09:00")
    changed = _record()
    changed["bets"][0]["horses"] = [2, 3]
    _commit(repo, {_record_rel(): changed}, "2026-10-11T16:00:00+09:00")
    return _record_rel()


def _rule_modified(repo: Path) -> str:
    # 発走前でも、ルールを書き換えたらその version の記録は検証に通らない（変えるなら新しい version）
    _commit(repo, {RULE: RULE_TEXT + "\n- 追記\n"}, "2026-10-11T12:00:00+09:00")
    _commit(repo, {_record_rel(): _record()}, "2026-10-11T14:00:00+09:00")
    return _record_rel()


def _rule_after_record(repo: Path) -> str:
    _commit(repo, {_record_rel(): _record(version="v2")}, "2026-10-11T14:00:00+09:00")
    _commit(repo, {f"data/chat_experiments/{EXP}/rule_v2.md": RULE_TEXT}, "2026-10-11T14:30:00+09:00")
    return _record_rel()


def _unknown_frozen_at(repo: Path) -> str:
    _commit(repo, {_record_rel(): _record(frozen_at="2026-10-11T12:00:00+09:00")}, "2026-10-11T14:00:00+09:00")
    return _record_rel()


def _snapshot_after_record(repo: Path) -> str:
    _commit(repo, {_record_rel(): _record(frozen_at="2026-10-11T15:00:00+09:00")}, "2026-10-11T14:00:00+09:00")
    _commit(repo, {SNAP: _snapshot(frozen_at="2026-10-11T15:00:00+09:00")}, "2026-10-11T15:01:00+09:00")
    return _record_rel()


def _misplaced(repo: Path) -> str:
    _commit(repo, {_record_rel(race=KYOTO): _record()}, "2026-10-11T14:00:00+09:00")
    return _record_rel(race=KYOTO)


def _not_committed(repo: Path) -> str:
    _write(repo / _record_rel(), _record())
    return _record_rel()


@pytest.mark.parametrize("setup,expected", [
    (_committed_after_post, "発走時刻以降のものがあります"),
    (_modified_after_post, "発走時刻以降のものがあります"),
    (_rule_modified, "追加の後に書き換え・削除されています"),
    (_rule_after_record, "ルールファイルが記録より後にコミットされています"),
    (_unknown_frozen_at, "snapshot の版が Git 履歴にありません"),
    (_snapshot_after_record, "見た snapshot の版が、記録より後にコミットされています"),
    (_misplaced, "記録の置き場所は"),
    (_not_committed, "記録が Git にコミットされていません"),
])
def test_a_record_without_prerace_proof_is_not_verified(repo: Path, setup, expected):
    result = ce.verify(repo / setup(repo), CONFIG, repo=repo)
    assert result["prerace_verified"] is False
    assert any(expected in e for e in result["errors"]), result["errors"]


# ------------------------------------------------------------------ 採点

def test_settle_scores_the_last_prerace_record_against_the_cards_of_the_same_snapshot(repo: Path, tmp_path: Path):
    early = _record(created_at="2026-10-11T13:50:00+09:00")
    early["bets"] = [{"type": "ワイド", "horses": [1, 2], "amt": 500, "frame_tag": "先行残り"}]
    # 最後の案は、ファイル名ではなくコミット時刻で決める
    _commit(repo, {_record_rel("20261011T140000"): early}, "2026-10-11T13:55:00+09:00")
    _commit(repo, {_record_rel("20261011T135000"): _record()}, "2026-10-11T14:05:00+09:00")
    _commit(repo, {SNAP: _snapshot(frozen_at="2026-10-11T15:00:00+09:00",
                                   gen_bets=[{"type": "ワイド", "horses": [7, 8], "amt": 500}])},
            "2026-10-11T15:01:00+09:00")
    late = _record(created_at="2026-10-11T14:30:00+09:00")
    late["bets"] = [{"type": "ワイド", "horses": [2, 3], "amt": 500, "frame_tag": "差し届く"}]
    _commit(repo, {_record_rel("20261011T143000"): late}, "2026-10-11T16:00:00+09:00")   # 発走後

    out = tmp_path / "out"
    summaries = ce.settle({RACE: RESULT}, CONFIG, repo=repo, output_dir=out)
    summary = json.loads((out / EXP / "summary.json").read_text(encoding="utf-8"))
    assert summaries[EXP] == summary
    assert summary["official_results_untouched"] is True
    v1 = summary["versions"]["v1"]
    assert (v1["races_settled"], v1["races_pending"]) == (1, 0)
    race = v1["races"][0]
    assert race["record"] == _record_rel("20261011T135000")
    assert race["superseded"] == [_record_rel("20261011T140000")]
    rejected = summary["rejected_records"]
    assert [r["path"] for r in rejected] == [_record_rel("20261011T143000")]
    assert any("発走時刻以降" in e for e in rejected[0]["errors"])

    experiment = race["experiment"]
    assert (experiment["spent"], experiment["payout"], experiment["hit_bets"], experiment["top3_capture"]) \
        == (500, 1500, 2, 3)                                        # ワイド 1-3（900）＋ 1-2（600）、馬連 2-3 は外れ
    assert race["cards"]["gen"]["payout"] == 1350                   # 見た版（13:20）の源のカード。15:00 の版ではない
    assert race["cards"]["tetsu"]["action"] == "pass"
    assert v1["primary_baseline"]["payout"] == 1350
    assert v1["paired_payout_diff_vs_primary_baseline"]["mean"] == 150
    assert v1["experiment"]["roi"] == 3.0
    assert v1["top3_finishers"] == {"total": 3, "with_any_frame": 3, "rate": 1.0}
    assert v1["by_frame_tag"]["先行残り"] == {"bets": 2, "hit_bets": 2, "spent": 400, "payout": 1500}
    assert race["bet_horses_finish"] == {"1": 1, "2": 2, "3": 3}
    text = json.dumps(summary, ensure_ascii=False)
    assert "EVIDENCE-TEXT" not in text and "SUMMARY-TEXT" not in text   # 根拠の文章は写さない


def test_versions_are_settled_separately_with_pass_and_pending_races(repo: Path, tmp_path: Path):
    _commit(repo, {f"data/chat_experiments/{EXP}/rule_v2.md": RULE_TEXT,
                   f"data/snapshots/{KYOTO}.json": _snapshot(race_id=KYOTO)}, "2026-10-11T09:30:00+09:00")
    passed = _record(version="v2")
    passed.update(action="pass", bets=[], pass_reason="どの枠にも2頭以上入らない")
    _commit(repo, {
        _record_rel(): _record(),                                  # v1・東京
        _record_rel("20261011T140100"): passed,                    # v2・東京（見送り）
        _record_rel(race=KYOTO): _record(race_id=KYOTO),           # v1・京都（結果待ち）
    }, "2026-10-11T14:05:00+09:00")
    summary = ce.settle({RACE: RESULT}, CONFIG, repo=repo, output_dir=tmp_path / "out")[EXP]
    v1, v2 = summary["versions"]["v1"], summary["versions"]["v2"]
    assert (v1["races_settled"], v1["races_pending"], v1["experiment"]["payout"]) == (1, 1, 1500)
    assert (v2["races_settled"], v2["experiment"]["pass_races"], v2["experiment"]["spent"]) == (1, 1, 0)
    assert v2["primary_baseline"]["payout"] == 1350               # 見送っても、比べる相手は同じレースで採点する
    assert v2["paired_payout_diff_vs_primary_baseline"]["mean"] == -1350
    assert v1["rule"]["registered_at"] == "2026-10-11T09:00:00+09:00"
    assert v2["rule"]["registered_at"] == "2026-10-11T09:30:00+09:00"


def test_a_registered_rule_shows_up_before_any_race(repo: Path, tmp_path: Path):
    summary = ce.settle({}, CONFIG, repo=repo, output_dir=tmp_path / "out")[EXP]
    assert summary["versions"]["v1"]["rule"] == {
        "path": RULE, "registered_at": "2026-10-11T09:00:00+09:00", "unmodified": True}
    assert summary["versions"]["v1"]["races_settled"] == 0


def test_records_deleted_after_post_stay_visible(repo: Path, tmp_path: Path):
    _commit(repo, {_record_rel(): _record()}, "2026-10-11T14:00:00+09:00")
    _commit(repo, {_record_rel("20261011T141000"): _record()}, "2026-10-11T14:10:00+09:00")
    (repo / _record_rel()).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "delete", date="2026-10-11T16:30:00+09:00")
    summary = ce.settle({RACE: RESULT}, CONFIG, repo=repo, output_dir=tmp_path / "out")[EXP]
    assert summary["deleted_records"] == [
        {"path": _record_rel(), "deleted_at": "2026-10-11T16:30:00+09:00", "after_post": True}]


# ------------------------------------------------------------------ 本番との切り離し

def test_results_workflow_settles_chat_experiments_after_shadow_scoring_without_blocking():
    results = (ROOT / ".github" / "workflows" / "run_results.yml").read_text(encoding="utf-8")
    at = results.index("python -m research.chat_experiment settle --results data/race_results.json")
    step = results[results.rindex("- name:", 0, at):at]
    assert "continue-on-error: true" in step
    assert results.index("python -m research.settle --results") < at < results.index("生成物をコミット")
    assert "tests/test_chat_experiment.py" in results
    assert "fetch-depth: 0" in results               # 発走前性は全履歴のコミット時刻で検証する
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python -m research.chat_experiment validate" in ci


def test_the_experiment_config_is_not_part_of_the_prediction_config_hash():
    assert "chat_experiment.json" not in model_registry.HASH_CONFIGS
