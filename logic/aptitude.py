"""
② 適性スコア（買い目生成仕様_v1.md §1.4）

「今日の条件への当てはまり」を過去走の着順から作る。スピード指数（①）とは役割を分離。

設定：config/cards.json の "aptitude" セクション（w_surface, b_dist, b_venue, b_going, dist_tol）

TODO（実装）：
- §1.4(a) prox（今日の条件との近さ）：芝ダ一致必須・距離の近さ・同場/同馬場ボーナス
- §1.4(b) perf（頭数正規化した着順）と prox 加重平均
- 該当走0本の場合は欠損（None）を返す → base_score.py 側で重み再正規化（§1.2）
"""
from __future__ import annotations

from typing import Any


def compute_aptitude(past_runs: list[dict[str, Any]], today_course: dict[str, Any],
                      config: dict[str, Any]) -> float | None:
    """
    馬1頭の past_runs と今日のコース条件（course dict: surface, dist, venue, going）から
    適性スコアの生値（z標準化前）を計算する。該当走が0本なら None。
    """
    raise NotImplementedError("買い目生成仕様§1.4 を実装")
