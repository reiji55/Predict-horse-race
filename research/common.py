"""research/ 共通の設定読み込み・ファイル入出力・時刻判定。"""
from __future__ import annotations

import datetime
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from logic import snapshots

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "shadow_research.json"
SHADOW_DIR = ROOT / "data" / "shadow"
PRERACE_DIR = SHADOW_DIR / "prerace"
RACES_DIR = SHADOW_DIR / "races"
SUMMARY_PATH = SHADOW_DIR / "summary.json"
DECISION_LOG_DIR = ROOT / "data" / "chappy_decisions"
JST = snapshots.JST


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as f:
        return json.load(f)


def config_hash(config: dict[str, Any]) -> str:
    """設定内容の指紋。キー順や空白に依存しないよう正規化してからハッシュする。"""
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def experiment_meta(config: dict[str, Any], experiment: str) -> dict[str, Any]:
    """各実験の出力に必ず付ける「どのルールで作ったか」。"""
    return {
        "experiment": experiment,
        "version": (config.get(experiment) or {}).get("version"),
        "research_version": config.get("version"),
        "research_config_hash": config_hash(config),
        "registered_at": config.get("registered_at"),
    }


def git_commit(root: Path | None = None) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root or ROOT,
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: Any) -> None:
    """途中で落ちても壊れたJSONを残さないよう、一時ファイル経由で置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def parse_dt(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def post_at(race_id: str, post_time: Any) -> datetime.datetime | None:
    """発走日時。snapshots.post_datetime と同じ規則（二重定義しない）。"""
    return snapshots.post_datetime({"id": race_id, "post_time": post_time})


def phase_of(post: datetime.datetime | None, config: dict[str, Any]) -> str:
    """事前登録日時以降に発走したレースだけを forward（前向き）とする。"""
    registered = parse_dt(config.get("registered_at"))
    if post is None or registered is None:
        return "unknown"
    return "forward" if post >= registered else "retrospective"


def is_model_race(snapshot: dict[str, Any]) -> bool:
    """研究対象は、発走前に凍結された自動モデルのレースだけ（手動チャット記録は除く）。"""
    return (
        snapshot.get("pre_race") is True
        and snapshot.get("evaluation_scope") != "manual_chat"
        and bool(snapshot.get("marks"))
    )
