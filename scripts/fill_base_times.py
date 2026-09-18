"""
基準タイム表を**少しずつ**埋める（`scripts/build_base_times.py` の運用ラッパー）。

全101コースを一度に取ると数百リクエストの連打になり、netkeibaのIP制限に触れかねない。
そこで「まだ埋まっていないコースを数件だけ取って、既存の表に追記する」形にした。
何日かに分けて走らせれば、アクセス量を抑えたまま表が埋まっていく。

    python -m scripts.fill_base_times --max-courses 6
    python -m scripts.fill_base_times --venues 阪神 中山 --max-courses 4

- 既に表にあるコースは飛ばすので、同じ設定で何度走らせても前に進む
- 取得できた勝ちタイムは既存の表に**マージ**する（他のコースを消さない）
- 1コースでも0件だったら、検索クエリが壊れた合図なので警告を出す
"""
from __future__ import annotations

import argparse
import json
import logging

from scripts import build_base_times as bbt

logger = logging.getLogger("scripts.fill_base_times")


def load_table() -> dict:
    if not bbt.OUTPUT_PATH.exists():
        return {}
    try:
        with bbt.OUTPUT_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("既存の base_times.json を読めなかったので新規作成します")
        return {}


def pending_courses(table: dict, venues: list[str] | None,
                    limit: int) -> list[tuple[str, str, int]]:
    """まだ表に無いコースを、指定した場（未指定なら全場）から limit 件まで返す。"""
    pending = []
    for venue, by_surface in bbt.COURSES.items():
        if venues and venue not in venues:
            continue
        for surface, dists in by_surface.items():
            for dist in dists:
                if str(dist) in table.get(venue, {}).get(surface, {}):
                    continue
                pending.append((venue, surface, dist))
                if len(pending) >= limit:
                    return pending
    return pending


def merge_table(base: dict, addition: dict) -> dict:
    """新しく求まったコースだけを既存の表に足す（他のコースは触らない）。"""
    for venue, by_surface in addition.items():
        for surface, by_dist in by_surface.items():
            base.setdefault(venue, {}).setdefault(surface, {}).update(by_dist)
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description="基準タイム表を少しずつ埋める")
    parser.add_argument("--venues", nargs="*", help="対象の場。未指定なら全場から未取得順に")
    parser.add_argument("--max-courses", type=int, default=6, help="1回で取るコース数の上限")
    parser.add_argument("--start-year", type=int, default=2023)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--min-samples", type=int, default=bbt.DEFAULT_MIN_SAMPLES)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    table = load_table()
    targets = pending_courses(table, args.venues, args.max_courses)
    if not targets:
        print("未取得のコースはありません（指定範囲は埋まっています）")
        return

    print("今回取得するコース: " + ", ".join(f"{v}/{s}/{d}" for v, s, d in targets))

    records: list[dict] = []
    empty: list[str] = []
    for venue, surface, dist in targets:
        got = bbt.fetch_course_records(venue, surface, dist, args.start_year, args.end_year)
        if not got:
            empty.append(f"{venue}/{surface}/{dist}")
        records.extend(got)

    addition, report = bbt.build_table(records, min_samples=args.min_samples)
    merged = merge_table(table, addition)

    with bbt.OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    filled = sum(len(d) for by_surface in merged.values() for d in by_surface.values())
    total = sum(len(d) for by_surface in bbt.COURSES.values() for d in by_surface.values())
    print(f"勝ちタイム {len(records)} 件を取得 → 今回 {len(addition) and sum(len(d) for bs in addition.values() for d in bs.values())} コース確定")
    print(f"基準タイム表: {filled}/{total} コース")

    if empty:
        logger.warning(
            "0件だったコースがあります: %s。実在するコースで0件はありえないので、"
            "検索クエリ（build_search_params）が壊れていないか確認してください",
            ", ".join(empty),
        )


if __name__ == "__main__":
    main()
