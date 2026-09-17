"""
スピード指数 算出ロジック（スピード指数仕様_v1.md 全章）

入力：共通内部フォーマットの past_runs（直近5走）
出力：馬1頭あたりの {"best": float, "avg": float, "latest": float, "n_usable": int} または None

設定：config/speed_index.json（§5）
基準タイム表：config/base_times.json（§4。scripts/build_base_times.py が生成）

TODO（実装）：
- §1 の1走あたり指数計算式（scale, going_sec, impost_sec, class_sec, expected_time, adjusted_time）
- §2 の usable run 判定（5条件）
- §3 の馬1頭への集約（best / 鮮度加重avg / latest / n_usable）
- n_usable=0 は speed=None を返し、呼び出し側（base_score.py）で不確実フラグを立てる
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "speed_index.json"
BASE_TIMES_PATH = Path(__file__).resolve().parent.parent / "config" / "base_times.json"


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_base_times() -> dict[str, Any]:
    with BASE_TIMES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def compute_run_index(run: dict[str, Any], config: dict[str, Any], base_times: dict[str, Any]) -> float | None:
    """1走分の指数を計算する（スピード指数仕様§1）。usable判定外なら None。"""
    raise NotImplementedError("スピード指数仕様§1・§2 を実装")


def compute_horse_speed(past_runs: list[dict[str, Any]], config: dict[str, Any] | None = None,
                         base_times: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """
    馬1頭の past_runs（直近5走）から speed 集約値を計算する（スピード指数仕様§3）。

    戻り値: {"best": ..., "avg": ..., "latest": ..., "n_usable": ...} または n_usable=0 なら None
    """
    raise NotImplementedError("スピード指数仕様§3 を実装")
