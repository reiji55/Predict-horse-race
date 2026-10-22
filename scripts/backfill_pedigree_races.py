"""
過去レースの出馬表「過去5走」ページ（C2）から、出走馬の血統（父・母・母父）をまとめて集める。

1ページで出走馬全員（〜18頭）の血統が取れるので、馬ごとの血統表を取るより少ないアクセスで済む。
`race.netkeiba.com` は GitHub Actions からも取れている経路（c2_shutuba_past の冒頭）なので、
手動 workflow（.github/workflows/manual_pedigree_backfill.yml）で少しずつ回せる。

- 1ページあたり約4秒（scraper.common.http の間隔をそのまま守る）。200レースで約15分。
- 済んだレースは data/pedigree/backfill_done_race_ids.txt に記録し、次の実行で飛ばす。
- 最初の5レースで血統が1頭も取れなければ止める（Horse_Info の並びが推定と違う＝アクセスの無駄を防ぐ）。
  そのときは5レースを「済み」にしないので、パーサーを直したあと同じレースから取り直せる。

使い方：
  python -m scripts.backfill_pedigree_races --race-ids data/pedigree/backfill_race_ids_11R.txt --limit 200
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from scraper.fetchers import c2_shutuba_past, pedigree

logger = logging.getLogger("scripts.backfill_pedigree_races")
DONE_PATH = pedigree.STORE_PATH.parent / "backfill_done_race_ids.txt"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="過去レースの過去5走ページから血統を集める")
    ap.add_argument("--race-ids", required=True, help="netkeibaのレースID（12桁）を1行に1つ書いたファイル")
    ap.add_argument("--limit", type=int, default=200, help="今回取りに行く最大レース数（既定200＝約15分）")
    ap.add_argument("--store", default=str(pedigree.STORE_PATH), help="保存先CSV")
    ap.add_argument("--done", default=str(DONE_PATH), help="済んだレースIDを記録するファイル")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    store_path, done_path = Path(args.store), Path(args.done)
    done = set(done_path.read_text(encoding="utf-8").split()) if done_path.exists() else set()
    ids = [x.strip() for x in Path(args.race_ids).read_text(encoding="utf-8").splitlines() if x.strip()]
    todo = [x for x in ids if x not in done][: args.limit]
    store = pedigree.load_store(store_path)
    logger.info("対象 %d レースのうち済み %d、今回 %d レース（目安 %.0f 分）", len(ids), len(done), len(todo),
                len(todo) * 4 / 60)

    horses = with_ped = added = 0
    newly_done: list[str] = []
    for n, race_id in enumerate(todo, 1):
        if n > 5 and with_ped == 0:
            # 並びの推定違いなら、直したあとで取り直せるように「済み」にしない
            logger.error("最初の5レースで血統が1頭も取れません。Horse_Info の並びを docs/PEDIGREE.md の手順で確認してください")
            newly_done = []
            break
        try:
            by_horse = c2_shutuba_past.fetch_shutuba_past(race_id)
        except RuntimeError:
            logger.warning("取得失敗のため次回に回します: %s", race_id)
            continue
        if not by_horse:
            logger.warning("過去5走の表がありません（古いレースはページが無い可能性）: %s", race_id)
            newly_done.append(race_id)
            continue
        for ref, found in by_horse.items():
            horses += 1
            if found.get("pedigree"):
                with_ped += 1
                added += pedigree.add_record(store, ref, found["pedigree"], "shutuba_past_backfill")
        newly_done.append(race_id)
        if n % 20 == 0:
            pedigree.save_store(store, store_path)
            logger.info("%d/%d レース：出走馬 %d 頭、血統あり %d 頭、新規 %d 頭", n, len(todo), horses, with_ped, added)

    pedigree.save_store(store, store_path)
    done_path.parent.mkdir(parents=True, exist_ok=True)
    with done_path.open("a", encoding="utf-8") as f:
        for race_id in newly_done:
            f.write(race_id + "\n")
    logger.info("完了：%d レース、出走馬 %d 頭、血統あり %d 頭、新規 %d 頭（保存済み合計 %d 頭）",
                len(newly_done), horses, with_ped, added, len(store))
    return 1 if (todo and with_ped == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
