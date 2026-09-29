"""
基準タイム v2 用の勝ちタイムを、netkeiba のレース検索から**管理された形で**取得する（PR B）。

設計: docs/audit/SPEED_BASE_TIMES_V2_DESIGN_20260928.md §5.1・§14（PR B — frozen artifact）

- 取得は既存の経路（scripts.build_base_times.fetch_course_records → scraper.common.http）だけを使う。
  リクエスト間隔 3〜5 秒・UA 明示・リトライ1回のマナーはそのまま。
- 順番は「芝を最優先 → 手元 raw の需要が多い順 → コース表の順」。
- 1コース取るごとに途中経過（.partial.json）を保存し、止まっても続きから再開できる。
- 全コースを取り終えたら、生レコードを1ファイルに固定して保存する（一度書いたら変えない）。
  cutoff はここでは掛けない（取得した事実をそのまま残す）。cutoff は base_times_v2 の build で掛ける。
- config/base_times.json・予想経路には一切触れない。書き出し先は data/reference/base_times/sources/ だけ。

    python -m scripts.acquire_base_times_v2 --start-year 2023 --end-year 2026 --max-courses 20
    （同じコマンドを繰り返すと、未取得のコースだけを続きから取る）
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable

from logic import speed_index
from scraper.common import constants
from scraper.common import http as http_mod
from scripts import base_times_v2 as v2
from scripts import build_base_times as bbt

logger = logging.getLogger("scripts.acquire_base_times_v2")

SCHEMA = "base-times-v2-records/1"
SOURCES_DIR = v2.OUTPUT_DIR / "sources"
SURFACE_ORDER = ("芝", "ダ")

Course = tuple[str, str, int]
Fetch = Callable[[str, str, int, int, int], list[dict[str, Any]]]


def course_key(course: Course) -> str:
    return f"{course[0]}/{course[1]}/{course[2]}"


def raw_demand(raw_dir: Path = bbt.RAW_DIR) -> collections.Counter:
    """手元 raw の過去走のうち、speed で使える走（JRA10場×芝/ダ）が必要とするコース。"""
    demand: collections.Counter = collections.Counter()
    for _name, raw in v2._iter_raw(raw_dir):
        for race in raw.get("races") or []:
            for entry in race.get("entries") or []:
                for run in entry.get("past_runs") or []:
                    venue, surface, dist = run.get("venue"), run.get("surface"), run.get("dist")
                    if venue in constants.JRA_VENUES and surface in speed_index.VALID_SURFACES and dist:
                        demand[(venue, surface, int(dist))] += 1
    return demand


def plan_courses(demand: collections.Counter | None = None, surfaces: Iterable[str] = SURFACE_ORDER,
                 venues: Iterable[str] | None = None) -> list[Course]:
    """取得順: 芝→ダ（surfaces の順）、同じ芝ダの中では raw 需要の多い順、同数ならコース表の順。"""
    demand = demand or collections.Counter()
    surfaces = list(surfaces)
    venues = set(venues) if venues else None
    catalog = [(v, s, d) for v, by_surface in bbt.COURSES.items() for s, dists in by_surface.items()
               for d in dists if s in surfaces and (venues is None or v in venues)]
    order = {c: i for i, c in enumerate(catalog)}
    return sorted(catalog, key=lambda c: (surfaces.index(c[1]), -demand.get(c, 0), order[c]))


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in ("date", "venue", "surface", "dist", "going", "class", "heads", "win_time")}


def _write_json(path: Path, payload: Any) -> None:
    v2.ensure_writable(path, v2.ARTIFACT_ALLOWED_DIRS)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def acquire(courses: list[Course], start_year: int, end_year: int, output: Path,
            fetch: Fetch = bbt.fetch_course_records, max_courses: int | None = None,
            clock: Callable[[], datetime.datetime] | None = None) -> dict[str, Any]:
    """
    courses を順に取得する。途中経過は output の .partial.json に1コースごとに保存する。

    戻り値: {"status": "complete" | "partial", "done": n, "remaining": n, "path": 保存先}
    output が既にある（取得完了済み）場合は何もしない（固定済みのファイルは変えない）。
    """
    clock = clock or (lambda: datetime.datetime.now(v2.JST))
    v2.ensure_writable(output, v2.ARTIFACT_ALLOWED_DIRS)
    if output.exists():
        logger.info("%s は取得完了済みのため何もしません", output)
        return {"status": "complete", "done": len(courses), "remaining": 0, "path": output}

    partial_path = output.with_suffix(".partial.json")
    state = v2_read(partial_path) or {
        "schema": SCHEMA,
        "source": {
            "kind": "netkeiba_race_search",
            "url": bbt.SEARCH_URL,
            "params_example": bbt.build_search_params("東京", "芝", 1600, start_year, end_year),
            "start_year": start_year,
            "end_year": end_year,
            "http": {"min_interval_sec": http_mod.MIN_INTERVAL_SEC, "jitter_sec": http_mod.JITTER_SEC,
                     "max_retries": http_mod.MAX_RETRIES, "user_agent": http_mod.USER_AGENT},
            "note": "cutoff はここでは掛けない。base_times_v2 の build で cutoff より後の記録を除外する。",
        },
        "planned_courses": [course_key(c) for c in courses],
        "courses": {},
        "records": [],
    }
    if (state["source"]["start_year"], state["source"]["end_year"]) != (start_year, end_year):
        raise ValueError("途中経過と取得期間が違います。別の出力ファイルを使ってください")
    if state["planned_courses"] != [course_key(c) for c in courses]:
        raise ValueError("途中経過と取得予定のコースが違います。別の出力ファイルを使ってください")

    pending = [c for c in courses if course_key(c) not in state["courses"]]
    batch = pending if max_courses is None else pending[:max_courses]
    for index, (venue, surface, dist) in enumerate(batch, start=1):
        key = course_key((venue, surface, dist))
        started = clock()
        logger.info("取得 %d/%d（全体の残り %d）: %s", index, len(batch), len(pending) - index + 1, key)
        got = [_normalize_record(r) for r in fetch(venue, surface, dist, start_year, end_year)]
        # 検索条件と違うコースの行が混ざっていたら捨てる（場名の表記ゆれ・別距離）
        matched = [r for r in got if (r["venue"], r["surface"], r["dist"]) == (venue, surface, dist)]
        state["courses"][key] = {
            "fetched_at": started.isoformat(timespec="seconds"),
            "rows": len(got),
            "records": len(matched),
            "dropped_mismatched_course": len(got) - len(matched),
            "empty": not matched,
        }
        state["records"].extend(matched)
        if not matched:
            logger.warning("%s で0件でした（検索クエリか取得の異常を疑ってください）", key)
        _write_json(partial_path, state)

    remaining = len(pending) - len(batch)
    if remaining:
        return {"status": "partial", "done": len(courses) - remaining, "remaining": remaining, "path": partial_path}

    state["records"].sort(key=v2._record_sort_key)
    state["completed_at"] = clock().isoformat(timespec="seconds")
    state["records_sha256"] = v2.sha256_text(v2.canonical_json(state["records"]))
    _write_json(output, state)
    partial_path.unlink(missing_ok=True)
    return {"status": "complete", "done": len(courses), "remaining": 0, "path": output}


def v2_read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        raise ValueError(f"途中経過 {path} が読めません。中身を確認してください") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="基準タイム v2 用の勝ちタイムを netkeiba から管理された形で取得する")
    parser.add_argument("--start-year", type=int, default=2023)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--surfaces", nargs="+", default=list(SURFACE_ORDER), choices=list(SURFACE_ORDER))
    parser.add_argument("--venues", nargs="*")
    parser.add_argument("--max-courses", type=int, default=None, help="この実行で取るコース数の上限（再実行で続きから）")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--plan-only", action="store_true", help="取得順だけ表示して終わる（通信しない）")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    courses = plan_courses(raw_demand(), args.surfaces, args.venues)
    output = args.output or SOURCES_DIR / f"netkeiba-race-search-{args.start_year}-{args.end_year}.records.json"
    print(f"取得予定 {len(courses)} コース（先頭: {', '.join(course_key(c) for c in courses[:8])} …）")
    if args.plan_only:
        return 0
    try:
        v2.ensure_writable(output, v2.ARTIFACT_ALLOWED_DIRS)
    except ValueError as exc:
        parser.error(str(exc))
    report = acquire(courses, args.start_year, args.end_year, output, max_courses=args.max_courses)
    print(f"{report['status']}: {report['done']}/{len(courses)} コース取得済み（残り {report['remaining']}）→ {report['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
