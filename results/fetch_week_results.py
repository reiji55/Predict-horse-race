"""
その週のレース結果をFページからまとめて取得する（成績集計の前段）。

`results/build_results.py` は `{race_id: {finish, dividends}}` という形のJSONを入力に取るが、
**それを作る人がこれまでいなかった**（f_results は1レース分を取る部品として実装済みだったが、
週単位で回す口が無く、結果が1件も溜まらない状態だった）。ここがその欠けていた繋ぎ目。

    python -m results.fetch_week_results --week 2026-W38
    python -m results.build_results --results data/race_results.json

出力：`data/race_results.json`（中間ファイル。後方検証のためコミット対象）

- 対象レースは `raw/{week_id}.json` の source_refs から引く（predictions.json でもよい）
- **まだ結果が出ていないレースは黙って飛ばす**（土曜の昼に回しても落ちない）
- 既存の中間ファイルがあればマージする（土日で別々に実行する運用・引き継ぎ書v6 §1）
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from scraper.fetchers import f_results

logger = logging.getLogger("results.fetch_week_results")

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
OUTPUT_PATH = ROOT / "data" / "race_results.json"


def load_races(week_id: str) -> list[tuple[str, str]]:
    """raw/{week_id}.json から [(race_id, netkeibaのレースID)] を取り出す。"""
    path = RAW_DIR / f"{week_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"raw が見つかりません: {path}")
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)

    races = []
    for race in raw.get("races", []):
        race_id = race.get("id")
        source_ref = (race.get("source_refs") or {}).get("netkeiba")
        if race_id and source_ref:
            races.append((race_id, source_ref))
    return races


def load_existing(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("既存の中間ファイルを読めなかったので新規作成します: %s", path)
        return {}


def is_finished(result: dict[str, Any]) -> bool:
    """着順が3頭以上入っていれば「結果が出た」とみなす（3連複の判定に最低3頭要る）。"""
    return len(result.get("finish") or []) >= 3


def fetch_week(week_id: str, skip_existing: bool = True) -> dict[str, Any]:
    collected = load_existing(OUTPUT_PATH)
    races = load_races(week_id)
    logger.info("%s: %d レースの結果を確認します", week_id, len(races))

    fetched = skipped = pending = 0
    for race_id, source_ref in races:
        if skip_existing and is_finished(collected.get(race_id, {})):
            skipped += 1
            continue
        try:
            result = f_results.fetch_results(source_ref)
        except RuntimeError:
            logger.warning("結果ページを取得できませんでした（%s）。次のレースに進みます", race_id)
            continue
        if not is_finished(result):
            logger.info("%s はまだ結果が出ていないため飛ばします", race_id)
            pending += 1
            continue
        collected[race_id] = result
        fetched += 1

    logger.info("取得 %d / 取得済みで省略 %d / 未確定 %d（合計 %d レース）",
                fetched, skipped, pending, len(collected))
    return collected


def main() -> None:
    parser = argparse.ArgumentParser(description="週のレース結果をまとめて取得する")
    parser.add_argument("--week", required=True, help="ISO週識別子 例: 2026-W38")
    parser.add_argument("--refetch", action="store_true",
                        help="取得済みのレースも取り直す（既定は省略する）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    collected = fetch_week(args.week, skip_existing=not args.refetch)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(collected, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"書き出し完了: {OUTPUT_PATH}（{len(collected)} レース）")


if __name__ == "__main__":
    main()
