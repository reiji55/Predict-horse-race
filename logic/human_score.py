"""
③ 人的スコア（買い目生成仕様_v1.md §1.5）

騎手・厩舎の着度数（共通内部フォーマット§2.6）から、縮小推定（全体平均へ引き戻す）を経て計算する。

設定：config/cards.json の "human" セクション（w_jockey, w_trainer, shrink_m）

TODO（実装）：
- rate_shrunk = (hits + α×m) / (starts + m) の縮小推定
- 騎手：当該場の複勝率、厩舎：全体成績
- 0.6×騎手rate_shrunk + 0.4×厩舎rate_shrunk
- starts が極端に小さい騎手は自動的に中立（全体平均）へ寄る
"""
from __future__ import annotations

from typing import Any


def shrink_rate(hits: int, starts: int, overall_rate: float, m: float) -> float:
    """縮小推定（買い目生成仕様§1.5）。overall_rate = 全体平均複勝率など。"""
    raise NotImplementedError("買い目生成仕様§1.5 を実装")


def compute_human_score(jockey_stats: dict[str, Any] | None, trainer_stats: dict[str, Any] | None,
                         overall_jockey_rate: float, overall_trainer_rate: float,
                         config: dict[str, Any]) -> float | None:
    """
    jockey_stats / trainer_stats（取得項目仕様§2.6の着度数）から人的スコアの生値を計算する。
    両方nullなら None。
    """
    raise NotImplementedError("買い目生成仕様§1.5 を実装")
