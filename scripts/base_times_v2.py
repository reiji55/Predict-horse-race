"""
基準タイム表 v2（研究用の reference foundation）。

設計: docs/audit/SPEED_BASE_TIMES_V2_DESIGN_20260928.md（PR A — reference foundation only）

**Champion の config/base_times.json と予想経路には一切触れない。** ここで作るのは
「基準タイムを正しく作れる研究基盤」だけで、予想・Challenger はまだこの表を読まない。

    lookup   … speed_index.lookup_base_time() と同じ形（{場: {芝ダ: {距離: 秒}}}）
    meta     … 監査用。コースごとの n / 期間 / 中央値 / MAD / P25 / P75 / クラス内訳 / 馬場内訳、
               cutoff・出どころ・推定方法・ハッシュ。予想には使わない。

lookup と meta は**同じ build（同じバケット）**から作る。基準タイムの式は v1 と同一:

    base_time = median( win_time − class_offset[class] × dist/2000 )   （小数1桁）

同じレコードなら v1（scripts.build_base_times.build_table）と同じ値になる（テストで固定）。

--- 使い方（既定は dry-run：何も書かない）---

    python -m scripts.base_times_v2 --source raw --cutoff 2026-09-27
    python -m scripts.base_times_v2 --source file --input records.json --cutoff 2026-09-27 \\
        --artifact-id base-times-v2-coverage-20260928 --write

--write のときだけ data/reference/base_times/{artifact_id}.json と .meta.json を書く。
config/ 配下へは書かない（拒否する）。netkeiba からの取得はこのスクリプトでは行わない。
"""
from __future__ import annotations

import argparse
import collections
import copy
import datetime
import hashlib
import json
import logging
import re
import statistics
from pathlib import Path
from typing import Any, Iterable

from logic import speed_index
from scraper.common import constants
from scripts import build_base_times as bbt

logger = logging.getLogger("scripts.base_times_v2")

META_SCHEMA = "base-times-v2-meta/1"
ESTIMATOR_VERSION = "v1-median-class-offset"
FORMULA = "base_time = median(win_time - class_offset[class] * dist/2000), rounded to 0.1s"
QUANTILE_METHOD = "statistics.quantiles(method='inclusive')"
OUTPUT_DIR = bbt.ROOT / "data" / "reference" / "base_times"
CONFIG_DIR = bbt.ROOT / "config"
# artifact_id はファイル名になる。パス区切り・「..」・絶対パスを入れさせない
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# リポジトリ内で書いてよい場所の**許可リスト**（それ以外のリポジトリ内パスには一切書かない）。
# リポジトリの外（一時ディレクトリなど）は自由。
ARTIFACT_ALLOWED_DIRS = (OUTPUT_DIR,)
REPORT_DIR = OUTPUT_DIR / "reports"
REPORT_ALLOWED_DIRS = (REPORT_DIR,)
JST = datetime.timezone(datetime.timedelta(hours=9))


# --------------------------------------------------------------------------- 共通


def canonical_json(payload: Any) -> str:
    """キー順・空白に依存しない正規化 JSON（ハッシュ用）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _estimator_fields(record: dict[str, Any]) -> dict[str, Any]:
    """推定に効く列だけ（出どころ source は含めない。どの raw から拾っても同じハッシュにするため）。"""
    return {k: record.get(k) for k in ("date", "venue", "surface", "dist", "class", "going", "win_time")}


def _record_sort_key(record: dict[str, Any]) -> tuple:
    return tuple("" if v is None else str(v) for v in bbt.record_key(record))


# --------------------------------------------------------------------------- レコードの前処理


def prepare_records(records: Iterable[dict[str, Any]], cutoff: datetime.date
                    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    cutoff ガードと重複排除（どの経路のレコードにも同じ規則を掛ける）。

    - 日付が読めないレコードは捨てる（cutoff を判定できないので fail-closed）
    - 日付が cutoff より後のレコードは捨てる（cutoff 当日は使う）
    - bbt.record_key が同じレコードは、入力順で最初の1件だけ残す
    戻り値のレコードは record_key 順に並べ替える（入力順に依存しない決定的な出力にするため）。
    """
    kept: list[dict[str, Any]] = []
    rejected = {"missing_date": 0, "after_cutoff": 0, "duplicate": 0}
    seen: set[tuple] = set()
    for record in records:
        run_date = bbt._parse_date(record.get("date"))
        if run_date is None:
            rejected["missing_date"] += 1
            continue
        if run_date > cutoff:
            rejected["after_cutoff"] += 1
            continue
        key = bbt.record_key(record)
        if key in seen:
            rejected["duplicate"] += 1
            continue
        seen.add(key)
        kept.append(record)
    kept.sort(key=_record_sort_key)
    return kept, rejected


