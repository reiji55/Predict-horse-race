"""
合成スコア base_score ＝ ①②③のz標準化合成（買い目生成仕様_v1.md §1）

**④オッズ乖離はここに入れない**（循環回避。買い目生成仕様§0参照）。

設定：config/cards.json の "score_weights"（speed 0.45 / aptitude 0.30 / human 0.25）

TODO（実装）：
- §1.1 レース内 z標準化 → 加重和（sd_race=0のときはz=0）
- §1.2 欠損ファクターは馬ごとに重み再正規化。全欠損はbase_score=None
- 不確実フラグ（n_usable<=2 の speed、該当走0本の aptitude 等）の伝播
"""
from __future__ import annotations

from typing import Any


def z_standardize(values: list[float | None]) -> list[float | None]:
    """レース内 z標準化。Noneはそのまま伝播させる（買い目生成仕様§1.1）。"""
    raise NotImplementedError("買い目生成仕様§1.1 を実装")


def compute_base_scores(horses: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    レース1件分の出走馬リスト（各馬が speed/aptitude/human の生値と不確実フラグを持つ）から、
    base_score（z標準化＋加重和、欠損時再正規化）を計算して付与する。

    horses の各要素に "base_score": float|None, "uncertain": bool を追加して返す想定。
    """
    raise NotImplementedError("買い目生成仕様§1 を実装")
