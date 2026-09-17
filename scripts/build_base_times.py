"""
config/base_times.json 初期構築スクリプト（スピード指数仕様_v1.md §4）

**1回きり**の実行を想定（以後は年1回程度の更新で十分）。

対象：JRA10場 × 芝/ダ × 施行距離の全コース（約120通り）
手順：
  1. 各コースについて、直近3年の全クラスの勝ちタイムを集める
  2. クラス補正を引いてOP水準に正規化：normalized = win_time - class_offset[class] * dist/2000
  3. 正規化後タイムの中央値を取る → base_time

出力：config/base_times.json
  {"小倉": {"芝": {"1200": 67.8, ...}, "ダ": {...}}, ...}

TODO（実装フェーズ・netkeibaのどのページから勝ちタイム一覧を取るか確定後）：
- どのページ（一覧系？）から過去3年の勝ちタイムをどう集めるか確定（スピード指数仕様§4末尾）
- 中央値計算（外れ値に強い理由は仕様§4参照）
"""
from __future__ import annotations

import json
from pathlib import Path

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "config" / "base_times.json"


def build_base_times() -> dict:
    """JRA10場×芝ダ×距離の base_time を構築する（スピード指数仕様§4）。"""
    raise NotImplementedError("netkeibaの取得元ページ確定後に実装（スピード指数仕様§4）")


def main() -> None:
    data = build_base_times()
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
