"""全JRAレースの発走前・締め切り直前・発走後のオッズの記録（odds-live-v1、observe-only）。

edge-loop（docs/research/EDGE_LOOP.md §8）と lab（lab/research-design-v0 の DATA_ACQUISITION_v0.md §6）で共通に使う記録。
予想・正式成績・UI・snapshot・raw には使わない。記録は main ではなく専用ブランチ odds-live に書く（workflow が用意する）。

1レースにつき4回、4券種（単勝・複勝＝type 1、3連複＝7、ワイド＝5、馬連＝4）を取る。障害戦は取らない（lab の D3）。

    pre    発走30分前（lab の判断時刻）。取得元のオッズはおよそ1時間おきにしか新しくならない（EDGE_LOOP.md §6.2）ので、
           中身は発走の約65分前の値になることが多い。60分前に取っても同じ値になるので取らない。
    close  発走3分前（締め切り直前の更新）。予定の時刻より早くは取らない。
    post1  発走25分後。発走後の観測で、これだけでは確定オッズとみなさない。
    post2  発走60分後。post1 と中身がそろい、両方の時刻が締め切りの後なら、確定オッズと確かめられたとする（final_check）。

券種ごとに、取得元が返した時刻（source_time、JST とみなす）と取得を終えた時刻（observed_at）を検証する。
次のどれかに当たる券種は ok にしない：source_time が無い・読めない、observed_at より未来（時計のずれは60秒まで許す）、
pre・close で取得を終えたのが発走後、pre・close で source_time が発走後。下流は status が ok の券種だけを使い、
確定オッズとしては final_check.verified が true の post2 だけを使う。

1観測1ファイル（{日付}/{race_key}/{phase}.json.gz）で、上書きしない。取り逃した回も、理由を書いたファイルを残す。
レース一覧は1時間ごとに取り直し、取った版を {日付}/races/{時刻}.json に残す（上書きしない）。予定は全部の版の和で、
発走時刻は最新の版を使う。取得元のレスポンス本文・URL・リクエスト固有の値は残さない（数値と時刻だけ）。
取得の間隔は scraper/common/http.py に従う。

1回の実行は「次の予定まで待ち、その時刻に来ている予定を取る」だけをして終わる。workflow がこれを繰り返し、
そのたびにコミットする。締め切り直前（close）を最優先にし、close がすぐ来るときは他の取得を始めない。
終了コード：0＝予定が残っている、1＝レース一覧を一度も取れていない（次の実行で取り直す）、
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
PHASES = (("pre", -30), ("close", -3), ("post1", 25), ("post2", 60))  # 発走時刻からの分
PRE_PHASES = ("pre", "close")
PRIORITY = {"close": 0, "pre": 1, "post1": 2, "post2": 2}
PRE_LATEST = datetime.timedelta(minutes=6)    # pre はこれより発走に近づいたら取らない（close と同じ値になる）
CLOSE_LATEST = datetime.timedelta(minutes=1)  # close は発走1分前を過ぎて始めない
CLOSE_GUARD = datetime.timedelta(seconds=120)  # close がこの時間内に来るなら、他の取得を始めない
DUE_WINDOW = datetime.timedelta(seconds=60)
SOURCE_SKEW = datetime.timedelta(seconds=60)   # 取得元と runner の時計のずれとして許す幅
SALES_CLOSE = datetime.timedelta(minutes=2)    # 締め切りは発走のおよそ2分前。確定オッズの時刻はこれより後のはず
RACE_LIST_REFRESH = datetime.timedelta(minutes=60)
RACE_LIST_RETRY = datetime.timedelta(minutes=10)  # 欠けている疑いのある一覧は、10分おきに取り直す
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


def parse_source_time(raw: Any) -> datetime.datetime | None:
    """取得元の official_datetime（"YYYY-MM-DD HH:MM:SS"、時差の表記なし）を JST の時刻にする。読めなければ None。"""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    except ValueError:
        return None


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


def list_check(entries: list[a_race_list.RaceListEntry]) -> dict[str, Any]:
    """一覧が欠けていないかの目安：場ごとに、レース番号が1から抜けなく並んでいるか（障害戦も含めて数える）。"""
    by_venue: dict[str, list[int]] = {}
    for e in entries:
        by_venue.setdefault(e.venue, []).append(e.race_no)
    contiguous = {v: sorted(n) == list(range(1, max(n) + 1)) for v, n in by_venue.items()}
    return {"status": "ok" if by_venue and all(contiguous.values()) else "partial_unverified",
            "races_by_venue": {v: len(n) for v, n in sorted(by_venue.items())}, "contiguous": contiguous}


def _race_dir(out_dir: Path, date_str: str) -> Path:
    return out_dir / date_str.replace("-", "") / "races"


def _list_versions(out_dir: Path, date_str: str) -> list[dict[str, Any]]:
    d = _race_dir(out_dir, date_str)
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.exists() else []


def load_races(out_dir: Path, date_str: str, now: datetime.datetime,
               fetch_list: Callable[[str], list[a_race_list.RaceListEntry]]) -> list[dict[str, Any]]:
    """予定に使うレース。最新の版が1時間より古いか、欠けている疑いがあって10分たてば取り直し、新しい版として残す。

    予定は全部の版の和（一度でも載ったレースは消さない）で、発走時刻は、そのレースが載っている最新の版のものを使う。
    取り直しに失敗しても、前の版があればそれを使う。一度も取れていなければ RaceListUnavailable。
    """
    versions = _list_versions(out_dir, date_str)
    latest = versions[-1] if versions else None
    age = now - datetime.datetime.fromisoformat(latest["fetched_at"]) if latest else None
    stale = latest is None or age >= RACE_LIST_REFRESH or \
        (latest["check"]["status"] != "ok" and age >= RACE_LIST_RETRY)
    if stale:
        try:
            entries = fetch_list(date_str)
        except RuntimeError as exc:
            if not versions:
                raise RaceListUnavailable(type(exc).__name__) from exc
            logger.warning("レース一覧を取り直せませんでした（%s）。前の版を使います", type(exc).__name__)
        else:
            rows = race_rows(entries)
            if rows:
                version = {"schema": SCHEMA, "date": date_str, "fetched_at": _iso(now),
                           "check": list_check(entries), "races": rows}
                path = _race_dir(out_dir, date_str) / f"{now.astimezone(JST):%H%M%S}.json"
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(version, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                versions.append(version)
            elif not versions:
                raise RaceListUnavailable("no_races_parsed")
    merged: dict[str, dict[str, Any]] = {}
    for v in versions:  # 古い版から順に上書きするので、最新の版の発走時刻が残る
        for r in v["races"]:
            merged[r["race_key"]] = r
    return sorted(merged.values(), key=lambda r: (r["post_time"], r["race_key"]))


class RaceListUnavailable(RuntimeError):
    """レース一覧を一度も取れていない。何も書かず、次の実行で取り直す。"""


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


def market_status(phase: str, observed: datetime.datetime, source: datetime.datetime | None,
                  post_at: datetime.datetime, rows: int) -> str:
    """1つの券種の観測を検証する。ok 以外は、下流の前向きの評価に使わない。"""
    if phase in PRE_PHASES and observed >= post_at:
        return "after_post"
    if source is None:
        return "source_time_unknown"
    if source > observed + SOURCE_SKEW:
        return "source_time_future"
    if phase in PRE_PHASES and source >= post_at:
        return "source_after_post"
    return "ok" if rows else "empty"


def observe(event: Event, clock: Callable[[], datetime.datetime],
            fetch_body: Callable[[str, str], dict[str, Any]]) -> dict[str, Any]:
    """1つの予定（レース×回）を取る。発走前の回で発走後に取得を終えた券種は、表を残さない（fail-closed）。"""
    r = event.race
    record: dict[str, Any] = {
        "schema": SCHEMA, "race_key": r["race_key"], "source_ref": r["source_ref"], "date": r["date"],
        "venue": r["venue"], "race_no": r["race_no"], "post_time": r["post_time"], "post_at": _iso(event.post_at),
        "surface": r["surface"], "dist": r["dist"], "heads": r["heads"], "grade": r["grade"],
        "phase": event.phase, "postrace": event.phase not in PRE_PHASES,
        "scheduled_at": _iso(event.when), "source_time_zone": "Asia/Tokyo（取得元の時刻に時差の表記が無いので JST とみなす）",
        "markets": {}, "used_for_prediction": False,
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
        raw_time = body.get("official_datetime")
        source = parse_source_time(raw_time)
        for name, table in _market_rows(type_code, body).items():
            status = market_status(event.phase, observed, source, event.post_at, len(table["odds"]))
            market = {"status": status, "observed_at": _iso(observed),
                      "source_time": raw_time if isinstance(raw_time, str) else None,
                      "source_time_jst": _iso(source) if source else None, "rows": len(table["odds"])}
            if status in ("after_post", "source_after_post") or (status == "source_time_future" and event.phase in PRE_PHASES):
                market["rows"] = 0  # 発走前の値として使えないものは、表そのものを残さない
            else:
                market.update(table)
            record["markets"][name] = market
    statuses = {m["status"] for m in record["markets"].values()}
    record["status"] = "ok" if statuses == {"ok"} else "partial" if "ok" in statuses else "failed"
    return record


def final_check(post1: dict[str, Any] | None, post2: dict[str, Any], post_at: datetime.datetime) -> dict[str, Any]:
    """確定オッズと確かめられるか。券種ごとに、post1 と post2 の両方が ok で、両方の時刻が締め切りの後で、表が同じこと。"""
    if post1 is None or post1.get("status") == "missed":
        return {"verified": False, "reason": "single_observation", "markets": {}}
    out: dict[str, Any] = {}
    for name, m2 in post2.get("markets", {}).items():
        m1 = post1.get("markets", {}).get(name) or {}
        if m1.get("status") != "ok" or m2.get("status") != "ok":
            out[name] = {"verified": False, "reason": "not_ok"}
            continue
        t1, t2 = parse_source_time(m1.get("source_time")), parse_source_time(m2.get("source_time"))
        if t1 is None or t2 is None or t1 < post_at - SALES_CLOSE or t2 < post_at - SALES_CLOSE:
            out[name] = {"verified": False, "reason": "source_before_close"}  # 締め切り前の古い版は確定にしない
        elif m1.get("odds") != m2.get("odds"):
            out[name] = {"verified": False, "reason": "changed_between_observations"}
        else:
            out[name] = {"verified": True, "reason": "same_after_close"}
    verified = bool(out) and all(v["verified"] for v in out.values())
    return {"verified": verified, "reason": "all_markets_verified" if verified else "some_markets_unverified",
            "markets": out}


def _read_record(path: Path) -> dict[str, Any] | None:
    return json.loads(gzip.decompress(path.read_bytes())) if path.exists() else None


def write_record(path: Path, record: dict[str, Any]) -> None:
    """上書きしない。gzip の時刻は 0 にして、同じ中身なら同じバイト列にする。"""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(gzip.compress(data, mtime=0))


# ------------------------------------------------------------------ 1回の実行

def _is_due(event: Event, now: datetime.datetime) -> bool:
    """close は予定の時刻ちょうどから（早く取ると締め切り直前の更新の前の値になる）。他は1分前から。"""
    return event.when <= (now if event.phase == "close" else now + DUE_WINDOW)


def _close_soon(pending: list[Event], out_dir: Path, now: datetime.datetime) -> list[Event]:
    return [e for e in pending if e.phase == "close" and not record_path(out_dir, e).exists()
            and e.when - now < CLOSE_GUARD]


def next_wake(pending: list[Event], out_dir: Path, now: datetime.datetime) -> datetime.datetime:
    """次に起きる時刻。来ている予定が close 以外だけで、close がすぐ来るなら、close の時刻まで待つ。"""
    due = [e for e in pending if _is_due(e, now)]
    if due:
        soon = _close_soon(pending, out_dir, now)
        if all(e.phase != "close" for e in due) and soon:
            return min(e.when for e in soon)
        return now
    return min(e.when for e in pending)  # 早めには起きない


def run_once(date_str: str, out_dir: Path, deadline: datetime.datetime, *,
             clock: Callable[[], datetime.datetime] = _now,
             sleep: Callable[[float], None] = time.sleep,
             fetch_body: Callable[[str, str], dict[str, Any]] = b2_odds.fetch_type_body,
             fetch_list: Callable[[str], list[a_race_list.RaceListEntry]] = a_race_list.fetch_race_list) -> int:
    try:
        races = load_races(out_dir, date_str, clock(), fetch_list)
    except RaceListUnavailable as exc:
        logger.error("%s のレース一覧を取れませんでした（%s）。今回は記録しません", date_str, exc)
        return EXIT_ERROR
    pending = [e for e in schedule(races) if not record_path(out_dir, e).exists()]
    if not pending:
        return EXIT_DONE
    wake = next_wake(pending, out_dir, clock())
    if wake > clock():
        if wake >= deadline:
            return EXIT_TIMEOUT
        sleep((wake - clock()).total_seconds())
    now = clock()
    due = sorted((e for e in pending if _is_due(e, now)), key=lambda e: (PRIORITY[e.phase], e.when))
    for event in due:
        if event.phase != "close" and _close_soon(pending, out_dir, clock()):
            break  # 締め切り直前の取得を先にする。残りは次の実行で
        record = observe(event, clock, fetch_body)
        if event.phase == "post2":
            post1 = _read_record(record_path(out_dir, Event(event.when, event.race, "post1")))
            record["final_check"] = final_check(post1, record, event.post_at)
        write_record(record_path(out_dir, event), record)
        logger.info("%s %s: %s", event.race["race_key"], event.phase, record["status"])
    if all(record_path(out_dir, e).exists() for e in pending):
        return EXIT_DONE
    return EXIT_MORE if clock() < deadline else EXIT_TIMEOUT


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
