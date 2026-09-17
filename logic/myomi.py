"""
妙味メーター計算（妙味メーター仕様_v1_確定版.md 全章）

入力：p（logic/prob_model.softmax_scores）, q（logic/prob_model.market_support）, n_usable群（信頼度用）
出力：myomi (0-100), myomi_parts {"umami": float, "conf": float}, legendary(bool)

設定：config/myomi.json（§8）

TODO（実装）：
- A. 旨みの深さ：EV_i = p_i*odds_i の最大値、edge_capで正規化（§2 A）
- B. 旨みの広がり：Σmax(p_i-q_i, 0)、breadth_capで正規化（§2 B）
- C. 予測信頼度：data_cov（n_usable>=usable_run_minの馬の割合）とconf_floor（§2 C）
   ※頭数は入れない（§7#4確定）
- 合成：myomi = clamp(100 * (w_edge*A + w_breadth*B) * conf, 0, 100)（§2 合成）
- legendary = myomi > myomi_threshold
"""
from __future__ import annotations

from typing import Any


def compute_myomi(p: list[float], q: list[float], n_usable_list: list[int],
                   win_odds: list[float], config: dict[str, Any]) -> dict[str, Any]:
    """
    レース1件分の p, q, n_usable, win_odds（出走馬順に対応するリスト）から
    {"myomi": float, "myomi_parts": {"umami": float, "conf": float}, "legendary": bool} を返す。
    """
    raise NotImplementedError("妙味メーター仕様§2 を実装")
