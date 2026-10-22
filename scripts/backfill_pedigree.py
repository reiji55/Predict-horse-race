"""
過去の馬の血統（父・母・母父）を、5代血統表ページから少しずつ集めて data/pedigree/pedigree.csv に追記する。

**手元のPC（またはColab）で手動実行する。GitHub Actions では動かさない。**
`db.netkeiba.com/horse/` は GitHub Actions から叩くとbot判定される（c2_shutuba_past の冒頭参照）。

- 間隔は scraper.common.http の 3〜5秒をそのまま守る（1頭あたり約4秒）。
  1,000頭で約70分、1万頭で約11時間。数日に分けて少しずつ回す前提。
- 途中で止めても、保存済みの馬は飛ばして続きから再開する（50頭ごとに保存）。
- 既に保存済みの馬は二度と取りに行かない（血統は変わらない）。

使い方：
  python -m scripts.backfill_pedigree --ids horse_ids.txt --limit 1000
    horse_ids.txt … netkeiba の馬ID（10桁）を1行に1つ
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from scraper.fetchers import pedigree

logger = logging.getLogger("scripts.backfill_pedigree")


def main() -> None:
    ap = argparse.ArgumentParser(description="過去の馬の血統を少しずつ集める（手元のPCで実行）")
    ap.add_argument("--ids", required=True, help="馬IDを1行に1つ書いたファイル")
    ap.add_argument("--limit", type=int, default=500, help="今回取りに行く最大頭数（既定500＝約35分）")
    ap.add_argument("--store", default=str(pedigree.STORE_PATH), help="保存先CSV")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    store_path = Path(args.store)
    store = pedigree.load_store(store_path)
    ids = [line.strip() for line in Path(args.ids).read_text(encoding="utf-8").splitlines() if line.strip()]
    todo = [i for i in ids if i not in store][: args.limit]
    logger.info("対象 %d 頭のうち未取得 %d 頭、今回 %d 頭を取りに行きます（目安 %.0f 分）",
                len(ids), len([i for i in ids if i not in store]), len(todo), len(todo) * 4 / 60)

    added, failed, started = 0, 0, time.monotonic()
    for n, horse_id in enumerate(todo, 1):
        ped = pedigree.fetch_ped_page(horse_id)
        if pedigree.add_record(store, horse_id, ped, "ped_page"):
            added += 1
        else:
            failed += 1
        if n % 50 == 0:
            pedigree.save_store(store, store_path)
            logger.info("%d/%d 頭（追加 %d、失敗 %d、経過 %.0f 分）", n, len(todo), added, failed,
                        (time.monotonic() - started) / 60)
    pedigree.save_store(store, store_path)
    logger.info("完了：追加 %d 頭、失敗 %d 頭（保存済み合計 %d 頭）", added, failed, len(store))


if __name__ == "__main__":
    main()
