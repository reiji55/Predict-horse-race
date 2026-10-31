"""全JRAレースの発走前・締め切り直前・確定オッズの記録（odds-live-v1、observe-only）。

edge-loop（docs/research/EDGE_LOOP.md §8）と lab（lab/research-design-v0 の DATA_ACQUISITION_v0.md §6）で共通に使う記録。
予想・正式成績・UI・snapshot・raw には使わない。記録は main ではなく専用ブランチ odds-live に書く（workflow が用意する）。

1レースにつき3回、4券種（単勝・複勝＝type 1、3連複＝7、ワイド＝5、馬連＝4）を取る。障害戦は取らない（lab の D3）。

    pre    発走30分前（lab の判断時刻）。取得元のオッズはおよそ1時間おきにしか新しくならない（EDGE_LOOP.md §6.2）ので、
           中身は発走の約65分前の値になることが多い。60分前に取っても同じ値になるので取らない。
    close  発走3分前（締め切り直前の更新）。券種ごとに、取得を終えた時刻が発走時刻を過ぎていたら保存しない（fail-closed）。
    final  発走25分後（確定オッズ）。発走後の値として別に残し、発走前の入力には使わない。

1観測1ファイル（{日付}/{race_key}/{phase}.json.gz）で、上書きしない。取り逃した回も、理由を書いたファイルを残す。
取得元のレスポンス本文・URL・リクエスト固有の値は残さない（数値と時刻だけ）。取得の間隔は scraper/common/http.py に従う。

1回の実行は「次の予定まで待ち、その時刻に来ている予定を全部取る」だけをして終わる。workflow がこれを繰り返し、
そのたびにコミットする。終了コード：0＝予定が残っている、1＝レース一覧を取れなかった（次の実行で取り直す）、
3＝その日の予定が終わった、4＝時間切れ（workflow が次を起動する）。
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from scraper.common import constants
from scraper.fetchers import a_race_list, b2_odds

logger = logging.getLogger("scraper.capture_odds_live")

SCHEMA = "odds-live-v1"
JST = datetime.timezone(datetime.timedelta(hours=9))
PHASES = (("pre", -30), ("close", -3), ("final", 25))  # 発走時刻からの分
PRE_LATEST = datetime.timedelta(minutes=6)    # pre はこれより発走に近づいたら取らない（close と同じ値になる）
CLOSE_LATEST = datetime.timedelta(minutes=1)  # close は発走1分前を過ぎて始めない
DUE_WINDOW = datetime.timedelta(seconds=60)
# final（確定オッズ）は急がない。pre・close がこの時間内に来るなら、final を後に回してそちらを待つ
FINAL_GUARD = datetime.timedelta(seconds=150)
# 取る順：締め切り直前に時間が足りなくなっても、単勝と3連複を先に確保する
MARKETS = ("1", "7", "5", "4")
MARKET_NAMES = {"1": ("win", "place"), "7": ("sanrenpuku",), "5": ("wide",), "4": ("umaren",)}
COMBO_NAMES = {"7": "3連複", "5": "ワイド", "4": "馬連"}

EXIT_MORE, EXIT_ERROR, EXIT_DONE, EXIT_TIMEOUT = 0, 1, 3, 4


@dataclass(frozen=True)
class Event:
    when: datetime.datetime
    race: dict[str, Any]
    phase: str

    @property
    def post_at(self) -> datetime.datetime:
        return post_datetime(self.race)


def post_datetime(race: dict[str, Any]) -> datetime.datetime:
    hh, mm = race["post_time"].split(":")
    d = datetime.date.fromisoformat(race["date"])
    return datetime.datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=JST)


def _now() -> datetime.datetime:
    return datetime.datetime.now(JST)


def _iso(t: datetime.datetime) -> str:
    return t.astimezone(JST).isoformat(timespec="seconds")


# ------------------------------------------------------------------ レース一覧と予定

def race_rows(entries: list[a_race_list.RaceListEntry]) -> list[dict[str, Any]]:
    """レース一覧から記録に要る事実だけを残す（レース名などの文字列は残さない）。障害戦と発走時刻の無いレースは外す。"""
    rows = []
    for e in entries:
        if e.surface == "障" or not e.post_time or e.venue not in constants.VENUE_ROMAJI:
            continue
        rows.append({
            "race_key": constants.race_id(e.date, e.venue, e.race_no),
            "source_ref": e.source_ref,
            "date": e.date,
            "venue": e.venue,
            "race_no": e.race_no,
            "post_time": e.post_time.strip(),
            "surface": e.surface,
            "dist": e.dist,
            "heads": e.heads,
            "grade": e.grade,
        })
    return sorted(rows, key=lambda r: (r["post_time"], r["race_key"]))


class RaceListUnavailable(RuntimeError):
    """レース一覧を取れなかった。races.json は書かず、次の実行で取り直す。"""


def load_races(out_dir: Path, date_str: str,
               fetch_list: Callable[[str], list[a_race_list.RaceListEntry]]) -> list[dict[str, Any]]:
    """その日の最初の実行でレース一覧を取り、races.json に残す。2回目以降はそれを読む（取り直さない）。

    取れなかったとき（通信の失敗、開催の無い日、ページの形が変わって1件も読めないとき）は races.json を書かない。
    空の一覧を残すと、その日の後の実行が全部「予定なし」で終わってしまうため。
    """
    path = out_dir / date_str.replace("-", "") / "races.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["races"]
    try:
        rows = race_rows(fetch_list(date_str))
    except RuntimeError as exc:
        raise RaceListUnavailable(type(exc).__name__) from exc
    if not rows:
        raise RaceListUnavailable("no_races_parsed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": SCHEMA, "date": date_str, "races": rows},
                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return rows


def schedule(races: list[dict[str, Any]]) -> list[Event]:
    events = [Event(post_datetime(r) + datetime.timedelta(minutes=m), r, phase)
              for r in races for phase, m in PHASES]
    return sorted(events, key=lambda e: (e.when, e.race["race_key"], e.phase))


def record_path(out_dir: Path, event: Event) -> Path:
    r = event.race
    return out_dir / r["date"].replace("-", "") / r["race_key"] / f"{event.phase}.json.gz"


# ------------------------------------------------------------------ 1回の観測

def _market_rows(type_code: str, body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """本体JSONから券種ごとの数値の表を作る。単勝・複勝は type 1 の1回の応答に両方入っている。"""
    if type_code == "1":
        by_num = b2_odds.extract_win_place_odds(body)
        win = {str(n): v["win_odds"] for n, v in sorted(by_num.items()) if v.get("win_odds") is not None}
        pop = {str(n): v["popularity"] for n, v in sorted(by_num.items()) if v.get("popularity") is not None}
        place = {str(n): v["place_odds"] for n, v in sorted(by_num.items()) if v.get("place_odds")}
        return {"win": {"odds": win, "popularity": pop}, "place": {"odds": place}}
    table = b2_odds.extract_combo_odds(body).get(COMBO_NAMES[type_code]) or {}
    return {MARKET_NAMES[type_code][0]: {"odds": dict(sorted(table.items()))}}


def observe(event: Event, clock: Callable[[], datetime.datetime],
            fetch_body: Callable[[str, str], dict[str, Any]]) -> dict[str, Any]:
    """1つの予定（レース×回）を取る。発走時刻より後に取得を終えた券種は、close では残さない。"""
    r = event.race
    record: dict[str, Any] = {
        "schema": SCHEMA, "race_key": r["race_key"], "source_ref": r["source_ref"], "date": r["date"],
        "venue": r["venue"], "race_no": r["race_no"], "post_time": r["post_time"], "surface": r["surface"],
        "dist": r["dist"], "heads": r["heads"], "grade": r["grade"],
        "phase": event.phase, "scheduled_at": _iso(event.when), "markets": {},
        "used_for_prediction": False,
    }
    now = clock()
    if (event.phase == "pre" and now >= event.post_at - PRE_LATEST) or \
            (event.phase == "close" and now >= event.post_at - CLOSE_LATEST):
        record.update(status="missed", reason="started_too_late", checked_at=_iso(now))
        return record
    for type_code in MARKETS:
        try:
            body = fetch_body(r["source_ref"], type_code)
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            observed = clock()
            for name in MARKET_NAMES[type_code]:
                record["markets"][name] = {"status": "fetch_failed", "error": type(exc).__name__,
                                           "observed_at": _iso(observed), "source_time": None, "rows": 0}
            continue
        observed = clock()
        after_post = event.phase == "close" and observed >= event.post_at
        for name, table in _market_rows(type_code, body).items():
            rows = len(table["odds"])
            if after_post:  # 締め切り直前の値として残せない（fail-closed）
                record["markets"][name] = {"status": "after_post", "observed_at": _iso(observed),
                                           "source_time": None, "rows": 0}
                continue
            record["markets"][name] = {"status": "ok" if rows else "empty", "observed_at": _iso(observed),
                                       "source_time": body.get("official_datetime") or None, "rows": rows, **table}
    statuses = {m["status"] for m in record["markets"].values()}
    record["status"] = "ok" if statuses == {"ok"} else "partial" if "ok" in statuses else "failed"
    return record


def write_record(path: Path, record: dict[str, Any]) -> None:
    """上書きしない。gzip の時刻は 0 にして、同じ中身なら同じバイト列にする。"""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(gzip.compress(data, mtime=0))


# ------------------------------------------------------------------ 1回の実行

def run_once(date_str: str, out_dir: Path, deadline: datetime.datetime, *,
             clock: Callable[[], datetime.datetime] = _now,
             sleep: Callable[[float], None] = time.sleep,
             fetch_body: Callable[[str, str], dict[str, Any]] = b2_odds.fetch_type_body,
             fetch_list: Callable[[str], list[a_race_list.RaceListEntry]] = a_race_list.fetch_race_list) -> int:
    try:
        races = load_races(out_dir, date_str, fetch_list)
    except RaceListUnavailable as exc:
        logger.error("%s のレース一覧を取れませんでした（%s）。今回は記録しません", date_str, exc)
        return EXIT_ERROR
    pending = [e for e in schedule(races) if not record_path(out_dir, e).exists()]
    if not pending:
        return EXIT_DONE
    wake = next_wake(pending, clock())
    if wake > clock():
        if wake >= deadline:
            return EXIT_TIMEOUT
        sleep((wake - clock()).total_seconds())
    now = clock()
    due = [e for e in pending if _is_due(e, now)]
    for event in [e for e in due if e.phase != "final"] + [e for e in due if e.phase == "final"]:
        if event.phase == "final" and _critical_soon(pending, out_dir, clock()):
            break  # 確定オッズは後でも取れる。締め切り直前の取得を先にする
        record = observe(event, clock, fetch_body)
        write_record(record_path(out_dir, event), record)
        logger.info("%s %s: %s", event.race["race_key"], event.phase, record["status"])
    if all(record_path(out_dir, e).exists() for e in pending):
        return EXIT_DONE
    return EXIT_MORE if clock() < deadline else EXIT_TIMEOUT


def _is_due(event: Event, now: datetime.datetime) -> bool:
    """close は予定の時刻ちょうどから（早く取ると締め切り直前の更新の前の値になる）。他は1分前から。"""
    return event.when <= (now if event.phase == "close" else now + DUE_WINDOW)


def next_wake(pending: list[Event], now: datetime.datetime) -> datetime.datetime:
    """次に起きる時刻。来ている予定が final だけで、pre・close がすぐ来るなら、そちらの時刻まで待つ。"""
    due = [e for e in pending if _is_due(e, now)]
    critical = [e for e in pending if e.phase != "final"]
    if due:
        soon = [e for e in critical if e.when - now < FINAL_GUARD]
        if all(e.phase == "final" for e in due) and soon:
            return min(e.when for e in soon)
        return now
    # 早めには起きない（close は発走3分前ちょうどから取り、締め切り直前の更新を取りこぼさない）
    return min(e.when for e in pending)


def _critical_soon(pending: list[Event], out_dir: Path, now: datetime.datetime) -> bool:
    return any(e.phase != "final" and not record_path(out_dir, e).exists() and e.when - now < FINAL_GUARD
               for e in pending)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全JRAレースのオッズ記録（odds-live-v1）")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD（JST）")
    ap.add_argument("--out", type=Path, required=True, help="odds-live ブランチを置いたディレクトリ")
    ap.add_argument("--deadline", required=True, help="この時刻（ISO 8601）より後の予定は次の実行に回す")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    deadline = datetime.datetime.fromisoformat(args.deadline.replace("Z", "+00:00"))
    return run_once(args.date, args.out, deadline)


if __name__ == "__main__":
    sys.exit(main())
