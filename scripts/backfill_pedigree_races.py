"""
過去レースの出馬表「過去5走」ページ（C2）から、出走馬の血統（父・母・母父）をまとめて集める。

1ページで出走馬全員（〜18頭）の血統が取れるので、馬ごとの血統表を取るより少ないアクセスで済む。
`race.netkeiba.com` は GitHub Actions からも取れている経路（c2_shutuba_past の冒頭）なので、
手動 workflow（.github/workflows/manual_pedigree_backfill.yml）で少しずつ回せる。

**C2の並びを実ページで確かめるまでは動かない**（pedigree.c2_layout_verified。docs/PEDIGREE.md）。

レースごとの結果は data/pedigree/backfill_status.csv に残す（1レース1行、上書き更新）：

  status        意味                                                       次回
  done          出走馬全員の父・母・母父がそろった                           取りに行かない
  retry         取得失敗・表が無い・一部の馬が欠けた・馬IDが読めない行がある（attempts 回目）  もう一度取りに行く
  unavailable   MAX_ATTEMPTS 回やっても表が無い／取得できない／1頭も取れない   取りに行かない
  partial_final MAX_ATTEMPTS 回やっても一部の馬が欠けたまま（そろった馬は保存済み）  取りに行かない

出走馬数（n_runners）は、パーサーが読めた馬の数ではなく、HTMLの馬の行の数で数える
（c2_shutuba_past.runner_row_stats）。馬IDが読めずに飛ばされた行があれば done にしない。
unavailable / partial_final を取り直したいときは、backfill_status.csv からそのレースの行を消す。

止まる条件（そのレースの結果は記録しない＝あとで取り直せる）：
- 今回の最初の5レースで、そろった血統が1頭も無い
- そのあと、そろった血統が1頭も無いレースが STREAK_LIMIT 回続いた

1ページあたり約4秒（scraper.common.http の間隔をそのまま守る）。200レースで約15分。

使い方：
  python -m scripts.backfill_pedigree_races --race-ids data/pedigree/backfill_race_ids_11R.txt --limit 200
"""
from __future__ import annotations

import argparse
import csv
import datetime
import logging
import sys
from pathlib import Path

from scraper.fetchers import c2_shutuba_past, pedigree

logger = logging.getLogger("scripts.backfill_pedigree_races")
STATUS_PATH = pedigree.PEDIGREE_DIR / "backfill_status.csv"
STATUS_FIELDS = ["race_id", "status", "n_runners", "n_complete", "n_missing", "attempts", "last_tried", "note"]
FINAL = {"done", "unavailable", "partial_final"}
MAX_ATTEMPTS = 3
FIRST_CHECK = 5
STREAK_LIMIT = 10


