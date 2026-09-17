"""
③ 人的スコア（買い目生成仕様_v1.md §1.5）

騎手・厩舎の着度数（共通内部フォーマット仕様§2.6）から、縮小推定（全体平均へ引き戻す）を経て計算する。
**分母が小さいと率が暴れる**ので縮小推定を必ずかける。

設定：config/cards.json の "human" セクション（w_jockey, w_trainer, shrink_m）

--- 計算式（§1.5） ---

    rate_shrunk = (hits + α × m) / (starts + m)      # m=引き戻しの強さ, α=全体平均率
    ③ = w_jockey × 騎手rate_shrunk + w_trainer × 厩舎rate_shrunk      # 0.6 / 0.4

- 騎手：当該場の**複勝率** (wins + seconds + thirds) / starts を縮小推定
- 厩舎：全体成績で同様
- starts が極端に小さい（< m）騎手は自動的に中立（全体平均）へ寄る

騎手・厩舎の片方だけ欠損している場合は、**残った側の重みを1.0に再正規化**して返す
（base_score §1.2 の「欠損ファクターは重み再正規化」と同じ考え方をファクター内部でも適用）。
両方欠損なら None を返し、③そのものが欠損として base_score 側で再正規化される。
"""
from __future__ import annotations

from typing import Any

# 着度数から複勝率を作るときのキー（取得項目仕様§2.6）
_HIT_KEYS = ("wins", "seconds", "thirds")


def shrink_rate(hits: int, starts: int, overall_rate: float, m: float) -> float:
    """
    縮小推定（買い目生成仕様§1.5）。

    hits: 的中数（複勝なら 1着+2着+3着）
    starts: 試行数（騎乗数・出走数）
    overall_rate: 全体平均率 α（リーグ平均。分母が小さいときの引き戻し先）
    m: 引き戻しの強さ。starts が m と同程度なら、実績と全体平均が半々でブレンドされる
    """
    if m < 0:
        raise ValueError(f"shrink_m は0以上である必要があります: {m}")
    denominator = starts + m
    if denominator <= 0:
        return overall_rate
    return (hits + overall_rate * m) / denominator


def place_rate_from_stats(stats: dict[str, Any] | None, overall_rate: float, m: float) -> float | None:
    """着度数（§2.6）から縮小推定済みの複勝率を作る。stats が無い・starts が無いなら None。"""
    if not stats:
        return None
    starts = stats.get("starts")
    if starts is None:
        return None
    hits = sum(stats.get(key) or 0 for key in _HIT_KEYS)
    return shrink_rate(hits, starts, overall_rate, m)


def compute_human_score(jockey_stats: dict[str, Any] | None, trainer_stats: dict[str, Any] | None,
                        overall_jockey_rate: float, overall_trainer_rate: float,
                        config: dict[str, Any]) -> float | None:
    """
    jockey_stats / trainer_stats（取得項目仕様§2.6の着度数）から人的スコアの生値を計算する。
    両方nullなら None。片方だけなら残った側の重みを再正規化して返す。
    """
    m = config["shrink_m"]
    jockey_rate = place_rate_from_stats(jockey_stats, overall_jockey_rate, m)
    trainer_rate = place_rate_from_stats(trainer_stats, overall_trainer_rate, m)

    parts: list[tuple[float, float]] = []  # (重み, 率)
    if jockey_rate is not None:
        parts.append((config["w_jockey"], jockey_rate))
    if trainer_rate is not None:
        parts.append((config["w_trainer"], trainer_rate))

    if not parts:
        return None

    weight_total = sum(w for w, _ in parts)
    if weight_total <= 0:
        return None
    return sum(w * rate for w, rate in parts) / weight_total


def league_place_rate(all_stats: list[dict[str, Any] | None], default: float = 0.25) -> float:
    """
    縮小推定の引き戻し先 α（全体平均複勝率）を、手元の着度数から推定する。

    本来はリーディング表の全騎手・全厩舎の合計から出すのが筋（取得項目仕様§1.2の一括取得方式は
    まさにそれを可能にするための設計）。raw に載っている範囲で Σhits / Σstarts を取る。
    データが1件も無ければ default（複勝率の常識的な水準）を返す。
    """
    total_hits = 0
    total_starts = 0
    for stats in all_stats:
        if not stats or not stats.get("starts"):
            continue
        total_starts += stats["starts"]
        total_hits += sum(stats.get(key) or 0 for key in _HIT_KEYS)

    if total_starts <= 0:
        return default
    return total_hits / total_starts