# --------------------------------------------------------------------------- 統計


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def course_stats(items: list[tuple[float, dict[str, Any]]], min_samples: int) -> dict[str, Any]:
    """1コースぶんの監査統計。value は lookup に載る値（n < min_samples なら None）。"""
    values = sorted(value for value, _ in items)
    n = len(values)
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    if n >= 2:
        p25, _, p75 = statistics.quantiles(values, n=4, method="inclusive")
    else:
        p25 = p75 = values[0]
    dates = sorted(str(r.get("date")) for _, r in items if r.get("date"))
    class_counts = collections.Counter(str(r.get("class")) for _, r in items)
    going_counts = collections.Counter(r.get("going") or "unknown" for _, r in items)
    ok = n >= min_samples
    return {
        "status": "ok" if ok else "insufficient_samples",
        "value": bbt.base_time_value(values) if ok else None,
        "n": n,
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "median": _round(median),
        "mad": _round(mad),
        "p25": _round(p25),
        "p75": _round(p75),
        "min": _round(values[0]),
        "max": _round(values[-1]),
        "class_counts": dict(sorted(class_counts.items())),
        "going_counts": dict(sorted(going_counts.items())),
    }


def coverage_totals(lookup: dict[str, Any]) -> dict[str, Any]:
    by_surface: dict[str, dict[str, int]] = {}
    for venue, surfaces in bbt.COURSES.items():
        for surface, dists in surfaces.items():
            row = by_surface.setdefault(surface, {"filled": 0, "total": 0})
            for dist in dists:
                row["total"] += 1
                row["filled"] += int(str(dist) in lookup.get(venue, {}).get(surface, {}))
    listed = {f"{v}/{s}/{d}" for v, ss in bbt.COURSES.items() for s, ds in ss.items() for d in ds}
    in_lookup = {f"{v}/{s}/{d}" for v, ss in lookup.items() for s, ds in ss.items() for d in ds}
    total = sum(r["total"] for r in by_surface.values())
    filled = sum(r["filled"] for r in by_surface.values())
    return {
        "courses_total": total,
        "courses_filled": filled,
        "fill_rate": round(filled / total, 4) if total else None,
        "by_surface": by_surface,
        "courses_missing": bbt.missing_courses(lookup),
        "courses_outside_catalog": sorted(in_lookup - listed),
    }


# --------------------------------------------------------------------------- build


def build_v2(records: Iterable[dict[str, Any]], *, cutoff: datetime.date, artifact_id: str,
             built_at: str, source: dict[str, Any], class_offset: dict[str, float] | None = None,
             min_samples: int = bbt.DEFAULT_MIN_SAMPLES, goings: list[str] | None = None
             ) -> dict[str, Any]:
    """
    レコードから lookup と meta を**同じバケットから**作る。

    built_at は meta に記録するだけで、ハッシュには含めない（同じ入力なら同じハッシュ）。
    """
    class_offset = class_offset if class_offset is not None else bbt.load_class_offset()
    records = list(records)
    kept, rejected = prepare_records(records, cutoff)
    buckets, skipped = bbt.bucket_records(kept, class_offset, goings)
    lookup, _counts = bbt.table_from_buckets(buckets, min_samples)

    courses = {f"{v}/{s}/{d}": course_stats(items, min_samples)
               for (v, s, d), items in sorted(buckets.items())}
    used = [r for items in buckets.values() for _, r in items]
    used.sort(key=_record_sort_key)
    dates = sorted(str(r["date"]) for r in used if r.get("date"))

    lookup_hash = sha256_text(canonical_json(lookup))
    records_hash = sha256_text(canonical_json([_estimator_fields(r) for r in used]))
    meta: dict[str, Any] = {
        "schema": META_SCHEMA,
        "artifact_id": artifact_id,
        "built_at": built_at,
        "cutoff_date": cutoff.isoformat(),
        "used_for_prediction": False,
        "champion_base_times_untouched": True,
        "source": {**source, "period": {"date_min": dates[0] if dates else None,
                                        "date_max": dates[-1] if dates else None}},
        "estimator": {
            "version": ESTIMATOR_VERSION,
            "formula": FORMULA,
            "class_offset": dict(sorted(class_offset.items())),
            "class_offset_hash": sha256_text(canonical_json(class_offset)),
            "min_samples": min_samples,
            "goings": sorted(goings) if goings else "all",
            "quantile_method": QUANTILE_METHOD,
            "dedupe_key": ["date", "venue", "surface", "dist", "class", "going", "win_time"],
        },
        "counts": {
            "input_records": len(records),
            "rejected": rejected,
            "kept_after_cutoff_and_dedupe": len(kept),
            "skipped_by_estimator": skipped,
            "used_records": len(used),
        },
        "coverage": coverage_totals(lookup),
        "courses": courses,
        "hashes": {"lookup_sha256": lookup_hash, "records_sha256": records_hash},
    }
    content = {k: v for k, v in meta.items() if k not in ("built_at", "hashes")}
    meta["hashes"]["meta_content_sha256"] = sha256_text(canonical_json(content))
    return {"lookup": lookup, "meta": meta}


