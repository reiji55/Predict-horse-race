from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
MODELS_PATH = CONFIG_DIR / "models.json"
# 人が決めるモデルの設定（＝ここが変わったら「モデルを変えた」）
HASH_CONFIGS = ("cards.json", "chappy.json", "myomi.json", "speed_index.json", "models.json")
# 自動で育つデータ表（＝ここが変わっても「モデルを変えた」わけではない）
BASE_TIMES_FILE = "base_times.json"


def load_registry(path: Path | None = None) -> dict[str, Any]:
    path = path or MODELS_PATH
    with path.open(encoding="utf-8") as f:
        registry = json.load(f)

    champion = registry.get("champion")
    if not isinstance(champion, dict) or not champion.get("id"):
        raise ValueError("config/models.json に champion.id が必要です")

    ids = [champion["id"]]
    for model in registry.get("challengers", []):
        if not isinstance(model, dict) or not model.get("id"):
            raise ValueError("challengers[] の各要素に id が必要です")
        ids.append(model["id"])
    if len(ids) != len(set(ids)):
        raise ValueError(f"model id が重複しています: {ids}")

    return registry


def enabled_challengers(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        model for model in registry.get("challengers", [])
        if model.get("enabled", True)
    ]


def git_commit() -> str:
    """
    GitHub ActionsではGITHUB_SHAを使う。
    ローカル/テストでは環境変数が無ければ unknown とし、外部コマンドには依存しない。
    """
    return os.environ.get("GITHUB_SHA") or "unknown"


def config_hash(config_dir: Path | None = None) -> str:
    """
    予想へ影響する**人が決める設定**を安定順でSHA-256化する。
    後から「同じmodel idでも設定値が違った」を判別するための指紋。

    基準タイム表（base_times.json）はここに入れず `base_times_hash` に分けている。
    base_times は `run_base_times.yml` / `fill_base_times.py` で**週ごとに自動で埋まっていく**
    データ表なので、混ぜると config_hash が人の判断と無関係に毎週変わり、
    「誰かがモデルの設定をいじったのか」を config_hash で見分けられなくなる。
    """
    config_dir = config_dir or CONFIG_DIR
    digest = hashlib.sha256()
    for name in HASH_CONFIGS:
        path = config_dir / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def base_times_hash(config_dir: Path | None = None) -> str:
    """
    基準タイム表の指紋。スピード指数を通じて予想へ直接効くので、再現には必要。

    ファイルが無い場合も落とさず "absent" を返す（①が全馬 None になる状態として識別できる）。
    """
    path = (config_dir or CONFIG_DIR) / BASE_TIMES_FILE
    if not path.exists():
        return "absent"
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def runtime_metadata(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_id": model["id"],
        "model_role": model.get("role"),
        "git_commit": git_commit(),
        "config_hash": config_hash(),
        "base_times_hash": base_times_hash(),
    }
