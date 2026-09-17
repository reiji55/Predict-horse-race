"""
スコア→モデル勝率p（softmax） / オッズ→市場支持率q

**妙味メーター仕様§1・§1.1 と 買い目生成仕様§3 で共有する部品**。二重定義しない。

設定：config/myomi.json の "prob_model"（temperature）

TODO（実装）：
- softmax(base_score / T)。base_score=None の馬は除外
- q = (1/odds) を正規化（Σr≈1.25の控除率を割り戻す。妙味メーター仕様§1.1）
- win_odds=None の馬はΣから除外
"""
from __future__ import annotations

from typing import Any


def softmax_scores(scores: list[float | None], temperature: float) -> list[float | None]:
    """base_scoreのリストからモデル勝率pのリストを返す。Noneはpも None。"""
    raise NotImplementedError("妙味メーター仕様§1 を実装")


def market_support(win_odds: list[float | None]) -> list[float | None]:
    """単勝オッズのリストから市場支持率qのリストを返す（妙味メーター仕様§1.1）。"""
    raise NotImplementedError("妙味メーター仕様§1.1 を実装")
