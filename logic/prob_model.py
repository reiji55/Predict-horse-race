"""
スコア→モデル勝率p（softmax） / オッズ→市場支持率q

**妙味メーター仕様_v1.md §1・§1.1 と 買い目生成仕様_v1.md §3 で共有する部品**。二重定義しない。

設定：config/myomi.json の "prob_model"（temperature）

- p_i = softmax(base_score_i / T)。base_score=None の馬は softmax から除外し、p も None
- q_i = (1/odds_i) を正規化（生の Σr は約1.25＝控除率20%ぶん上振れする。正規化で割り戻す）
- win_odds=None の馬は Σ から除外し、q も None

どちらも「Noneの馬は分母に入れない」点が肝。欠損馬を0扱いすると残りの確率が薄まってしまう。
"""
from __future__ import annotations

import math


def softmax_scores(scores: list[float | None], temperature: float) -> list[float | None]:
    """
    base_scoreのリストからモデル勝率pのリストを返す（妙味メーター仕様§1）。

    scores: レース内の各馬の base_score。None（全ファクター欠損）はそのまま None を返す
    temperature: 分布の尖り具合T。低T＝1強に集中、高T＝横並び
    戻り値: 各馬のp（Noneを除いた合計が1.0）。Noneの馬はNoneのまま
    """
    if temperature <= 0:
        raise ValueError(f"temperature は正の数である必要があります: {temperature}")

    usable = [(i, s) for i, s in enumerate(scores) if s is not None]
    if not usable:
        return [None] * len(scores)

    # オーバーフロー回避のため最大値を引いてから指数化（結果は不変）
    max_score = max(s for _, s in usable)
    exps = {i: math.exp((s - max_score) / temperature) for i, s in usable}
    total = sum(exps.values())

    return [exps[i] / total if i in exps else None for i in range(len(scores))]


def market_support(win_odds: list[float | None]) -> list[float | None]:
    """
    単勝オッズのリストから市場支持率qのリストを返す（妙味メーター仕様§1.1）。

    r_i = 1/odds_i（控除率を含んだ生の暗黙確率）→ q_i = r_i / Σr で正規化。
    この正規化が、オッズに埋め込まれたハウスエッジ（Σr≈1.25）を割り戻して
    「純粋な人気分布」に直す処理にあたる。
    """
    r: list[float | None] = []
    for odds in win_odds:
        if odds is None or odds <= 0:
            r.append(None)
        else:
            r.append(1.0 / odds)

    total = sum(v for v in r if v is not None)
    if total <= 0:
        return [None] * len(win_odds)

    return [v / total if v is not None else None for v in r]


def overround(win_odds: list[float | None]) -> float | None:
    """
    生の Σ(1/odds)（＝overround）を返す。1.25前後なら控除率20%と整合する。
    取得したオッズが妥当かの健全性チェック用（妙味メーター仕様§1.1の注記）。
    """
    values = [1.0 / o for o in win_odds if o is not None and o > 0]
    if not values:
        return None
    return sum(values)
