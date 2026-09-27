"""チャットChappy（手動1000円案）の発走前 decision log（chat-chappy-decision-v1）。

保存先: data/chappy_decisions/{race_id}/{YYYYmmddTHHMMSS}.json（1案1ファイル・追記のみ）

目的は「LLMが何となく選んだ」を、後から検証できる記録に変えること。
- どの snapshot を見たか（パスと SHA-256）
- 全出走馬について base順位 / Top3順位 / 市場人気順位 / 固定3人の採用状況 / 順位ズレ該当
- 各馬の採否と、落とした理由（固定語彙）
- 買い目と、各点の仮説タグ

**必ず買う、という制約は無い。** ただし次の馬を落とすときは reason_code が必須:
    - 順位ズレ馬（rank-gap-v1: base順位≤5 かつ 人気順位≥6）
    - 固定3人（ケイ・哲・源）のどれかが買っている馬

発走前性の検証（verify）:
    1. created_at < 発走時刻
    2. このファイルがGitに**最初に追加されたコミットのコミット時刻** < 発走時刻
    3. snapshot_ref.sha256 が、Git履歴上の data/snapshots/{race_id}.json のどれかの版と一致
   GitHub の API / Web から作られたコミットはコミット時刻を GitHub 側が付けるため、
   ChatGPT がコネクタ経由で保存する運用ならこれが機械的な証拠になる。手元の git push では
   コミット時刻を偽装できるので、その場合は GitHub の push 記録（Actions の CI 実行時刻）と併用する。

この記録は shadow 研究用。正式な Chappy カード（data/chappy_manual/ による上書き）とは別物で、
Champion の出力・正式成績には影響しない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from logic import snapshots
from research import common, rank_gap

FIXED_CHARACTERS = ("kei", "tetsu", "gen")
BET_SIZE = {"ワイド": 2, "馬連": 2, "3連複": 3}


# ------------------------------------------------------------------ 派生情報

def snapshot_facts(snapshot: dict[str, Any], config: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """snapshot から、候補馬ごとの「事実」列（ログ側が書き換えてはいけない値）を作る。"""
    marks = snapshot.get("marks") or []
    b_ranks = rank_gap.base_ranks(marks)
    m_ranks = rank_gap.market_ranks(marks)
    gap_nums = {h["num"] for h in rank_gap.register(marks, config)["horses"]}
    fixed: dict[int, list[str]] = {}
    for card in snapshot.get("cards") or []:
        if card.get("char") in FIXED_CHARACTERS and card.get("action") != "pass":
            for num in {n for bet in card.get("bets") or [] for n in bet["horses"]}:
                fixed.setdefault(int(num), []).append(card["char"])
    facts = {}
    for mark in marks:
        num = int(mark["num"])
        facts[num] = {
            "num": num,
            "name": mark.get("name"),
            "base_rank": b_ranks.get(num),
            "top3_rank": mark.get("top3_rank"),
            "market_rank": m_ranks.get(num),
            "win_odds": mark.get("odds"),
            "in_fixed_cards": sorted(fixed.get(num, []), key=FIXED_CHARACTERS.index),
            "rank_gap": num in gap_nums,
        }
    return facts


def template(snapshot: dict[str, Any], snapshot_path: Path, config: dict[str, Any],
             created_at: str | None = None) -> dict[str, Any]:
    """ChatGPT が埋めるひな形。decision / reason_code / bets だけを書けばよい状態にする。"""
    facts = snapshot_facts(snapshot, config)
    fixed_cards = {
        c["char"]: sorted({int(n) for b in c.get("bets") or [] for n in b["horses"]})
        for c in snapshot.get("cards") or []
        if c.get("char") in FIXED_CHARACTERS and c.get("action") != "pass"
    }
    return {
        "schema_version": config["decision_log"]["schema_version"],
        "race_id": snapshot.get("race_id"),
        "created_at": created_at,
        "author": "ChatGPT",
        "snapshot_ref": {
            # 参照先は常に正規パス。検証側も race_id から決まるこのパスだけを見る。
            "path": canonical_snapshot_path(snapshot.get("race_id") or ""),
            "sha256": common.sha256_file(snapshot_path),
            "frozen_at": snapshot.get("frozen_at"),
            "model_id": snapshot.get("model_id"),
            "config_hash": snapshot.get("config_hash"),
        },
        "rank_gap_definition": {
            "version": config["rank_gap"]["version"],
            "max_base_rank": config["rank_gap"]["max_base_rank"],
            "min_market_rank": config["rank_gap"]["min_market_rank"],
        },
        "fixed_cards": fixed_cards,
        "candidates": [
            {**f, "decision": None, "reason_code": None, "note": ""}
            for f in sorted(facts.values(), key=lambda f: (f["base_rank"] or 999, f["num"]))
        ],
        "bets": [],
        "total": config["decision_log"]["total"],
        "summary": "",
    }


# ------------------------------------------------------------------ 検証

def validate(log: dict[str, Any], config: dict[str, Any],
             snapshot: dict[str, Any] | None = None,
             snapshot_sha256: str | None = None) -> list[str]:
    """構造と規約の検証。エラー文言のリストを返す（空なら合格）。

    snapshot を渡し、かつその SHA-256 が snapshot_ref と一致する場合は、
    候補馬の順位・固定3人・順位ズレの列が snapshot と一致するかも確かめる。
    """
    cfg = config["decision_log"]
    errors: list[str] = []
    if log.get("schema_version") != cfg["schema_version"]:
        errors.append(f"schema_version は {cfg['schema_version']} である必要があります")
    race_id = log.get("race_id")
    if not race_id:
        errors.append("race_id がありません")
    created = common.parse_dt(log.get("created_at"))
    if created is None:
        errors.append("created_at がISO8601ではありません")
    ref = log.get("snapshot_ref") or {}
    for key in ("path", "sha256", "frozen_at"):
        if not ref.get(key):
            errors.append(f"snapshot_ref.{key} がありません")

    post = None
    if snapshot is not None:
        post = common.post_at(race_id or "", snapshot.get("post_time"))
    if created and post and created >= post:
        errors.append("created_at が発走時刻以降です（発走前の記録ではありません）")

    candidates = log.get("candidates") or []
    by_num: dict[int, dict[str, Any]] = {}
    for cand in candidates:
        num = cand.get("num")
        if num is None:
            errors.append("num の無い候補馬があります")
            continue
        if int(num) in by_num:
            errors.append(f"候補馬 {num} が重複しています")
        by_num[int(num)] = cand
        decision = cand.get("decision")
        if decision not in ("keep", "drop"):
            errors.append(f"{num}番: decision は keep / drop のどちらかです")
            continue
        protected = bool(cand.get("rank_gap")) or bool(cand.get("in_fixed_cards"))
        code = cand.get("reason_code")
        if decision == "drop" and protected and not code:
            why = "順位ズレ馬" if cand.get("rank_gap") else "固定3人が買っている馬"
            errors.append(f"{num}番: {why}を落とす場合は reason_code が必須です")
        if code is not None and code not in cfg["drop_reason_codes"]:
            errors.append(f"{num}番: reason_code '{code}' は語彙にありません")
        if code == "other" and not (cand.get("note") or "").strip():
            errors.append(f"{num}番: reason_code=other のときは note が必須です")

    if snapshot is not None and snapshot_sha256 and snapshot_sha256 == ref.get("sha256"):
        facts = snapshot_facts(snapshot, config)
        missing = sorted(set(facts) - set(by_num))
        extra = sorted(set(by_num) - set(facts))
        if missing:
            errors.append(f"snapshot の出走馬が候補に含まれていません: {missing}")
        if extra:
            errors.append(f"snapshot に無い馬番が候補にあります: {extra}")
        for num in sorted(set(facts) & set(by_num)):
            for key in ("base_rank", "top3_rank", "market_rank", "in_fixed_cards", "rank_gap"):
                if by_num[num].get(key) != facts[num][key]:
                    errors.append(f"{num}番: {key} が snapshot と一致しません"
                                  f"（log={by_num[num].get(key)!r} snapshot={facts[num][key]!r}）")

    bets = log.get("bets") or []
    if not bets:
        errors.append("bets が空です")
    bet_horses: set[int] = set()
    total = 0
    for i, bet in enumerate(bets, start=1):
        bet_type = bet.get("type")
        horses = bet.get("horses") or []
        amt = bet.get("amt")
        if bet_type not in cfg["bet_types"]:
            errors.append(f"買い目{i}: 券種 {bet_type!r} は使えません")
        elif len(horses) != BET_SIZE[bet_type] or len(set(horses)) != len(horses):
            errors.append(f"買い目{i}: 券種と頭数が一致しないか、馬番が重複しています")
        if not isinstance(amt, int) or amt <= 0 or amt % int(cfg["amt_unit"]) != 0:
            errors.append(f"買い目{i}: 金額は {cfg['amt_unit']} 円単位の正の整数です")
        else:
            total += amt
        if bet.get("hypothesis_tag") not in cfg["hypothesis_tags"]:
            errors.append(f"買い目{i}: hypothesis_tag は {cfg['hypothesis_tags']} から選びます")
        for num in horses:
            bet_horses.add(int(num))
            cand = by_num.get(int(num))
            if cand is None:
                errors.append(f"買い目{i}: {num}番が候補に含まれていません")
            elif cand.get("decision") != "keep":
                errors.append(f"買い目{i}: {num}番は drop なのに買い目に入っています")
    kept = {n for n, c in by_num.items() if c.get("decision") == "keep"}
    unused = sorted(kept - bet_horses)
    if unused:
        errors.append(f"keep なのに買い目に一度も入っていない馬があります（落とすなら drop と理由を）: {unused}")
    if total != int(cfg["total"]) or log.get("total") != int(cfg["total"]):
        errors.append(f"合計金額は {cfg['total']} 円である必要があります（買い目合計 {total}）")
    return errors


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=30)


def first_commit_time(path: Path, repo: Path | None = None) -> datetime.datetime | None:
    """そのファイルが最初に追加されたコミットのコミット時刻（Gitの記録）。"""
    repo = repo or common.ROOT
    rel = path.relative_to(repo) if path.is_absolute() and path.is_relative_to(repo) else path
    out = _git(["log", "--diff-filter=A", "--format=%cI", "--", str(rel)], repo)
    lines = [l for l in out.stdout.decode().splitlines() if l.strip()]
    return common.parse_dt(lines[-1]) if out.returncode == 0 and lines else None


def snapshot_blob_from_git(snapshot_rel_path: str, sha256: str,
                           repo: Path | None = None) -> bytes | None:
    """Git履歴上の snapshot のうち、SHA-256 が一致する版の中身（バイト列）を返す。無ければ None。

    snapshot は発走前の再実行で上書きされるので、log が参照した版は過去のコミットにしか無いことがある。
    """
    repo = repo or common.ROOT
    out = _git(["log", "--format=%H", "--", snapshot_rel_path], repo)
    if out.returncode != 0:
        return None
    for commit in out.stdout.decode().split():
        blob = _git(["show", f"{commit}:{snapshot_rel_path}"], repo)
        if blob.returncode == 0 and common.sha256_bytes(blob.stdout) == sha256:
            return blob.stdout
    return None


def snapshot_version_in_git(snapshot_rel_path: str, sha256: str, repo: Path | None = None) -> bool:
    """snapshot_ref.sha256 が、Git履歴にある snapshot のどれかの版と一致するか。"""
    return snapshot_blob_from_git(snapshot_rel_path, sha256, repo) is not None


def resolve_referenced_snapshot(log: dict[str, Any], repo: Path | None = None,
                                snapshot_dir: Path | None = None
                                ) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """log.snapshot_ref が指す**その版**の snapshot を返す。(snapshot, sha256, source)。

    source は "current"（作業ツリーの現行版と一致）/ "git_history"（過去のコミットの版）/ None（見つからない）。
    参照先のパスは log 側の記述を信用せず、race_id から決まる正規のパスだけを見る。
    """
    repo = repo or common.ROOT
    race_id = log.get("race_id") or ""
    ref_sha = (log.get("snapshot_ref") or {}).get("sha256")
    current_path = snapshots.snapshot_path(race_id, snapshot_dir or (repo / "data" / "snapshots"))
    if not race_id or not ref_sha:
        return None, None, None
    if current_path.exists() and common.sha256_file(current_path) == ref_sha:
        snapshot = common.read_json(current_path)
        return (snapshot, ref_sha, "current") if isinstance(snapshot, dict) else (None, None, None)
    blob = snapshot_blob_from_git(canonical_snapshot_path(race_id), ref_sha, repo)
    if blob is None:
        return None, None, None
    try:
        snapshot = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None, None
    return (snapshot, ref_sha, "git_history") if isinstance(snapshot, dict) else (None, None, None)


def canonical_snapshot_path(race_id: str) -> str:
    return f"data/snapshots/{race_id}.json"


def verify(log_path: Path, config: dict[str, Any], repo: Path | None = None,
           snapshot_dir: Path | None = None) -> dict[str, Any]:
    """構造検証＋Gitでの発走前性の検証。採点に使ってよいかどうかを返す。

    候補馬の順位・固定3人・順位ズレの列は、log が参照した**その版の snapshot**と照合する
    （現行版に上書きされていても、Git履歴から参照版を復元して照合する）。
    """
    repo = repo or common.ROOT
    log = common.read_json(log_path)
    if not isinstance(log, dict):
        return {"path": str(log_path), "valid": False, "errors": ["JSONとして読めません"],
                "prerace_verified": False}
    race_id = log.get("race_id") or ""
    snapshot, ref_sha, source = resolve_referenced_snapshot(log, repo, snapshot_dir)
    errors = validate(log, config, snapshot, ref_sha)
    if snapshot is None:
        errors.append("snapshot_ref.sha256 に一致する snapshot の版が現行にも Git履歴にも無く、"
                      "候補馬の事実を照合できません")
    ref_path = (log.get("snapshot_ref") or {}).get("path")
    if ref_path and ref_path != canonical_snapshot_path(race_id):
        errors.append(f"snapshot_ref.path は {canonical_snapshot_path(race_id)} である必要があります")

    post = common.post_at(race_id, snapshot.get("post_time")) if snapshot else None
    committed = first_commit_time(log_path, repo)
    created = common.parse_dt(log.get("created_at"))
    prerace = bool(post and committed and committed < post and created and created < post)
    return {
        "path": str(log_path.relative_to(repo)) if log_path.is_relative_to(repo) else str(log_path),
        "race_id": race_id,
        "valid": not errors,
        "errors": errors,
        "post_at": post.isoformat() if post else None,
        "created_at": log.get("created_at"),
        "git_first_commit_at": committed.isoformat() if committed else None,
        "snapshot_version_found": snapshot is not None,
        "facts_checked_against": source,
        "prerace_verified": prerace and snapshot is not None and not errors,
    }


def log_paths(race_id: str | None = None, directory: Path | None = None) -> list[Path]:
    base = directory or common.DECISION_LOG_DIR
    if race_id:
        return sorted((base / race_id).glob("*.json"))
    return sorted(base.glob("*/*.json"))


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="チャットChappy decision log のひな形作成・検証")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("template", help="snapshot からひな形を作る")
    t.add_argument("--race", required=True)
    t.add_argument("--out", help="書き出し先（省略時は標準出力）")
    v = sub.add_parser("validate", help="構造と規約を検証（Gitは見ない。CI用）")
    v.add_argument("paths", nargs="*")
    g = sub.add_parser("verify", help="構造＋Gitでの発走前性を検証")
    g.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    config = common.load_config()

    if args.cmd == "template":
        path = snapshots.snapshot_path(args.race)
        snapshot = common.read_json(path)
        if not isinstance(snapshot, dict):
            print(f"{path} がありません", file=sys.stderr)
            return 1
        created = datetime.datetime.now(common.JST).isoformat(timespec="seconds")
        payload = template(snapshot, path, config, created_at=created)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    paths = [Path(p) for p in args.paths] or log_paths()
    failed = 0
    for path in paths:
        path = path if path.is_absolute() else common.ROOT / path
        if args.cmd == "validate":
            log = common.read_json(path)
            if isinstance(log, dict):
                # 参照版の snapshot を現行 → Git履歴の順に探して事実を照合する。
                # CI の浅い clone では過去版が無いことがあるので、その場合は警告だけ出す
                # （発走前性と事実照合の最終判定は、全履歴を取る verify / settle で行う）。
                snapshot, ref_sha, source = resolve_referenced_snapshot(log)
                errors = validate(log, config, snapshot, ref_sha)
                if snapshot is None:
                    print(f"  注意: {path.name} の参照 snapshot 版が見つからないため事実照合を省略しました")
            else:
                errors = ["JSONとして読めません"]
            status = "OK" if not errors else "NG"
        else:
            result = verify(path, config)
            errors = result["errors"]
            status = "OK" if result["prerace_verified"] else "NG"
            print(f"  post={result['post_at']} created={result['created_at']} "
                  f"git_first_commit={result['git_first_commit_at']} "
                  f"snapshot_version_found={result['snapshot_version_found']}")
        print(f"{status} {path.relative_to(common.ROOT) if path.is_relative_to(common.ROOT) else path}")
        for err in errors:
            print(f"    - {err}")
        failed += status != "OK"
    print(f"decision logs: {len(paths)} checked, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
