"""
合成スコア base_score ＝ ①②③のz標準化合成（買い目生成仕様_v1.md §1）

**④オッズ乖離はここに入れない**（循環回避。買い目生成仕様§0）。
④を混ぜると base_score → p がオッズ側に引かれ、「p と q の乖離」で測る妙味が
自分で薄めた乖離を測ることになる（妙味メーターが自分の尻尾を噛む）。
④の居場所は妙味メーターと買い目選定（value = p − q）の2箇所だけ。

設定：config/cards.json の "score_weights"（speed 0.45 / aptitude 0.30 / human 0.25）

--- 計算式（§1.1・§1.2） ---

    z(f)_i       = (f_i − mean_race(f)) / sd_race(f)        # レース内で標準化
    base_score_i = Σ_{f∈F_i} (w_f / Σ_{f∈F_i} w_f) × z(f)_i  # 欠損は馬ごとに重み再正規化

- sd_race(f) = 0（全馬同値）のときは z(f)=0（ゼロ除算回避）
- 全ファクター欠損の馬は base_score=None（印を打たない／買い目にも使わない）
- 不確実フラグ：①が n_usable ≤ 2、または②が該当走0本（欠損）の馬に立てる。
  妙味メーター仕様§2C の信頼度を下げる入力にもなる

--- ⚠ 表示スコアへの変換（温度Tの較正・要レビュー） ---

上式の base_score は z の加重和なので、おおむね −2〜+2 のスケールになる。
一方 softmax の温度は `config/myomi.json` の T=10.0 で、これは妙味メーター仕様§4 の検算サンプル
（score 60〜90）と predictions.sample.json の `marks[].score`（81.2, 74.0, …）が示すとおり
**0〜100スケールのスコアを前提にした値**である。z のまま T=10 で割ると全馬ほぼ横並びの p になり、
妙味が常時振り切れてしまう。

妙味メーター仕様§7 #2 が「T は仮値。スコアスケール確定後に較正する」と予告していた箇所なので、
**T を動かす代わりに、z を仕様が想定していた表示スケールへ移すアフィン変換を定義する**：

    score_i = score_base + score_unit × base_score_i        # 既定 70 + 10×z

`score_unit`(10) と T(10) が等しいので、実効的には「z空間で温度1.0の softmax」になる。
18頭立て・z が概ね ±2 の分布なら1番人気の p は25%前後となり、妥当な水準に収まる。
`marks[].score` にはこの変換後の値を入れる（サンプルの見た目とも一致する）。
**この判断は docs/OPEN_QUESTIONS.md B-6 に記録。**
"""
from __future__ import annotations

import statistics
from typing import Any

# horses[] の各要素が持つ生値のキー → config/cards.json の score_weights のキー
FACTOR_KEYS = {
    "speed_raw": "speed",
    "aptitude_raw": "aptitude",
    "human_raw": "human",
}


def z_standardize(values: list[float | None]) -> list[float | None]:
    """
    レース内 z標準化。Noneはそのまま伝播させる（買い目生成仕様§1.1）。
    有効値が1件以下、または全馬同値（sd=0）の場合は 0.0 を返す（ゼロ除算回避）。
    """
    usable = [v for v in values if v is not None]
    if not usable:
        return [None] * len(values)
    if len(usable) == 1:
        return [0.0 if v is not None else None for v in values]

    mean = statistics.fmean(usable)
    sd = statistics.pstdev(usable)  # レース内の全出走馬＝母集団なので pstdev
    if sd == 0:
        return [0.0 if v is not None else None for v in values]

    return [(v - mean) / sd if v is not None else None for v in values]


def compute_base_scores(horses: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    レース1件分の出走馬リストから base_score を計算して付与する（買い目生成仕様§1）。

    horses の各要素に期待するキー（無ければ欠損として扱う）:
      speed_raw / aptitude_raw / human_raw … 各ファクターの生値（float|None）
      uncertain                            … 呼び出し側が立てた不確実フラグ（bool・任意）

    各要素に "base_score"（float|None）と "uncertain"（bool）を追加したリストを返す
    （入力のdictを破壊的に更新して返す）。
    """
    weights = config["score_weights"]

    # ファクターごとにレース内z標準化
    z_by_factor: dict[str, list[float | None]] = {}
    for raw_key in FACTOR_KEYS:
        z_by_factor[raw_key] = z_standardize([h.get(raw_key) for h in horses])

    for i, horse in enumerate(horses):
        parts: list[tuple[float, float]] = []  # (重み, z値)
        for raw_key, weight_key in FACTOR_KEYS.items():
            z = z_by_factor[raw_key][i]
            if z is not None:
                parts.append((weights[weight_key], z))

        if not parts:
            horse["base_score"] = None
            horse["uncertain"] = True  # 評価材料が皆無＝最も不確実
            continue

        weight_total = sum(w for w, _ in parts)
        horse["base_score"] = (
            sum(w * z for w, z in parts) / weight_total if weight_total > 0 else None
        )
        # ファクターが1つでも欠けていれば不確実（呼び出し側が既に立てたフラグは尊重して合成）
        horse["uncertain"] = bool(horse.get("uncertain")) or len(parts) < len(FACTOR_KEYS)

    for horse in horses:
        horse["score"] = to_display_score(horse["base_score"], config)

    return horses


def to_display_score(base_score: float | None, config: dict[str, Any]) -> float | None:
    """
    z合成値を、仕様が想定する0〜100スケールへ移す（本モジュール冒頭の注記を参照）。

        score = score_scale.base + score_scale.unit × base_score

    この値を `marks[].score` に格納し、softmax(score / T) の入力にする。
    """
    if base_score is None:
        return None
    scale = config.get("score_scale", {"base": 70, "unit": 10})
    return scale["base"] + scale["unit"] * base_score
