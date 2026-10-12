"""チャットで作った実験ロジック（仮説）の、発走前の記録・検証・採点（chat-experiment-record-v1）。

チャット（ChatGPT など）で組んだ買い目のロジックを、本番に入れる前に前向きに試すための記録。
結果を見てから「当たる理由」を作れないよう、ルールと買い目を発走前に Git に残し、結果が出たら
記録が見たのと同じ版の snapshot にある現行のカードと並べて採点する。

置き場所（どちらも追記のみ）:
    data/chat_experiments/{experiment_id}/rule_{version}.md
        ルールの本文。最初のレースより前に1回だけ追加し、以後は書き換えない。
        変えたくなったら新しい version のファイルを足す。集計は version ごとに分け、混ぜない。
    data/chat_experiments/{experiment_id}/races/{race_id}/{YYYYmmddTHHMMSS}.json
        1レース1案の記録。発走前にコミットする。出し直すときは新しいファイルを足す
        （発走前の最後の案を採点し、それより前の案は superseded として残す）。

発走前性の検証（verify。採点に使ってよいか）:
    1. 構造と規約（validate）に通る。出走馬の網羅は、記録が見た版の snapshot と照合する
    2. created_at < 発走時刻
    3. 記録に触れたコミット（追加・修正）がすべて発走時刻より前
    4. ルールファイルに触れたコミットは追加の1回だけで、その時刻 ≤ 記録の最初のコミット時刻
    5. snapshot_ref.frozen_at と同じ frozen_at を持つ snapshot の版が Git 履歴にあり、発走前の版で、
       その版のコミット時刻 ≤ 記録の最初のコミット時刻
   時刻は Git のコミット時刻で見る。GitHub の Web 画面や API（ChatGPT のコネクタなど）で作った
   コミットは、時刻を GitHub が付ける。手元からの git push は時刻を偽装できるので、記録には使わない。

採点（settle）は shadow 研究用で、data/shadow/chat_experiments/{experiment_id}/summary.json にだけ書く。
正式成績（data/results.json）・UI・予想には使わない。根拠の文章（evidence・summary など）は写さない。
エラー文言にも記録の自由記述を写さない（採点の出力はワークフローがコミットし、リポジトリは公開のため）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import statistics
import subprocess
import sys
import urllib.parse
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from research import common
from research.decision_log import BET_SIZE, canonical_snapshot_path
from results import build_results

logger = logging.getLogger("research.chat_experiment")

CONFIG_PATH = common.ROOT / "config" / "chat_experiment.json"
RECORD_DIR = common.ROOT / "data" / "chat_experiments"
SUMMARY_SCHEMA = "chat-experiment-summary-v1"
RACE_ID = re.compile(r"\d{8}-[a-z]+-\d{2}")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
SUMMARY_NOTE = (
    "チャットで作った実験ロジックの shadow 採点。発走前の検証（Git のコミット時刻・ルールの不変・"
    "記録が見た snapshot の版）に通った記録だけを使い、ルールの version ごとに分けて集計する。"
    "比べる相手は、記録が見たのと同じ版の snapshot にある現行のカード。正式成績・UI・予想には使わない。"
    "少数のレースでは偶然の影響が大きいので、ルールに書いた評価レース数に達するまで結論を出さない。"
)


def load_config(path: Path | None = None) -> dict[str, Any]:
    return common.load_config(path or CONFIG_PATH)


def rule_path(experiment_id: str, version: str) -> str:
    return f"data/chat_experiments/{experiment_id}/rule_{version}.md"


def race_dir(experiment_id: str, race_id: str) -> str:
    return f"data/chat_experiments/{experiment_id}/races/{race_id}"


def record_paths(directory: Path | None = None) -> list[Path]:
    return sorted((directory or RECORD_DIR).glob("*/races/*/*.json"))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _rel(path: Path, repo: Path) -> str:
    path = path if path.is_absolute() else repo / path
    return path.relative_to(repo).as_posix() if path.is_relative_to(repo) else path.as_posix()


def _ids(record: dict[str, Any], config: dict[str, Any]) -> tuple[str, str, str] | None:
    """(experiment_id, version, race_id)。どれかが決まった形でなければ None（パスの組み立てに使わない）。"""
    exp_id = record.get("experiment_id")
    version = _dict(record.get("rule_ref")).get("version")
    race_id = record.get("race_id")
    if (isinstance(exp_id, str) and re.fullmatch(config["experiment_id_pattern"], exp_id)
            and isinstance(version, str) and re.fullmatch(config["rule_version_pattern"], version)
            and isinstance(race_id, str) and RACE_ID.fullmatch(race_id)):
        return exp_id, version, race_id
    return None


# ------------------------------------------------------------------ 構造と規約

def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def tokened_url_count(value: Any) -> int:
    """クエリ・フラグメント・ユーザー情報を含む URL の数。公開リポジトリに残さないため。URL 自体は返さない。"""
    count = 0
    for text in _strings(value):
        for url in _URL.findall(text):
            try:
                parts = urllib.parse.urlsplit(url)
            except ValueError:
                count += 1
                continue
            count += bool(parts.query or parts.fragment or "@" in parts.netloc)
    return count


def _label_ok(value: Any, config: dict[str, Any]) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and len(value) <= int(config["frame_label_max_length"]))


def _positive_multiple(value: Any, unit: int) -> bool:
    return type(value) is int and value > 0 and value % unit == 0


def validate(record: dict[str, Any], config: dict[str, Any],
             snapshot: dict[str, Any] | None = None, rule_text: str | None = None) -> list[str]:
    """構造と規約の検証。エラー文言のリストを返す（空なら合格）。

    snapshot（記録が見た版）を渡すと、出走馬の網羅と created_at < 発走時刻を確かめる。
    rule_text（ルールの本文）を渡すと、枠のラベルがルールに書かれているかを確かめる。
    """
    errors: list[str] = []
    if record.get("schema_version") != config["record_schema_version"]:
        errors.append(f"schema_version は {config['record_schema_version']} です")
    exp_id, race_id = record.get("experiment_id"), record.get("race_id")
    exp_ok = isinstance(exp_id, str) and bool(re.fullmatch(config["experiment_id_pattern"], exp_id))
    if not exp_ok:
        errors.append("experiment_id は英小文字・数字・ハイフンの名前です（例 scenario-frame）")
    rule_ref = _dict(record.get("rule_ref"))
    version = rule_ref.get("version")
    if not (isinstance(version, str) and re.fullmatch(config["rule_version_pattern"], version)):
        errors.append("rule_ref.version は v1, v2 … の形です")
    elif exp_ok and rule_ref.get("path") != rule_path(exp_id, version):
        errors.append(f"rule_ref.path は {rule_path(exp_id, version)} です")
    race_ok = isinstance(race_id, str) and bool(RACE_ID.fullmatch(race_id))
    if not race_ok:
        errors.append("race_id は 20261011-tokyo-11 の形です")
    created = common.parse_dt(record.get("created_at"))
    if created is None:
        errors.append("created_at が日時（ISO 8601）ではありません")
    snap_ref = _dict(record.get("snapshot_ref"))
    if race_ok and snap_ref.get("path") != canonical_snapshot_path(race_id):
        errors.append(f"snapshot_ref.path は {canonical_snapshot_path(race_id)} です")
    if common.parse_dt(snap_ref.get("frozen_at")) is None:
        errors.append("snapshot_ref.frozen_at（見た snapshot の frozen_at）が日時ではありません")
    if record.get("baseline_char") not in config["baseline_chars"]:
        errors.append(f"baseline_char は {' / '.join(config['baseline_chars'])} のどれかです")
    unit, max_budget = int(config["amt_unit"]), int(config["max_budget"])
    budget = record.get("budget")
    if not (_positive_multiple(budget, unit) and budget <= max_budget):
        errors.append(f"budget は {unit} 円単位・{max_budget} 円以下の正の整数です")
        budget = None
    action = record.get("action")
    if action not in ("bet", "pass"):
        errors.append("action は bet（買う）か pass（見送り）です")

    # --- 出走馬（全頭）と枠 ----------------------------------------------
    candidates = record.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        errors.append("candidates が空です（出走馬を全頭並べます）")
        candidates = []
    by_num: dict[int, list[str]] = {}
    for i, cand in enumerate(candidates, start=1):
        num = _dict(cand).get("num")
        if type(num) is not int or num <= 0:
            errors.append(f"candidates の {i} 件目: num（馬番）が正の整数ではありません")
            continue
        if num in by_num:
            errors.append(f"{num}番が candidates に重複しています")
        frames = cand.get("frames")
        if not isinstance(frames, list) or not all(_label_ok(f, config) for f in frames):
            errors.append(f"{num}番: frames は枠のラベル（{config['frame_label_max_length']}文字以内）"
                          "のリストです。どの枠にも入れないなら []")
            frames = []
        evidence = cand.get("evidence")
        if evidence is not None and not isinstance(evidence, str):
            errors.append(f"{num}番: evidence（根拠）は文字列です")
        elif frames and not (evidence or "").strip():
            errors.append(f"{num}番: 枠に入れた馬は evidence（根拠）が必須です")
        by_num[num] = frames
    if rule_text is not None:
        undefined = sorted(n for n, frames in by_num.items() if any(f not in rule_text for f in frames))
        if undefined:
            errors.append(f"ルールに書かれていない枠のラベルがあります（{', '.join(map(str, undefined))}番）")
    if snapshot is not None:
        runners = {int(m["num"]) for m in snapshot.get("marks") or []
                   if isinstance(m, dict) and m.get("num") is not None}
        missing, extra = sorted(runners - set(by_num)), sorted(set(by_num) - runners)
        if missing:
            errors.append(f"snapshot の出走馬が candidates にありません（取消・除外も含めて全頭）: {missing}")
        if extra:
            errors.append(f"snapshot に無い馬番が candidates にあります: {extra}")
        post = common.post_at(race_id, snapshot.get("post_time")) if race_ok else None
        if created and post and created >= post:
            errors.append("created_at が発走時刻以降です（発走前の記録ではありません）")

    # --- 買い目 ------------------------------------------------------------
    bets = record.get("bets")
    if not isinstance(bets, list):
        errors.append("bets はリストです（見送りなら []）")
        bets = []
    if action == "pass":
        if bets:
            errors.append("action=pass のときは bets を空にします")
        reason = record.get("pass_reason")
        if not (isinstance(reason, str) and reason.strip()):
            errors.append("action=pass のときは pass_reason（見送りの理由）が必須です")
    elif action == "bet":
        if not bets:
            errors.append("action=bet なのに bets が空です")
        total = 0
        for i, bet in enumerate(bets, start=1):
            bet = _dict(bet)
            bet_type, horses, amt = bet.get("type"), bet.get("horses"), bet.get("amt")
            horses_ok = (isinstance(horses, list) and all(type(n) is int for n in horses)
                         and len(set(horses)) == len(horses))
            if bet_type not in config["bet_types"]:
                errors.append(f"買い目{i}: 券種は {' / '.join(config['bet_types'])} のどれかです")
            elif not horses_ok or len(horses) != BET_SIZE[bet_type]:
                errors.append(f"買い目{i}: 馬番の数が券種と合わないか、重複・整数でない馬番があります")
            if _positive_multiple(amt, unit):
                total += amt
            else:
                errors.append(f"買い目{i}: 金額は {unit} 円単位の正の整数です")
            tag = bet.get("frame_tag")
            if not _label_ok(tag, config):
                errors.append(f"買い目{i}: frame_tag（どの枠から作った券か）が必須です")
            elif rule_text is not None and tag not in rule_text:
                errors.append(f"買い目{i}: frame_tag がルールに書かれていません")
            for num in horses if horses_ok else []:
                if num not in by_num:
                    errors.append(f"買い目{i}: {num}番が candidates にありません")
                elif not by_num[num]:
                    errors.append(f"買い目{i}: {num}番はどの枠にも入っていません（買えるのは枠に入れた馬だけ）")
        if budget is not None and bets and total != budget:
            errors.append(f"買い目の合計 {total} 円が budget {budget} 円と一致しません")

    if tokened_url_count(record):
        errors.append("クエリ（? 以降）・#・ユーザー情報つきの URL があります。公開リポジトリなので書かないでください")
    return errors


def _layout_error(rel: str, ids: tuple[str, str, str]) -> str | None:
    exp_id, _, race_id = ids
    if str(PurePosixPath(rel).parent) != race_dir(exp_id, race_id):
        return f"記録の置き場所は {race_dir(exp_id, race_id)}/ です"
    return None


# ------------------------------------------------------------------ Git の記録

def _git(args: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=30)


def _commits(rel_path: str, repo: Path, *options: str) -> list[tuple[str, datetime.datetime]]:
    """そのパスに触れたコミットの (SHA, コミット時刻)。最初・最後は並び順に頼らず時刻の min / max で決める。"""
    out = _git(["log", *options, "--format=%H %cI", "--", rel_path], repo)
    rows = []
    for line in out.stdout.decode().splitlines() if out.returncode == 0 else []:
        sha, _, stamp = line.partition(" ")
        when = common.parse_dt(stamp)
        if sha and when:
            rows.append((sha, when))
    return rows


def _show(sha: str, rel_path: str, repo: Path) -> bytes | None:
    out = _git(["show", f"{sha}:{rel_path}"], repo)
    return out.stdout if out.returncode == 0 else None


def rule_registration(experiment_id: str, version: str, repo: Path | None = None) -> dict[str, Any]:
    """ルールファイルの登録（最初の追加コミット）と、その後に書き換え・削除が無いか。"""
    repo = repo or common.ROOT
    rel = rule_path(experiment_id, version)
    info: dict[str, Any] = {"path": rel, "registered_at": None, "unmodified": False, "text": None}
    added = _commits(rel, repo, "--diff-filter=A")
    if added:
        sha, when = min(added, key=lambda c: c[1])
        blob = _show(sha, rel, repo)
        info.update(registered_at=when,
                    unmodified=[c for c, _ in _commits(rel, repo)] == [sha],
                    text=blob.decode("utf-8", errors="replace") if blob is not None else None)
    return info


def snapshot_version(race_id: str, frozen_at: Any, repo: Path | None = None) -> dict[str, Any] | None:
    """frozen_at が同じ snapshot の版を Git 履歴から探す。複数あれば最初にコミットされた版。

    snapshot は発走前の再実行で上書きされるので、記録が見た版は過去のコミットにしか無いことがある。
    ChatGPT は SHA-256 を計算できないので、版は frozen_at（凍結時刻＝オッズの観測時刻）で特定する。
    """
    repo = repo or common.ROOT
    target = common.parse_dt(frozen_at)
    rel = canonical_snapshot_path(race_id)
    found = []
    for sha, when in _commits(rel, repo) if target else []:
        blob = _show(sha, rel, repo)
        try:
            snapshot = json.loads(blob.decode("utf-8")) if blob is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            snapshot = None
        if isinstance(snapshot, dict) and common.parse_dt(snapshot.get("frozen_at")) == target:
            found.append((when, snapshot, blob))
    if not found:
        return None
    when, snapshot, blob = min(found, key=lambda f: f[0])
    return {"snapshot": snapshot, "committed_at": when, "sha256": common.sha256_bytes(blob),
            "versions_with_same_frozen_at": len(found)}


def _verify(path: Path, config: dict[str, Any], repo: Path
            ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """(検証結果, 記録, 見た版の snapshot の情報)。"""
    rel = _rel(path, repo)
    result: dict[str, Any] = {
        "path": rel, "experiment_id": None, "rule_version": None, "race_id": None,
        "post_at": None, "created_at": None,
        "record_first_commit_at": None, "record_last_commit_at": None,
        "rule_registered_at": None, "rule_unmodified": False,
        "snapshot_frozen_at": None, "snapshot_committed_at": None, "snapshot_sha256": None,
        "errors": [], "prerace_verified": False,
    }
    record = common.read_json(path)
    if not isinstance(record, dict):
        result["errors"] = ["JSON のオブジェクトとして読めません"]
        return result, None, None
    ids = _ids(record, config)
    found = rule = None
    if ids:
        exp_id, version, race_id = ids
        found = snapshot_version(race_id, _dict(record.get("snapshot_ref")).get("frozen_at"), repo)
        rule = rule_registration(exp_id, version, repo)
        result.update(experiment_id=exp_id, rule_version=version, race_id=race_id)
    snapshot = found["snapshot"] if found else None
    errors = validate(record, config, snapshot, rule["text"] if rule else None)

    times = [when for _, when in _commits(rel, repo)]
    first, last = (min(times), max(times)) if times else (None, None)
    post = common.post_at(ids[2], snapshot.get("post_time")) if ids and snapshot else None
    if ids:
        layout = _layout_error(rel, ids)
        if layout:
            errors.append(layout)
        if found is None:
            errors.append("snapshot_ref.frozen_at と同じ frozen_at の snapshot の版が Git 履歴にありません")
        else:
            frozen = common.parse_dt(snapshot.get("frozen_at"))
            if snapshot.get("pre_race") is not True or post is None or frozen is None or frozen >= post:
                errors.append("見た snapshot の版が、発走前に凍結されたものではありません")
            if first and found["committed_at"] > first:
                errors.append("見た snapshot の版が、記録より後にコミットされています")
        if rule["registered_at"] is None:
            errors.append(f"ルールファイル {rule['path']} が Git にありません（最初のレースより前にコミットします）")
        else:
            if not rule["unmodified"]:
                errors.append(f"ルールファイル {rule['path']} が追加の後に書き換え・削除されています"
                              "（変えるなら新しい version を足します）")
            if first and rule["registered_at"] > first:
                errors.append("ルールファイルが記録より後にコミットされています")
    if not times:
        errors.append("記録が Git にコミットされていません")
    elif post and last >= post:
        errors.append("記録のコミット（追加・修正）に、発走時刻以降のものがあります")

    result.update({
        "post_at": _iso(post),
        "created_at": _iso(common.parse_dt(record.get("created_at"))),
        "record_first_commit_at": _iso(first),
        "record_last_commit_at": _iso(last),
        "rule_registered_at": _iso(rule["registered_at"]) if rule else None,
        "rule_unmodified": bool(rule and rule["unmodified"]),
        "snapshot_frozen_at": snapshot.get("frozen_at") if snapshot else None,
        "snapshot_committed_at": _iso(found["committed_at"]) if found else None,
        "snapshot_sha256": found["sha256"] if found else None,
        "errors": errors,
        "prerace_verified": not errors and post is not None,
    })
    return result, record, found


def verify(path: Path, config: dict[str, Any] | None = None, repo: Path | None = None) -> dict[str, Any]:
    """構造と規約に加えて、Git の記録で発走前性を検証する。prerace_verified の記録だけを採点に使う。"""
    return _verify(path, config or load_config(), repo or common.ROOT)[0]


def validate_file(path: Path, config: dict[str, Any] | None = None,
                  repo: Path | None = None) -> tuple[list[str], bool]:
    """構造と規約だけの検証（Git の時刻は見ない。CI 用）。(エラー, 見た版の snapshot が見つかったか)。

    snapshot は作業ツリーの版 → Git 履歴の順に探す。CI の浅い clone では過去の版が無いことがあり、
    その場合は出走馬の網羅の照合だけを省く（発走前性の最終判定は、全履歴を取る verify と採点で行う）。
    """
    config = config or load_config()
    repo = repo or common.ROOT
    record = common.read_json(path)
    if not isinstance(record, dict):
        return ["JSON のオブジェクトとして読めません"], False
    ids = _ids(record, config)
    snapshot = rule_text = None
    if ids:
        exp_id, version, race_id = ids
        frozen_at = _dict(record.get("snapshot_ref")).get("frozen_at")
        current = common.read_json(repo / canonical_snapshot_path(race_id))
        target = common.parse_dt(frozen_at)
        if target and isinstance(current, dict) and common.parse_dt(current.get("frozen_at")) == target:
            snapshot = current
        else:
            found = snapshot_version(race_id, frozen_at, repo)
            snapshot = found["snapshot"] if found else None
        rule_file = repo / rule_path(exp_id, version)
        rule_text = rule_file.read_text(encoding="utf-8") if rule_file.is_file() else None
    errors = validate(record, config, snapshot, rule_text)
    if ids:
        layout = _layout_error(_rel(path, repo), ids)
        if layout:
            errors.append(layout)
        if rule_text is None:
            errors.append(f"ルールファイル {rule_path(ids[0], ids[1])} がありません（最初のレースより前にコミットします）")
    return errors, snapshot is not None


def deleted_records(experiment_id: str, repo: Path | None = None) -> list[dict[str, Any]]:
    """Git で削除された記録。発走後に消したもの（結果を見て消した疑い）を見えるようにする。"""
    repo = repo or common.ROOT
    out = _git(["log", "--diff-filter=D", "--format=@%cI", "--name-only", "--",
                f"data/chat_experiments/{experiment_id}/races"], repo)
    deleted: dict[str, list[datetime.datetime]] = {}
    when = None
    for line in out.stdout.decode().splitlines() if out.returncode == 0 else []:
        if line.startswith("@"):
            when = common.parse_dt(line[1:])
        elif line.strip().endswith(".json"):
            deleted.setdefault(line.strip(), []).extend([when] if when else [])
    rows = []
    for rel, times in sorted(deleted.items()):
        if (repo / rel).exists():
            continue                                          # 消したあと戻したもの
        deleted_at = max(times) if times else None            # 最後に消した時刻
        race_id = PurePosixPath(rel).parent.name
        snapshot = common.read_json(repo / canonical_snapshot_path(race_id)) if RACE_ID.fullmatch(race_id) else None
        post = common.post_at(race_id, snapshot.get("post_time")) if isinstance(snapshot, dict) else None
        rows.append({"path": rel, "deleted_at": _iso(deleted_at),
                     "after_post": (deleted_at >= post) if deleted_at and post else None})
    return rows


# ------------------------------------------------------------------ 採点

def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _side(action: str, bets: list[dict[str, Any]], dividends: dict[str, Any],
          top3: set[int]) -> dict[str, Any]:
    settled = [build_results.settle_bet({"type": b["type"], "horses": [int(n) for n in b["horses"]],
                                         "amt": int(b["amt"])}, dividends)
               for b in bets] if action == "bet" else []
    return {
        "action": action,
        "spent": sum(b["amt"] for b in settled),
        "payout": sum(b["payout"] for b in settled),
        "hit": any(b["hit"] for b in settled),
        "hit_bets": sum(1 for b in settled if b["hit"]),
        "top3_capture": len(top3 & {n for b in settled for n in b["horses"]}),
        "bets": settled,
    }


def _card_side(card: dict[str, Any], dividends: dict[str, Any], top3: set[int]) -> dict[str, Any]:
    bets = [b for b in card.get("bets") or []
            if b.get("type") in BET_SIZE and b.get("horses") and b.get("amt")]
    side = _side("pass" if card.get("action") == "pass" or not bets else "bet", bets, dividends, top3)
    side.pop("bets")
    return side


def _race_row(result: dict[str, Any], record: dict[str, Any], found: dict[str, Any],
              race_result: dict[str, Any] | None, config: dict[str, Any]) -> dict[str, Any]:
    row = {
        "race_id": result["race_id"], "record": result["path"],
        "record_first_commit_at": result["record_first_commit_at"],
        "snapshot_frozen_at": result["snapshot_frozen_at"], "post_at": result["post_at"],
        "baseline_char": record["baseline_char"], "budget": record["budget"],
    }
    finish = [int(n) for n in _dict(race_result).get("finish") or []]
    if not finish:
        return {**row, "status": "pending"}
    dividends = race_result.get("dividends") or {}
    top3 = finish[:3]
    experiment = _side(record["action"], record["bets"], dividends, set(top3))
    for settled, bet in zip(experiment["bets"], record["bets"]):
        settled["frame_tag"] = bet["frame_tag"]
    frames = {c["num"]: list(c["frames"]) for c in record["candidates"]}
    position = {n: i for i, n in enumerate(finish, start=1)}
    cards = {c.get("char"): c for c in found["snapshot"].get("cards") or [] if isinstance(c, dict)}
    return {
        **row, "status": "settled", "finish_top3": top3,
        "experiment": experiment,
        # 買った馬の着順（軸が大敗したのか、相手が抜けたのかを後から見るため）
        "bet_horses_finish": {str(n): position.get(n)
                              for n in sorted({n for b in record["bets"] for n in b["horses"]})},
        # 3着内の馬が、どの枠に入っていたか（買い目とは別に、枠の当たり方を見るため）
        "top3_frames": [{"finish": i, "num": n, "frames": frames.get(n, [])}
                        for i, n in enumerate(top3, start=1)],
        "runners": len(frames),
        "framed_runners": sum(1 for f in frames.values() if f),
        "cards": {char: _card_side(cards[char], dividends, set(top3))
                  for char in config["baseline_chars"] if char in cards},
    }


def _totals(sides: list[dict[str, Any]]) -> dict[str, Any]:
    spent = sum(s["spent"] for s in sides)
    payout = sum(s["payout"] for s in sides)
    top = max(sides, key=lambda s: s["payout"], default=None)
    rest = spent - top["spent"] if top else 0
    return {
        "races": len(sides),
        "bet_races": sum(1 for s in sides if s["action"] == "bet"),
        "pass_races": sum(1 for s in sides if s["action"] == "pass"),
        "spent": spent,
        "payout": payout,
        "roi": round(payout / spent, 6) if spent else None,
        # 払戻が最大のレースを除いた回収率（1本の大当たりに引っ張られていないか）
        "roi_ex_max_payout_race": round((payout - top["payout"]) / rest, 6) if top and rest else None,
        "hit_races": sum(1 for s in sides if s["hit"]),
        "mean_top3_capture": _mean([s["top3_capture"] for s in sides if s["action"] == "bet"]),
    }


def _version_block(rows: list[dict[str, Any]], rule: dict[str, Any],
                   config: dict[str, Any]) -> dict[str, Any]:
    settled = [r for r in rows if r["status"] == "settled"]
    declared = sorted({r["baseline_char"] for r in rows})
    primary = [(r["experiment"], r["cards"][r["baseline_char"]]) for r in settled
               if r["baseline_char"] in r["cards"]]
    diffs = [e["payout"] - b["payout"] for e, b in primary]
    tags: dict[str, dict[str, int]] = {}
    for r in settled:
        for bet in r["experiment"]["bets"]:
            tag = tags.setdefault(bet["frame_tag"], {"bets": 0, "hit_bets": 0, "spent": 0, "payout": 0})
            tag["bets"] += 1
            tag["hit_bets"] += int(bet["hit"])
            tag["spent"] += bet["amt"]
            tag["payout"] += bet["payout"]
    finishers = [f for r in settled for f in r["top3_frames"]]
    with_frame = sum(1 for f in finishers if f["frames"])
    runners = sum(r["runners"] for r in settled)
    framed = sum(r["framed_runners"] for r in settled)
    warnings = []
    if len(declared) > 1:
        warnings.append("記録によって baseline_char が違います（version の中では1つに固定します）")
    if rule["registered_at"] is not None and not rule["unmodified"]:
        warnings.append("ルールファイルが追加の後に書き換え・削除されています（この version の記録は検証に通りません）")
    return {
        "rule": {"path": rule["path"], "registered_at": _iso(rule["registered_at"]),
                 "unmodified": rule["unmodified"]},
        "races_settled": len(settled),
        "races_pending": len(rows) - len(settled),
        "baseline_chars_declared": declared,
        "experiment": _totals([r["experiment"] for r in settled]),
        "primary_baseline": _totals([b for _, b in primary]),
        "paired_payout_diff_vs_primary_baseline": {
            "races": len(diffs), "mean": _mean(diffs),
            "experiment_higher": sum(1 for d in diffs if d > 0),
            "baseline_higher": sum(1 for d in diffs if d < 0),
            "equal": sum(1 for d in diffs if d == 0),
        },
        "cards": {char: _totals([r["cards"][char] for r in settled if char in r["cards"]])
                  for char in config["baseline_chars"] if any(char in r["cards"] for r in settled)},
        # rate を framed_runner_share（全出走馬のうち枠に入れた割合＝でたらめに選んだときの期待値）と比べる。
        # 枠に入れる馬を増やせば rate は上がるので、rate だけでは枠の当たり方を判断しない
        "top3_finishers": {"total": len(finishers), "with_any_frame": with_frame,
                           "rate": round(with_frame / len(finishers), 6) if finishers else None,
                           "framed_runner_share": round(framed / runners, 6) if runners else None},
        "by_frame_tag": {k: tags[k] for k in sorted(tags)},
        "warnings": warnings,
        "races": sorted(rows, key=lambda r: r["race_id"]),
    }


def settle_experiment(experiment_id: str, race_results: dict[str, Any],
                      config: dict[str, Any], repo: Path) -> dict[str, Any]:
    """1つの実験の全記録を検証して採点する。発走前の検証に通った記録だけを使う。"""
    base = repo / "data" / "chat_experiments" / experiment_id
    verified: dict[tuple[str, str], list[tuple[dict, dict, dict]]] = {}
    rejected = []
    for path in sorted(base.glob("races/*/*.json")):
        result, record, found = _verify(path, config, repo)
        if result["prerace_verified"] and result["experiment_id"] == experiment_id:
            verified.setdefault((result["rule_version"], result["race_id"]), []).append((result, record, found))
        else:
            rejected.append({k: result[k] for k in ("path", "race_id", "rule_version", "errors")})

    versions = {v for v, _ in verified}
    versions |= {p.stem.removeprefix("rule_") for p in base.glob("rule_*.md")
                 if re.fullmatch(config["rule_version_pattern"], p.stem.removeprefix("rule_"))}
    blocks = {}
    for version in sorted(versions, key=lambda v: int(v[1:])):
        rows = []
        for (v, race_id), items in sorted(verified.items()):
            if v != version:
                continue
            # 発走前で最後にコミットされた案を採点する。それより前の案は superseded として残す
            items.sort(key=lambda item: (common.parse_dt(item[0]["record_last_commit_at"]), item[0]["path"]))
            result, record, found = items[-1]
            row = _race_row(result, record, found, race_results.get(race_id), config)
            row["superseded"] = [item[0]["path"] for item in items[:-1]]
            rows.append(row)
        blocks[version] = _version_block(rows, rule_registration(experiment_id, version, repo), config)
    return {
        "schema": SUMMARY_SCHEMA,
        "experiment_id": experiment_id,
        "config_version": config["version"],
        "note": SUMMARY_NOTE,
        "official_results_untouched": True,
        "versions": blocks,
        "rejected_records": sorted(rejected, key=lambda r: r["path"]),
        "deleted_records": deleted_records(experiment_id, repo),
    }


def settle(race_results: dict[str, Any], config: dict[str, Any] | None = None,
           repo: Path | None = None, output_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """全実験を採点し、data/shadow/chat_experiments/{experiment_id}/summary.json に書く。"""
    config = config or load_config()
    repo = repo or common.ROOT
    output_dir = output_dir or repo / "data" / "shadow" / "chat_experiments"
    base = repo / "data" / "chat_experiments"
    experiments = sorted(p.name for p in base.iterdir()
                         if p.is_dir() and re.fullmatch(config["experiment_id_pattern"], p.name)
                         ) if base.is_dir() else []
    summaries = {}
    for experiment_id in experiments:
        summary = settle_experiment(experiment_id, race_results, config, repo)
        if summary["versions"] or summary["rejected_records"] or summary["deleted_records"]:
            common.write_json(output_dir / experiment_id / "summary.json", summary)
            summaries[experiment_id] = summary
    return summaries


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="チャット実験の記録の検証と採点（shadow 研究用）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="構造と規約を検証する（Git の時刻は見ない。CI 用）")
    v.add_argument("paths", nargs="*")
    g = sub.add_parser("verify", help="構造と規約に加えて、Git の記録で発走前性を検証する")
    g.add_argument("paths", nargs="*")
    s = sub.add_parser("settle", help="結果で採点し、data/shadow/chat_experiments/ に書く")
    s.add_argument("--results", required=True)
    args = parser.parse_args(argv)
    config = load_config()

    if args.cmd == "settle":
        race_results = common.read_json(Path(args.results))
        if not isinstance(race_results, dict):
            print(f"{args.results} を読めません", file=sys.stderr)
            return 1
        summaries = settle(race_results, config)
        for experiment_id, summary in summaries.items():
            for version, block in summary["versions"].items():
                print(f"{experiment_id} {version}: 採点 {block['races_settled']} レース・"
                      f"結果待ち {block['races_pending']} レース")
            print(f"{experiment_id}: 検証に通らなかった記録 {len(summary['rejected_records'])} 件・"
                  f"削除された記録 {len(summary['deleted_records'])} 件")
        if not summaries:
            print("チャット実験の記録はまだありません")
        return 0

    paths = [Path(p) if Path(p).is_absolute() else common.ROOT / p for p in args.paths] or record_paths()
    failed = 0
    for path in paths:
        note = None
        if args.cmd == "validate":
            errors, snapshot_found = validate_file(path, config)
            ok = not errors
            if not snapshot_found:
                note = "見た版の snapshot が見つからないため、出走馬の網羅の照合を省きました"
        else:
            result = verify(path, config)
            errors, ok = result["errors"], result["prerace_verified"]
            note = (f"post={result['post_at']} created={result['created_at']} "
                    f"first_commit={result['record_first_commit_at']} "
                    f"rule_registered={result['rule_registered_at']} "
                    f"snapshot_frozen_at={result['snapshot_frozen_at']}")
        print(f"{'OK' if ok else 'NG'} {_rel(path, common.ROOT)}")
        for err in errors:
            print(f"    - {err}")
        if note:
            print(f"    ({note})")
        failed += not ok
    print(f"chat experiment records: {len(paths)} checked, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
