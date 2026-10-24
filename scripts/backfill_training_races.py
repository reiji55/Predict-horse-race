"""
過去レースの調教ページ（無料で見られる「調教評価」と記号）を集める。予想ロジックはまだ使わない。

  保存先：data/training/training_eval.csv（race_id, horse_ref, grade, critic, fetched_at）
  レースごとの結果：data/training/backfill_status.csv（血統の取り込みと同じ決まり）
    done          表の馬の行が全部読めて、全頭に記号か評価がある
    retry         取得失敗・表なし・読めない行・記号も評価も無い馬がいる（attempts 回目）
    unavailable   3回やっても表が無い／取得できない／1頭も取れない
    partial_final 3回やっても一部の馬が欠けたまま（取れた馬は保存済み）
  最初の5レースで1頭も取れない、または1頭も取れないレースが10回続いたら、その分は記録せず止める。
  --sample：確認用。指定したレースを取り、記号の内訳などを表示するだけ（保存しない）

使い方：
  python -m scripts.backfill_training_races --race-ids data/pedigree/backfill_race_ids_11R.txt --limit 200
  python -m scripts.backfill_training_races --sample 201406040811 202609040811
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import sys
from pathlib import Path

from scraper.fetchers import training

logger = logging.getLogger("scripts.backfill_training_races")
ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "data" / "training" / "training_eval.csv"
STATUS = ROOT / "data" / "training" / "backfill_status.csv"
FIELDS = ["race_id", "horse_ref", "grade", "critic", "fetched_at"]
STATUS_FIELDS = ["race_id", "status", "n_runners", "n_complete", "n_missing", "attempts", "last_tried", "note"]
FINAL = {"done", "unavailable", "partial_final"}
MAX_ATTEMPTS, FIRST_CHECK, STREAK_LIMIT = 3, 5, 10
JST = datetime.timezone(datetime.timedelta(hours=9))


def _load(path: Path, key) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {key(r): r for r in csv.DictReader(f)}


def _save(rows: dict, path: Path, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for k in sorted(rows, reverse=True):
            w.writerow({c: rows[k].get(c, "") or "" for c in fields})
    tmp.replace(path)


def classify(by_horse: dict, n_rows: int) -> tuple[str, int, int]:
    ok = sum(1 for v in by_horse.values() if v.get("grade") or v.get("critic"))
    n = max(n_rows, len(by_horse))
    if n == 0:
        return "no_table", 0, 0
    if len(by_horse) < n:
        return "runner_id_missing", n, ok
    if ok == n:
        return "done", n, ok
    return ("no_eval" if ok == 0 else "partial"), n, ok


def next_status(outcome: str, attempts: int, ok: int) -> str:
    if outcome == "done":
        return "done"
    if attempts < MAX_ATTEMPTS:
        return "retry"
    return "partial_final" if ok > 0 else "unavailable"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="過去レースの調教評価を集める")
    ap.add_argument("--race-ids", help="レースIDを1行に1つ書いたファイル")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--store", default=str(STORE))
    ap.add_argument("--status", default=str(STATUS))
    ap.add_argument("--sample", nargs="*", help="確認用：レースIDを並べる（保存しない）")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    if args.sample is not None:
        report = {}
        for rid in args.sample:
            try:
                by_horse, n_rows = training.parse_oikiri_html(training.fetch_oikiri_html(rid))
                report[rid] = training.summarize(by_horse, n_rows)
            except RuntimeError as e:
                report[rid] = {"error": str(e)}
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    store_path, status_path = Path(args.store), Path(args.status)
    store = _load(store_path, lambda r: (r["race_id"], r["horse_ref"]))
    status = _load(status_path, lambda r: r["race_id"])
    ids = [x.strip() for x in Path(args.race_ids).read_text(encoding="utf-8").splitlines() if x.strip()]
    todo = [x for x in ids if status.get(x, {}).get("status") not in FINAL][: args.limit]
    logger.info("対象 %d レース、今回 %d レース（目安 %.0f 分）", len(ids), len(todo), len(todo) * 4 / 60)
    pending: dict[str, dict] = {}
    streak: list[str] = []
    total_ok, stopped = 0, False

    def flush(hold=()):
        _save(store, store_path, FIELDS)
        for rid in [r for r in pending if r not in hold]:
            status[rid] = pending.pop(rid)
        _save(status, status_path, STATUS_FIELDS)

    for n, rid in enumerate(todo, 1):
        attempts = int(status.get(rid, {}).get("attempts") or 0) + 1
        try:
            by_horse, n_rows = training.parse_oikiri_html(training.fetch_oikiri_html(rid))
            outcome, n_run, n_ok = classify(by_horse, n_rows)
        except RuntimeError:
            by_horse, outcome, n_run, n_ok = {}, "fetch_error", 0, 0
        now = datetime.datetime.now(JST).isoformat(timespec="seconds")
        for ref, v in by_horse.items():
            if v.get("grade") or v.get("critic"):
                store[(rid, ref)] = {"race_id": rid, "horse_ref": ref, "grade": v.get("grade"),
                                     "critic": v.get("critic"), "fetched_at": now}
        total_ok += n_ok
        st = next_status(outcome, attempts, n_ok)
        pending[rid] = {"race_id": rid, "status": st, "n_runners": n_run, "n_complete": n_ok,
                        "n_missing": n_run - n_ok, "attempts": attempts, "last_tried": now,
                        "note": "" if outcome == "done" else outcome}
        streak = streak + [rid] if n_ok == 0 else []
        if n == FIRST_CHECK and total_ok == 0:
            logger.error("最初の%dレースで1頭も取れません。記録せず止めます", FIRST_CHECK)
            pending.clear()
            stopped = True
            break
        if len(streak) >= STREAK_LIMIT:
            logger.error("1頭も取れないレースが%d回続きました。その分は記録せず止めます", STREAK_LIMIT)
            for r in streak:
                pending.pop(r, None)
            stopped = True
            break
        if n % 20 == 0:
            flush(hold=streak)
    flush()
    logger.info("完了：保存済み %d 行", len(store))
    return 1 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