def validate_artifact_id(artifact_id: Any) -> str:
    """artifact_id はファイル名の slug に限る（`/`・`\\`・`..`・絶対パスで外へ抜けさせない）。"""
    if not isinstance(artifact_id, str) or not ARTIFACT_ID_RE.fullmatch(artifact_id) or ".." in artifact_id:
        raise ValueError(f"artifact_id は英数字・'.'・'_'・'-' だけの名前にしてください: {artifact_id!r}")
    return artifact_id


def _is_within(path: Path, directory: Path) -> bool:
    resolved, base = path.resolve(), directory.resolve()
    return resolved == base or base in resolved.parents


def ensure_writable(path: Path, allowed: tuple[Path, ...] = ()) -> Path:
    """
    書き出し先の最終パスを resolve して検査する（許可リスト方式）。

    - リポジトリの外 → 書いてよい
    - リポジトリの中 → allowed のどれかの配下だけ。config/・コード・テスト・CI 設定などには書かない
    """
    if not _is_within(path, bbt.ROOT) or any(_is_within(path, d) for d in allowed):
        return path
    where = ", ".join(str(d.relative_to(bbt.ROOT)) + "/" for d in allowed) or "（なし）"
    raise ValueError(f"{path} には書き出しません。リポジトリ内で書けるのは {where} だけです")


