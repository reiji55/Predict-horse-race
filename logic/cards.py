"""
印の付与・キャラ別買い目生成（買い目生成仕様_v1.md §2〜§5）

入力：base_score（logic/base_score）、p・q（logic/prob_model）、win_odds
出力：marks[]（印つき）、cards[]（キャラ別買い目、predictions.jsonスキーマ準拠）

設定：config/cards.json（marks, characters, combo_prob）

TODO（実装）：
- §2 印付与：base_score降順に ◎○▲△△✕（7位以下は無印）
- §4 value = p - q、myomi_rank（過小評価順）
- §5.1 sel = (1-λ)*base_rank_norm + λ*myomi_rank_norm（キャラ別λはconfig参照）
- §5.2 源さんの axis_base_rank_floor（下位すぎる馬は軸にしない）
- §5.3 Harville式での券種確率近似（ワイド/馬連/3連複）、payout_range概算
- §5.4 鳳の降臨判定（myomi > myomi_threshold）と1000円配分
- §5.5 不変条件の検証（100円単位、Σamt=total、券種、頭数、marks存在チェック）
        崩れたら生成エラーにする
"""
from __future__ import annotations

from typing import Any


def assign_marks(horses_with_base_score: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """base_score順に印(mk)を付与する（買い目生成仕様§2）。"""
    raise NotImplementedError("買い目生成仕様§2 を実装")


def compute_value_and_myomi_rank(p: list[float], q: list[float]) -> list[dict[str, Any]]:
    """value=p-q とそのレース内順位(myomi_rank)を計算する（買い目生成仕様§4）。"""
    raise NotImplementedError("買い目生成仕様§4 を実装")


def generate_card_for_character(char_id: str, horses: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """
    1キャラ分のcards[]要素を生成する（買い目生成仕様§5）。
    生成後、§5.5の不変条件を検証すること（崩れたら例外を投げる）。
    """
    raise NotImplementedError("買い目生成仕様§5 を実装")


def validate_card_invariants(card: dict[str, Any], marks: list[dict[str, Any]]) -> None:
    """買い目生成仕様§5.5の5条件を検証。崩れていたら ValueError を投げる。"""
    raise NotImplementedError("買い目生成仕様§5.5 を実装")
