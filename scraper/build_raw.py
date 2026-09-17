"""
raw/{week_id}.json ビルドスクリプト（取得項目・共通内部フォーマット仕様_v1.md §2 が出力契約）

流れ：
  A（レース一覧）→ メインレース特定
  → B（出馬表）で基本情報＋出走馬一覧
  → B2（オッズAPI）で単勝・複勝・人気を埋める
     ※ 出馬表HTMLのオッズ欄はJS描画のため空。内部AJAX API（b2_odds）を叩いて補完する
  → C（馬戦績）で各出走馬の past_runs を埋める（同一週内キャッシュ）
  → D（騎手LB）／E（厩舎LB）で jockey_stats／trainer_stats を埋める
  → 正規化（取得項目仕様§2.1）を通して raw/{week_id}.json に書き出す

GitHub Actions（workflow_dispatch）から `python -m scraper.build_raw --week 2026-W27` のように呼ばれる想定
（引き継ぎ書v3 §4）。

TODO（各フェッチャーの実装が揃い次第）：
- メインレース絞り込みルールの確定（a_race_list.py 参照）
- Cページの週内キャッシュ：c_horse_history.fetch_horse_history_cached() を利用可能
  （プロセス内キャッシュのみ。土日で別Actions実行に分かれる場合の永続化は要確認）
- 正規化ルール（取得項目仕様§2.1）を通す共通処理の実装
- パース失敗時にフィールドをnullにして続行する例外ハンドリング（全体を止めない）
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from scraper.fetchers import (
    a_race_list,
    b_shutuba,
    b2_odds,
    c_horse_history,
    d_jockey_leading,
    e_trainer_leading,
)

logger = logging.getLogger("scraper.build_raw")

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "raw"


def build_week(week_id: str, dates: list[str]) -> dict:
    """
    週単位で raw データを組み立てる。

    week_id: "2026-W27" のようなISO週識別子
    dates: その週の対象日（例 ["2026-07-04", "2026-07-05"] 土日）
    戻り値: 取得項目仕様§2.2 のトップレベル構造
    """
    raise NotImplementedError(
        "各フェッチャーの実装完了後に組み立てロジックを実装（取得項目仕様§2）"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="週末3CARDS raw/{week_id}.json ビルド")
    parser.add_argument("--week", required=True, help='例: "2026-W27"')
    parser.add_argument("--dates", required=True, nargs="+", help='例: 2026-07-04 2026-07-05')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    data = build_week(args.week, args.dates)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{args.week}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("書き出し完了: %s", out_path)


if __name__ == "__main__":
    main()