def _artifact_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_artifacts(result: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    """
    lookup と meta を書く。**一度書いた artifact は変えない**（forward 実験中に表が差し替わらないように）。

    - 両方とも無い                   → 書く
    - 両方あり、中身がバイト単位で同じ → 何もしない（同じ build の再実行）
    - 片方だけある / 中身が違う       → ValueError（新しい artifact_id を使うこと）
    書き出し先は ensure_writable で検査し、config/ などの保護対象には書かない。
    """
    artifact_id = validate_artifact_id(result["meta"]["artifact_id"])
    lookup_path = ensure_writable(output_dir / f"{artifact_id}.json", ARTIFACT_ALLOWED_DIRS)
    meta_path = ensure_writable(output_dir / f"{artifact_id}.meta.json", ARTIFACT_ALLOWED_DIRS)
    for path in (lookup_path, meta_path):
        if path.resolve().parent != output_dir.resolve():
            raise ValueError(f"書き出し先が output_dir の外です: {path}")

    planned = {lookup_path: _artifact_bytes(result["lookup"]), meta_path: _artifact_bytes(result["meta"])}
    existing = [path for path in planned if path.exists()]
    if len(existing) == len(planned):
        if all(path.read_bytes() == data for path, data in planned.items()):
            logger.info("artifact %s は同じ内容で書き出し済みのため何もしません", artifact_id)
            return lookup_path, meta_path
        raise ValueError(f"artifact {artifact_id} は別の内容で既に存在します。新しい artifact_id を使ってください")
    if existing:
        raise ValueError(f"artifact {artifact_id} の片方だけが存在します: {existing}。手で確認してください")

    output_dir.mkdir(parents=True, exist_ok=True)
    for path, data in planned.items():
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    return lookup_path, meta_path


# --------------------------------------------------------------------------- coverage dry-run


def _iter_raw(raw_dir: Path) -> Iterable[tuple[str, dict[str, Any]]]:
    for path in sorted(raw_dir.glob("*.json")) if raw_dir.is_dir() else []:
        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.warning("raw の読み込みに失敗したのでスキップします path=%s", path)
            continue
        if isinstance(raw, dict):
            yield path.name, raw


def race_speed_quality(race: dict[str, Any], base_times: dict[str, Any],
                       speed_config: dict[str, Any]) -> dict[str, Any]:
    """
    そのレースで speed ガードが通るか（build_predictions.prepare_horses と同じ手順で計算）。

    compute_horse_speed → speed_raw → apply_race_speed_guard。スコアは作らない（カバレッジだけ）。
    """
    surface = (race.get("course") or {}).get("surface")
    horses = []
    for entry in race.get("entries") or []:
        speed = speed_index.compute_horse_speed(entry.get("past_runs") or [], speed_config, base_times,
                                                target_surface=surface)
        horses.append({"speed_raw": speed_index.speed_raw(speed, speed_config),
                       "n_usable": speed["n_usable"] if speed else 0})
    quality = speed_index.apply_race_speed_guard(horses, copy.deepcopy(speed_config))
    return {k: quality[k] for k in ("qualified_horses", "total_horses", "coverage", "used", "reason")}


PLACEHOLDER_BASE_TIME = 100.0


def ceiling_table(demand: Iterable[str]) -> dict[str, Any]:
    """
    「必要なコースが全部埋まったら」の上限見積もり用の表。**値はダミー**。

    speed ガードの通過（usable 走の本数・有効頭数）は基準タイムの有無だけで決まるので、
    この表で数えた qualified / used は意味を持つ。指数の値そのものは意味を持たない。
    """
    table: dict[str, Any] = {}
    for key in demand:
        venue, surface, dist = key.split("/")
        table.setdefault(venue, {}).setdefault(surface, {})[dist] = PLACEHOLDER_BASE_TIME
    return table


def coverage_report(raw_dir: Path, tables: dict[str, dict[str, Any]],
                    speed_config: dict[str, Any] | None = None, top_n: int = 15,
                    include_ceiling: bool = True) -> dict[str, Any]:
    """
    手元 raw の過去走が必要とするコースに、各表がどれだけ応えられるか（dry-run・書き出しなし）。

    tables: {"champion_v1": config/base_times.json, "candidate_v2": build_v2 の lookup, ...}
    これは**カバレッジの見積もり**であり、精度や成績の検証ではない
    （同じ raw から作った候補表なら、なおさら retrospective な参考値）。
    """
    speed_config = speed_config if speed_config is not None else speed_index.load_config()
    # speed で使える走（JRA10場 × 芝/ダ）だけを需要として数える。
    # 地方・海外・障害の走は基準タイムがあっても speed_index.is_usable で落ちるので別枠。
    demand: collections.Counter = collections.Counter()
    ineligible: collections.Counter = collections.Counter()
    races: list[dict[str, Any]] = []
    for _name, raw in _iter_raw(raw_dir):
        for race in raw.get("races") or []:
            for entry in race.get("entries") or []:
                for run in entry.get("past_runs") or []:
                    venue, surface, dist = run.get("venue"), run.get("surface"), run.get("dist")
                    if not (venue and surface and dist):
                        continue
                    if venue in constants.JRA_VENUES and surface in speed_index.VALID_SURFACES:
                        demand[f"{venue}/{surface}/{int(dist)}"] += 1
                    elif venue not in constants.JRA_VENUES:
                        ineligible["non_jra_venue"] += 1
                    else:
                        ineligible["non_flat_surface"] += 1
            races.append(race)
    races.sort(key=lambda r: str(r.get("id")))
    if include_ceiling:
        tables = {**tables, "ceiling_all_demanded": ceiling_table(demand)}

    def has(table: dict[str, Any], key: str) -> bool:
        venue, surface, dist = key.split("/")
        return speed_index.lookup_base_time(table, venue, surface, int(dist)) is not None

    total_runs = sum(demand.values())
    out_tables: dict[str, Any] = {}
    for name, table in tables.items():
        missing = collections.Counter({k: n for k, n in demand.items() if not has(table, k)})
        out_tables[name] = {
            "courses_filled": coverage_totals(table)["courses_filled"],
            "runs_covered": total_runs - sum(missing.values()),
            "runs_missing": sum(missing.values()),
            "run_coverage": round((total_runs - sum(missing.values())) / total_runs, 4) if total_runs else None,
            "demanded_courses_missing": len(missing),
            "top_missing": [{"course": k, "runs": n}
                            for k, n in sorted(missing.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]],
        }

    race_rows = []
    for race in races:
        course = race.get("course") or {}
        race_rows.append({
            "race_id": race.get("id"),
            "course": f"{race.get('venue')}/{course.get('surface')}/{course.get('dist')}",
            **{name: race_speed_quality(race, table, speed_config) for name, table in tables.items()},
        })

    return {
        "note": "カバレッジの見積もり。精度・成績の検証ではない。予想には使わない。"
                "runs / courses は speed で使える走（JRA10場×芝/ダ）だけ。地方・海外・障害は ineligible に分ける。"
                "ceiling_all_demanded は必要コースが全部埋まった場合の上限で、値はダミー（頭数だけ意味を持つ）。",
        "guard": {k: speed_config.get("guard", {}).get(k)
                  for k in ("same_surface_only", "min_usable_runs", "min_race_coverage")},
        "demand": {"eligible_runs": total_runs, "eligible_courses": len(demand),
                   "ineligible_runs": sum(ineligible.values()),
                   "ineligible_by_reason": dict(sorted(ineligible.items()))},
        "tables": out_tables,
        "races": race_rows,
    }


# --------------------------------------------------------------------------- CLI


def _file_digest(path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {"path": str(resolved.relative_to(bbt.ROOT)) if resolved.is_relative_to(bbt.ROOT) else str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="基準タイム表 v2（研究用）を作る。既定は dry-run")
    parser.add_argument("--source", choices=["raw", "file"], default="raw",
                        help="raw＝コミット済み raw/*.json、file＝手元の JSON/CSV（netkeiba 取得はしない）")
    parser.add_argument("--input", type=Path, help="--source file の入力")
    parser.add_argument("--raw-dir", type=Path, default=bbt.RAW_DIR)
    parser.add_argument("--cutoff", required=True, type=datetime.date.fromisoformat,
                        help="この日付より後のレースは使わない（YYYY-MM-DD・当日は使う）")
    parser.add_argument("--artifact-id", default=None)
    parser.add_argument("--built-at", default=None, help="meta に記録する作成日時（既定は現在時刻）")
    parser.add_argument("--min-samples", type=int, default=bbt.DEFAULT_MIN_SAMPLES)
    parser.add_argument("--going", action="append", help="馬場で絞る（既定は全馬場＝v1 と同じ）")
    parser.add_argument("--report", type=Path,
                        help="カバレッジ報告の JSON をここへ書く（リポジトリ外か data/reference/base_times/reports/ のみ）")
    parser.add_argument("--write", action="store_true", help="lookup と meta を --output-dir に書く")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.source == "raw":
        records, collect_report = bbt.collect_raw_records(args.raw_dir)
        inputs = [_file_digest(p) for p in sorted(args.raw_dir.glob("*.json"))]
    else:
        if args.input is None:
            parser.error("--source file には --input が必要です")
        records, collect_report = bbt.load_records(args.input), None
        inputs = [_file_digest(args.input)]

    artifact_id = args.artifact_id or f"base-times-v2-{args.source}-{args.cutoff.strftime('%Y%m%d')}"
    try:
        validate_artifact_id(artifact_id)
        if args.report:
            ensure_writable(args.report, REPORT_ALLOWED_DIRS)
    except ValueError as exc:
        parser.error(str(exc))
    built_at = args.built_at or datetime.datetime.now(JST).isoformat(timespec="seconds")
    result = build_v2(records, cutoff=args.cutoff, artifact_id=artifact_id, built_at=built_at,
                      source={"kind": args.source, "inputs": inputs, "collector": collect_report},
                      min_samples=args.min_samples, goings=args.going)
    meta = result["meta"]
    report = coverage_report(args.raw_dir, {"champion_v1": speed_index.load_base_times(),
                                            "candidate_v2": result["lookup"]})

    cov = meta["coverage"]
    print(f"artifact_id={artifact_id} cutoff={meta['cutoff_date']}")
    print(f"レコード {meta['counts']['input_records']} 件 → 使用 {meta['counts']['used_records']} 件 "
          f"（除外 {meta['counts']['rejected']}、推定対象外 {meta['counts']['skipped_by_estimator']}）")
    print(f"候補 v2 の基準タイム: {cov['courses_filled']}/{cov['courses_total']} コース "
          + " ".join(f"{s}={r['filled']}/{r['total']}" for s, r in cov["by_surface"].items()))
    for name, row in report["tables"].items():
        used = sum(1 for r in report["races"] if r[name]["used"])
        print(f"[{name}] 過去走カバー {row['runs_covered']}/{row['runs_covered'] + row['runs_missing']} "
              f"・speed が通るレース {used}/{len(report['races'])}")
    print(f"lookup_sha256={meta['hashes']['lookup_sha256']}")

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8")
        print(f"カバレッジ報告: {args.report}")
    if args.write:
        lookup_path, meta_path = write_artifacts(result, args.output_dir)
        print(f"書き出し: {lookup_path} / {meta_path}")
    else:
        print("dry-run のため表は書き出しません（--write で data/reference/base_times/ へ）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
