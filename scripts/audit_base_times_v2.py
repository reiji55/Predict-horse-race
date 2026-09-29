"""
基準タイム v2 artifact の監査（PR B）。**値は一切変えない**（手修正しない）。見るだけ。

設計: docs/audit/SPEED_BASE_TIMES_V2_DESIGN_20260928.md §14（PR B: sample counts / outliers review）

入力:
    lookup / meta … data/reference/base_times/{artifact_id}.json / .meta.json
    source        … 生レコード（acquire_base_times_v2 の出力）。ここから同じ手順で作り直し、
                    lookup・ハッシュが一致するか（＝artifact が再現できるか）も確かめる
    champion      … config/base_times.json（読むだけ。v1 との差を見る）
    raw           … 手元 raw（カバレッジ・9/27 の speed guard 回復見込み）

判定の閾値は下の定数で**事前に**決めてある（結果を見て変えない）。フラグは「要確認」で、
自動で値を直したり除外したりはしない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import statistics
from pathlib import Path
from typing import Any

from logic import speed_index
from scripts import base_times_v2 as v2
from scripts import build_base_times as bbt

AUDIT_SCHEMA = "base-times-v2-audit/1"

# --- 事前に決めた閾値（要確認フラグ用。値の修正・除外には使わない） ---------------------
THIN_SAMPLE_N = 20                 # これ未満のコースは「サンプルが薄い」として一覧化
ROBUST_Z_OUTLIER = 5.0             # コース内: |x − median| / (1.4826·MAD) がこれを超える記録
WIDE_IQR_SEC_PER_1000M = 1.5       # コース内: (P75 − P25) / 距離(km) がこれを超えるコース
TREND_RESID_MIN_SEC = 1.0          # コース間: 芝ダ別の「距離→基準タイム」直線からのずれ（秒）の下限
TREND_RESID_ROBUST_Z = 3.5         #           かつ残差のロバスト z がこれを超えるコース
V1_DIFF_SEC = 0.5                  # v1（Champion の表）との差がこれを超えるコース
FOCUS_COURSES = ("東京/芝/1600", "東京/芝/1400", "阪神/芝/1600", "京都/芝/1600", "阪神/芝/1200")
FOCUS_RACES = ("20260927-hanshin-11", "20260927-nakayama-11")


def _robust_sd(values: list[float]) -> float:
    median = statistics.median(values)
    return 1.4826 * statistics.median(abs(v - median) for v in values)


def _key(venue: str, surface: str, dist: int) -> str:
    return f"{venue}/{surface}/{dist}"


def _lookup_items(lookup: dict[str, Any]) -> dict[str, float]:
    return {_key(v, s, int(d)): float(value) for v, ss in lookup.items() for s, ds in ss.items()
            for d, value in ds.items()}


def reproduce(records: list[dict[str, Any]], meta: dict[str, Any], lookup: dict[str, Any]) -> dict[str, Any]:
    """meta に記録された cutoff・推定設定で作り直し、lookup とハッシュが一致するか確かめる。"""
    est = meta["estimator"]
    rebuilt = v2.build_v2(
        records, cutoff=datetime.date.fromisoformat(meta["cutoff_date"]), artifact_id=meta["artifact_id"],
        built_at=meta["built_at"], source=meta["source"], class_offset=est["class_offset"],
        min_samples=est["min_samples"], goings=None if est["goings"] == "all" else est["goings"])
    return {
        "lookup_matches": rebuilt["lookup"] == lookup,
        "lookup_sha256_matches": rebuilt["meta"]["hashes"]["lookup_sha256"] == meta["hashes"]["lookup_sha256"],
        "records_sha256_matches": rebuilt["meta"]["hashes"]["records_sha256"] == meta["hashes"]["records_sha256"],
        "meta_content_sha256_matches":
            rebuilt["meta"]["hashes"]["meta_content_sha256"] == meta["hashes"]["meta_content_sha256"],
    }


def within_course_outliers(records: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    """コース内で他の勝ちタイムから大きく外れた記録（正規化後）。値は変えずに一覧化するだけ。"""
    est = meta["estimator"]
    kept, _ = v2.prepare_records(records, datetime.date.fromisoformat(meta["cutoff_date"]))
    buckets, _ = bbt.bucket_records(kept, est["class_offset"], None if est["goings"] == "all" else est["goings"])
    rows = []
    for (venue, surface, dist), items in sorted(buckets.items()):
        values = [value for value, _ in items]
        if len(values) < 3:
            continue
        median = statistics.median(values)
        sd = _robust_sd(values)
        if sd == 0:
            continue
        for value, record in items:
            z = (value - median) / sd
            if abs(z) > ROBUST_Z_OUTLIER:
                rows.append({"course": _key(venue, surface, dist), "date": record.get("date"),
                             "class": record.get("class"), "going": record.get("going"),
                             "win_time": record.get("win_time"), "normalized": round(value, 2),
                             "course_median": round(median, 2), "robust_z": round(z, 2)})
    rows.sort(key=lambda r: (-abs(r["robust_z"]), r["course"], str(r["date"])))
    return {"threshold_robust_z": ROBUST_Z_OUTLIER, "count": len(rows),
            "courses_with_outliers": len({r["course"] for r in rows}), "rows": rows}


def cross_course_trend(meta: dict[str, Any]) -> dict[str, Any]:
    """芝ダ別に「距離 → 基準タイム」を直線で近似し、大きく外れるコースを一覧化（場の差は正常にありうる）。"""
    out: dict[str, Any] = {"min_resid_sec": TREND_RESID_MIN_SEC, "robust_z": TREND_RESID_ROBUST_Z, "by_surface": {}}
    for surface in ("芝", "ダ"):
        rows = [(k, c["value"], int(k.split("/")[2])) for k, c in meta["courses"].items()
                if c["status"] == "ok" and k.split("/")[1] == surface]
        if len(rows) < 3:
            out["by_surface"][surface] = {"courses": len(rows), "flags": []}
            continue
        slope, intercept = statistics.linear_regression([d for _, _, d in rows], [v for _, v, _ in rows])
        resid = {k: v - (intercept + slope * d) for k, v, d in rows}
        sd = _robust_sd(list(resid.values())) or 1e-9
        flags = [{"course": k, "value": v, "fitted": round(intercept + slope * d, 2),
                  "resid_sec": round(resid[k], 2), "robust_z": round(resid[k] / sd, 2)}
                 for k, v, d in rows
                 if abs(resid[k]) > TREND_RESID_MIN_SEC and abs(resid[k] / sd) > TREND_RESID_ROBUST_Z]
        out["by_surface"][surface] = {
            "courses": len(rows), "sec_per_100m_slope": round(slope * 100, 3),
            "flags": sorted(flags, key=lambda r: -abs(r["robust_z"])),
        }
    return out


def audit(lookup: dict[str, Any], meta: dict[str, Any], records: list[dict[str, Any]],
          champion: dict[str, Any], raw_dir: Path) -> dict[str, Any]:
    courses = meta["courses"]
    ok = {k: c for k, c in courses.items() if c["status"] == "ok"}
    cov = meta["coverage"]
    turf_catalog = [_key(v, "芝", d) for v, ss in bbt.COURSES.items() for d in ss.get("芝", [])]
    dirt_catalog = [_key(v, "ダ", d) for v, ss in bbt.COURSES.items() for d in ss.get("ダ", [])]

    def course_row(key: str) -> dict[str, Any]:
        c = courses.get(key)
        if c is None:
            return {"course": key, "status": "no_records"}
        iqr = None if c["p25"] is None else round(c["p75"] - c["p25"], 3)
        return {"course": key, **{k: c[k] for k in ("status", "value", "n", "median", "mad", "p25", "p75",
                                                   "date_min", "date_max")},
                "iqr": iqr, "class_counts": c["class_counts"], "going_counts": c["going_counts"]}

    wide = []
    for key, c in ok.items():
        km = int(key.split("/")[2]) / 1000
        iqr_per_km = (c["p75"] - c["p25"]) / km
        if iqr_per_km > WIDE_IQR_SEC_PER_1000M:
            wide.append({"course": key, "n": c["n"], "iqr": round(c["p75"] - c["p25"], 2),
                         "iqr_per_1000m": round(iqr_per_km, 2), "mad": c["mad"]})

    v1 = _lookup_items(champion)
    v2_items = _lookup_items(lookup)
    diffs = [{"course": k, "v1": v1[k], "v2": v2_items[k], "diff_sec": round(v2_items[k] - v1[k], 2),
              "n_v2": courses[k]["n"]}
             for k in sorted(set(v1) & set(v2_items))]

    report = v2.coverage_report(raw_dir, {"champion_v1": champion, "candidate_v2": lookup})
    races = {r["race_id"]: r for r in report["races"]}

    return {
        "schema": AUDIT_SCHEMA,
        "artifact_id": meta["artifact_id"],
        "cutoff_date": meta["cutoff_date"],
        "hashes": meta["hashes"],
        "note": "監査は値を変えない。フラグは要確認の目印で、閾値は事前に固定した定数。",
        "thresholds": {"thin_sample_n": THIN_SAMPLE_N, "robust_z_outlier": ROBUST_Z_OUTLIER,
                       "wide_iqr_sec_per_1000m": WIDE_IQR_SEC_PER_1000M,
                       "trend_resid_min_sec": TREND_RESID_MIN_SEC, "trend_resid_robust_z": TREND_RESID_ROBUST_Z,
                       "v1_diff_sec": V1_DIFF_SEC, "min_samples": meta["estimator"]["min_samples"]},
        "reproducibility": reproduce(records, meta, lookup),
        "cutoff_check": {
            "records_date_max": meta["source"]["period"]["date_max"],
            "within_cutoff": (meta["source"]["period"]["date_max"] or "") <= meta["cutoff_date"],
            "rejected": meta["counts"]["rejected"],
        },
        "counts": meta["counts"],
        "coverage": {
            "total": {"filled": cov["courses_filled"], "total": cov["courses_total"]},
            "turf": {"filled": sum(k in ok for k in turf_catalog), "total": len(turf_catalog),
                     "missing": [k for k in turf_catalog if k not in ok]},
            "dirt": {"filled": sum(k in ok for k in dirt_catalog), "total": len(dirt_catalog),
                     "missing": [k for k in dirt_catalog if k not in ok]},
        },
        "sample_size": {
            "n_min": min((c["n"] for c in ok.values()), default=None),
            "n_median": statistics.median([c["n"] for c in ok.values()]) if ok else None,
            "insufficient": sorted(({"course": k, "n": c["n"]} for k, c in courses.items()
                                    if c["status"] != "ok"), key=lambda r: (r["n"], r["course"])),
            "thin": sorted(({"course": k, "n": c["n"]} for k, c in ok.items() if c["n"] < THIN_SAMPLE_N),
                           key=lambda r: (r["n"], r["course"])),
        },
        "focus_courses": [course_row(k) for k in FOCUS_COURSES],
        "top_demand_courses": [course_row(r["course"]) | {"raw_runs": r["runs"]}
                               for r in _top_demand(raw_dir)],
        "outliers_within_course": within_course_outliers(records, meta),
        "wide_dispersion": {"threshold_iqr_sec_per_1000m": WIDE_IQR_SEC_PER_1000M,
                            "courses": sorted(wide, key=lambda r: -r["iqr_per_1000m"])},
        "cross_course_trend": cross_course_trend(meta),
        "v1_comparison": {"common_courses": len(diffs), "threshold_sec": V1_DIFF_SEC,
                          "flags": [d for d in diffs if abs(d["diff_sec"]) > V1_DIFF_SEC], "all": diffs},
        "raw_coverage": {"demand": report["demand"], "tables": report["tables"]},
        "speed_guard": {
            "guard": report["guard"],
            "focus_races": {rid: races.get(rid) for rid in FOCUS_RACES},
            "all_races": report["races"],
        },
    }


def _top_demand(raw_dir: Path, n: int = 12) -> list[dict[str, Any]]:
    """raw の需要が多いコース上位（空の表に対する「未充足」＝需要そのもの）。"""
    report = v2.coverage_report(raw_dir, {"empty": {}}, include_ceiling=False, top_n=n)
    return report["tables"]["empty"]["top_missing"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="基準タイム v2 artifact を監査する（値は変えない）")
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--artifact-dir", type=Path, default=v2.OUTPUT_DIR)
    parser.add_argument("--source", type=Path, required=True, help="生レコードの JSON")
    parser.add_argument("--raw-dir", type=Path, default=bbt.RAW_DIR)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    artifact_id = v2.validate_artifact_id(args.artifact_id)
    lookup = json.loads((args.artifact_dir / f"{artifact_id}.json").read_text(encoding="utf-8"))
    meta = json.loads((args.artifact_dir / f"{artifact_id}.meta.json").read_text(encoding="utf-8"))
    records = bbt.load_records(args.source)
    result = audit(lookup, meta, records, speed_index.load_base_times(), args.raw_dir)

    output = args.output or v2.REPORT_DIR / f"{artifact_id}.audit.json"
    v2.ensure_writable(output, v2.REPORT_ALLOWED_DIRS)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    rep, cov = result["reproducibility"], result["coverage"]
    print(f"再現性: {rep}")
    print(f"芝 {cov['turf']['filled']}/{cov['turf']['total']}・ダ {cov['dirt']['filled']}/{cov['dirt']['total']}")
    print(f"監査結果: {output}")
    return 0 if all(rep.values()) and result["cutoff_check"]["within_cutoff"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