def load_status(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {row["race_id"]: row for row in csv.DictReader(f)}


def save_status(status: dict[str, dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STATUS_FIELDS)
        w.writeheader()
        for rid in sorted(status, reverse=True):
            w.writerow({k: status[rid].get(k, "") for k in STATUS_FIELDS})
    tmp.replace(path)


def classify(by_horse: dict[str, dict], rows: dict[str, int] | None = None) -> tuple[str, int, int]:
    """
    1レースの取得結果を (outcome, 出走馬数, そろった頭数) にする。
    outcome は done / no_table / no_pedigree / partial / runner_id_missing。

    出走馬数は、パーサーが読めた馬の数ではなく、HTMLの馬の行の数（c2_shutuba_past.runner_row_stats）。
    馬IDが読めずに飛ばされた行や、同じ馬IDの重複があれば、残りがそろっていても done にしない。
    """
    complete = sum(1 for v in by_horse.values() if pedigree.is_complete(v.get("pedigree")))
    rows = rows or {"rows": len(by_horse), "id_missing": 0, "duplicates": 0}
    n = max(rows["rows"], len(by_horse))
    if n == 0:
        return "no_table", 0, 0
    if rows["id_missing"] or rows["duplicates"] or len(by_horse) < n:
        return "runner_id_missing", n, complete
    if complete == n:
        return "done", n, complete
    if complete == 0:
        return "no_pedigree", n, 0
    return "partial", n, complete


def next_status(outcome: str, attempts: int, complete: int = 0) -> str:
    if outcome == "done":
        return "done"
    if attempts < MAX_ATTEMPTS:
        return "retry"
    if outcome in ("partial", "runner_id_missing") and complete > 0:
        return "partial_final"
    return "unavailable"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="過去レースの過去5走ページから血統を集める")
    ap.add_argument("--race-ids", required=True, help="netkeibaのレースID（12桁）を1行に1つ書いたファイル")
    ap.add_argument("--limit", type=int, default=200, help="今回取りに行く最大レース数（既定200＝約15分）")
    ap.add_argument("--store", default=str(pedigree.STORE_PATH), help="保存先CSV")
    ap.add_argument("--status", default=str(STATUS_PATH), help="レースごとの取得結果CSV")
    ap.add_argument("--conflicts", default=str(pedigree.CONFLICTS_PATH), help="食い違いの記録CSV")
    ap.add_argument("--layout-check", default=str(pedigree.LAYOUT_CHECK_PATH), help="C2の並びの確認結果")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    if not pedigree.c2_layout_verified(Path(args.layout_check)):
        logger.error("C2の並びが実ページで未確認です。先に manual_pedigree_verify を実行してください（docs/PEDIGREE.md）")
        return 2

    store_path, status_path, conflicts_path = Path(args.store), Path(args.status), Path(args.conflicts)
    status = load_status(status_path)
    ids = [x.strip() for x in Path(args.race_ids).read_text(encoding="utf-8").splitlines() if x.strip()]
    todo = [x for x in ids if status.get(x, {}).get("status") not in FINAL][: args.limit]
    store = pedigree.load_store(store_path)
    logger.info("対象 %d レースのうち終了 %d、今回 %d レース（目安 %.0f 分）", len(ids),
                sum(1 for x in ids if status.get(x, {}).get("status") in FINAL), len(todo), len(todo) * 4 / 60)

    pending: dict[str, dict] = {}      # 今回の結果（保存前）
    streak: list[str] = []             # そろった血統が0頭のレースの連続
    tally = {"done": 0, "retry": 0, "unavailable": 0, "partial_final": 0}
    rec_counts = {"added": 0, "incomplete": 0, "same": 0, "conflict": 0, "skipped": 0}
    complete_total, stopped = 0, False

    def flush(hold: list[str] = ()) -> None:
        """血統を先に保存してから、レースの結果を書く。hold のレースは書かずに持ち越す。"""
        pedigree.save_store(store, store_path)
        for rid in [r for r in pending if r not in hold]:
            status[rid] = pending.pop(rid)
        save_status(status, status_path)

    for n, race_id in enumerate(todo, 1):
        attempts = int(status.get(race_id, {}).get("attempts") or 0) + 1
        try:
            html = c2_shutuba_past.fetch_shutuba_past_html(race_id)
            by_horse = c2_shutuba_past.parse_shutuba_past_html(html, read_pedigree=True)
            outcome, n_run, n_ok = classify(by_horse, c2_shutuba_past.runner_row_stats(html))
        except RuntimeError:
            by_horse, outcome, n_run, n_ok = {}, "fetch_error", 0, 0
        for ref, found in by_horse.items():
            rec_counts[pedigree.add_record(store, ref, found.get("pedigree"), "shutuba_past_backfill",
                                           conflicts_path)] += 1
        complete_total += n_ok
        st = next_status(outcome, attempts, n_ok)
        tally[st] += 1
        pending[race_id] = {"race_id": race_id, "status": st, "n_runners": n_run, "n_complete": n_ok,
                            "n_missing": n_run - n_ok, "attempts": attempts,
                            "last_tried": datetime.datetime.now(pedigree.JST).isoformat(timespec="seconds"),
                            "note": "" if outcome == "done" else outcome}
        streak = streak + [race_id] if n_ok == 0 else []
        logger.info("%s：%s（%d/%d 頭そろう、%d回目）", race_id, st, n_ok, n_run, attempts)

        if n == FIRST_CHECK and complete_total == 0:
            logger.error("最初の%dレースで、そろった血統が1頭もありません。結果は記録せず止めます（並びの変化を疑う）",
                         FIRST_CHECK)
            pending.clear()
            stopped = True
            break
        if len(streak) >= STREAK_LIMIT:
            logger.error("そろった血統が0頭のレースが%d回続きました。その%dレースは記録せず止めます",
                         STREAK_LIMIT, STREAK_LIMIT)
            for rid in streak:
                pending.pop(rid, None)
            stopped = True
            break
        if n % 20 == 0:
            flush(hold=streak)    # 0頭が続いている途中のレースは、続きが分かるまで書かない

    flush()
    logger.info("完了：レース %s ／ 馬 %s ／ 保存済み合計 %d 頭", tally, rec_counts, len(store))
    return 1 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
