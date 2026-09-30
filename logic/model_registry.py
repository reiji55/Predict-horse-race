from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
MODELS_PATH = CONFIG_DIR / "models.json"
# 人が決めるモデルの設定（＝ここが変わったら「モデルを変えた」）
HASH_CONFIGS = ("cards.json", "chappy.json", "myomi.json", "speed_index.json", "models.json", "race_regime.json")
# 自動で育つデータ表（＝ここが変わっても「モデルを変えた」わけではない）
BASE_TIMES_FILE = "base_times.json"
# Challenger 専用の凍結した基準タイム表は、ここより下にあるものだけ読む（Champion の config/ は読まない）
REFERENCE_BASE_TIMES_DIR = ROOT / "data" / "reference" / "base_times"
BASE_TIMES_OVERRIDE_KEYS = ("artifact_id", "lookup_file", "meta_file", "lookup_sha256", "cutoff_date")


def load_registry(path: Path | None = None) -> dict[str, Any]:
    path = path or MODELS_PATH
    with path.open(encoding="utf-8") as f:
        registry = json.load(f)

    champion = registry.get("champion")
    if not isinstance(champion, dict) or not champion.get("id"):
        raise ValueError("config/models.json に champion.id が必要です")
    if "base_times" in champion:
        # Champion は常に config/base_times.json を使う。専用の表は shadow の Challenger だけ
        raise ValueError("champion に base_times の差し替えは指定できません")

    ids = [champion["id"]]
    for model in registry.get("challengers", []):
        if not isinstance(model, dict) or not model.get("id"):
            raise ValueError("challengers[] の各要素に id が必要です")
        if "base_times" in model:
            _validate_base_times_override(model)
        ids.append(model["id"])
    if len(ids) != len(set(ids)):
        raise ValueError(f"model id が重複しています: {ids}")

    return registry


def _validate_base_times_override(model: dict[str, Any]) -> None:
    """専用の基準タイム表を使う Challenger の登録内容を確かめる（ファイルはまだ読まない）。"""
    override = model["base_times"]
    missing = [k for k in BASE_TIMES_OVERRIDE_KEYS if not (isinstance(override, dict) and override.get(k))]
    if missing:
        raise ValueError(f"{model['id']}: base_times に {missing} が必要です")
    registered_at = model.get("registered_at")
    if not registered_at:
        raise ValueError(f"{model['id']}: 凍結した表を使う Challenger には registered_at が必要です")
    registered = datetime.datetime.fromisoformat(registered_at)
    if registered.tzinfo is None:
        raise ValueError(f"{model['id']}: registered_at はタイムゾーン付きで書いてください")
    # 表に使った記録の最終日より後に登録していないと、登録前のレースを forward として数えてしまう
    cutoff = datetime.date.fromisoformat(override["cutoff_date"])
    if registered.date() <= cutoff:
        raise ValueError(f"{model['id']}: registered_at は cutoff_date より後にしてください")


def _reference_path(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    if REFERENCE_BASE_TIMES_DIR.resolve() not in path.parents:
        raise ValueError(f"基準タイム表は {REFERENCE_BASE_TIMES_DIR.relative_to(ROOT)} の下だけ読めます: {relative}")
    return path


def _canonical_sha256(payload: Any) -> str:
    """凍結表の meta に記録した lookup_sha256 と同じ正規化（キー順・空白に依存しない）。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_model_base_times(model: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """
    Challenger 専用の凍結した基準タイム表を読み、(lookup, ref) を返す。専用の表が無いモデルは None。

    登録した lookup_sha256・artifact_id・cutoff_date と中身が1つでも違えば ValueError（fail-closed）。
    forward 検証の途中で表が差し替わると、同じ model id の中身が変わってしまうため。
    """
    override = model.get("base_times")
    if override is None:
        return None
    lookup_path = _reference_path(override["lookup_file"])
    meta_path = _reference_path(override["meta_file"])
    data = lookup_path.read_bytes()
    lookup = json.loads(data)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    actual = _canonical_sha256(lookup)
    if actual != override["lookup_sha256"]:
        raise ValueError(f"{model['id']}: 基準タイム表の lookup_sha256 が登録と違います（{actual}）")
    if (meta.get("hashes") or {}).get("lookup_sha256") != actual:
        raise ValueError(f"{model['id']}: meta の lookup_sha256 が lookup と一致しません")
    if meta.get("artifact_id") != override["artifact_id"]:
        raise ValueError(f"{model['id']}: meta の artifact_id が登録と違います（{meta.get('artifact_id')}）")
    if meta.get("cutoff_date") != override["cutoff_date"]:
        raise ValueError(f"{model['id']}: meta の cutoff_date が登録と違います（{meta.get('cutoff_date')}）")

    ref = {
        "artifact_id": override["artifact_id"],
        "lookup_file": override["lookup_file"],
        "lookup_sha256": actual,
        "cutoff_date": override["cutoff_date"],
        "method_version": (meta.get("estimator") or {}).get("version"),
        "file_hash": hashlib.sha256(data).hexdigest()[:16],
    }
    return lookup, ref


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
    meta = {
        "model_id": model["id"],
        "model_role": model.get("role"),
        "git_commit": git_commit(),
        "config_hash": config_hash(),
        "base_times_hash": base_times_hash(),
    }
    loaded = load_model_base_times(model)
    if loaded is not None:
        # 専用の表を使うモデルは、その表の指紋を残す（Champion の出力にはこのキーは出ない）
        _lookup, ref = loaded
        meta["base_times_hash"] = ref["file_hash"]
        meta["base_times_ref"] = ref
    return meta
