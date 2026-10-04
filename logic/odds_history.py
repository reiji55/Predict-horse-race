"""単勝オッズの時系列観測をレース単位で保存する。

目的は「直前資金を今すぐ賢い資金として使う」ことではない。
まず発走前に観測された市場分布を時系列で残し、後から
- 13時→直前で人気がどう動いたか
- 動きと実際の着順/モデル成績に関係があったか
を検証できるようにする。

保存先は data/odds_history/{race_id}/{観測時刻}_{phase}.json（1観測=1ファイル）。
通常pipelineと直前観測workflowは別々にcommit/pushするため、1レース1ファイルへ
追記する形だと両者が同じJSONを書き換えてrebaseが衝突する。1観測1ファイルなら
互いに新規ファイルを足すだけで、push競合時も `git pull --rebase` で必ず合流できる。

予想ロジックとは独立した観測ログで、p・妙味・カードには一切使わない。
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
from typing import Any

from logic import snapshots

ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = ROOT / "data" / "odds_history"
JST = datetime.timezone(datetime.timedelta(hours=9))

# 通常pipelineのrawがこれより古ければ「今回取った値」とみなさない。
# build_raw が途中で止まり前回のrawが残っているケースで、古いオッズに
# 新しい observed_at を付けて保存しないため。
MAX_RAW_AGE = datetime.timedelta(minutes=60)

ADDED = "added"
DUPLICATE = "duplicate"
NO_ODDS = "no_odds"
AFTER_POST = "after_post"
UNKNOWN_POST_TIME = "unknown_post_time"


def _rows_from_race(race: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for entry in race.get("entries", []):
        num = entry.get("num")
        odds = entry.get("win_odds")
        if num is None or odds is None:
            continue
        rows.append({
            "num": num,
            "win_odds": float(odds),
            "popularity": entry.get("popularity"),
        })
    return sorted(rows, key=lambda row: row["num"])


def _market_signature(observation: dict[str, Any]) -> str:
    """券種別の source_time と状態。式別の価格時刻が変わった観測は、単勝が同じでも別に残す。"""
    markets = observation.get("market_meta") or {}
    return json.dumps(
        {k: [(v or {}).get("source_time"), (v or {}).get("status")]
         for k, v in sorted(markets.items())},
        ensure_ascii=False, sort_keys=True,
    )


def _signature(observation: dict[str, Any]) -> tuple[Any, str, str]:
    """API側時刻＋馬番/オッズ本体（＋券種別の価格時刻）が同じ観測は重複保存しない。"""
    return (
        observation.get("source_time"),
        json.dumps(observation.get("odds") or [], ensure_ascii=False, sort_keys=True),
        _market_signature(observation),
    )


# 観測が「どの券種の鮮度まで証明できるか」。odds の行そのものは常に単勝だけ。
SCOPE_WIN = "win"            # 単勝だけを取った観測（直前の軽量取得など）
SCOPE_ALL = "all"            # 単勝・馬連・ワイド・3連複を取りに行った観測（通常pipeline）
SCOPE_LEGACY = "legacy_unknown"  # 券種別の記録が無い旧形式。source_time は単勝のものとしてだけ読める


def market_scope_of(market_meta: dict[str, Any] | None) -> str:
    if not market_meta:
        return SCOPE_LEGACY
    fetched = {k for k, v in market_meta.items() if (v or {}).get("status") != "not_fetched"}
    if fetched <= {"win"}:
        return SCOPE_WIN
    return SCOPE_ALL


def parse_source_time(value: Any) -> datetime.datetime | None:
    """netkeiba の official_datetime（"2026-09-26 15:10:02"、JST・tz無し）を読む。"""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("/", "-"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def pre_race_status(race: dict[str, Any], observed_at: datetime.datetime,
                    source_time: Any = None,
                    market_meta: dict[str, Any] | None = None) -> str | None:
    """発走前の観測と言えないなら理由を返す（fail-closed）。発走前なら None。"""
    post_at = snapshots.post_datetime(race)
    if post_at is None:
        return UNKNOWN_POST_TIME
    if observed_at >= post_at:
        return AFTER_POST
    # 取得開始は発走前でも、APIが返した時刻が発走後なら確定オッズ扱い。式別も同じ。
    times = [source_time] + [(v or {}).get("source_time") for v in (market_meta or {}).values()]
    for value in times:
        source_at = parse_source_time(value)
        if source_at is not None and source_at >= post_at:
            return AFTER_POST
    for value in [(v or {}).get("observed_at") for v in (market_meta or {}).values()]:
        fetched_at = parse_source_time(value)
        if fetched_at is not None and fetched_at >= post_at:
            return AFTER_POST
    return None


def build_observation(race: dict[str, Any], phase: str,
                      observed_at: datetime.datetime | None = None) -> dict[str, Any] | None:
    rows = _rows_from_race(race)
    if not rows:
        return None
    now = observed_at or datetime.datetime.now(JST)
    market_meta = (race.get("odds_market_meta") or {}).get("markets") or None
    observation = {
        "race_id": race.get("id"),
        "source_ref": (race.get("source_refs") or {}).get("netkeiba"),
        "post_time": race.get("post_time"),
        "observed_at": now.isoformat(timespec="seconds"),
        # 単勝の source_time（provider が返した official_datetime）。odds の行と同じく単勝だけの時刻。
        "source_time": race.get("odds_updated_at"),
        "phase": phase,
        "odds_rows_market": "win",
        # この観測が鮮度を証明できる券種の範囲。単勝が新しくても式別まで新しいとは扱わない。
        "market_scope": market_scope_of(market_meta),
        "odds": rows,
    }
    if market_meta:
        observation["market_meta"] = market_meta
    return observation


def load_observations(race_id: str, directory: Path | None = None) -> list[dict[str, Any]]:
    """1レースの観測を observed_at 順に返す（研究用の読み出し口）。"""
    race_dir = (directory or HISTORY_DIR) / race_id
    if not race_dir.is_dir():
        return []
    rows = []
    for path in sorted(race_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                loaded = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            rows.append(loaded)
    return sorted(rows, key=lambda row: row.get("observed_at") or "")


def append_observation(race: dict[str, Any], phase: str,
                       observed_at: datetime.datetime | None = None,
                       directory: Path | None = None) -> str:
    """1観測を保存し、結果（ADDED / DUPLICATE / NO_ODDS / AFTER_POST …）を返す。"""
    race_id = race.get("id")
    now = observed_at or datetime.datetime.now(JST)
    observation = build_observation(race, phase, now)
    if not race_id or observation is None:
        return NO_ODDS

    status = pre_race_status(race, now, observation["source_time"],
                             observation.get("market_meta"))
    if status is not None:
        return status

    directory = directory or HISTORY_DIR
    sig = _signature(observation)
    if any(_signature(row) == sig for row in load_observations(race_id, directory)):
        return DUPLICATE

    race_dir = directory / race_id
    race_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(JST).strftime("%Y%m%dT%H%M%S")
    path = race_dir / f"{stamp}_{phase}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(observation, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return ADDED


def _raw_fetched_at(raw: dict[str, Any]) -> datetime.datetime | None:
    value = raw.get("fetched_at")
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def append_current_run(raw: dict[str, Any], phase: str = "pipeline",
                       observed_at: datetime.datetime | None = None,
                       directory: Path | None = None,
                       now: datetime.datetime | None = None) -> dict[str, Any]:
    """通常pipelineで今まさに取得したレースだけを履歴へ追加する。

    観測時刻は「このスクリプトを動かした時刻」ではなく raw の fetched_at
    （オッズを取り終えた時刻）を使う。rawが古い／collection_reportが無い場合は
    今回取った値と確認できないので何も保存しない。
    """
    now = now or datetime.datetime.now(JST)
    report: dict[str, Any] = {"added": 0, "skipped": 0, "reasons": {}}

    collection = raw.get("collection_report")
    fetched_at = _raw_fetched_at(raw)
    if not isinstance(collection, dict):
        report["reasons"]["missing_collection_report"] = len(raw.get("races", []))
        return report
    if fetched_at is None or now - fetched_at > MAX_RAW_AGE:
        report["reasons"]["stale_raw"] = len(raw.get("races", []))
        return report

    built_ids = {
        item.get("race_id") for item in collection.get("built", [])
        if item.get("race_id")
    }
    races = [race for race in raw.get("races", []) if race.get("id") in built_ids]

    for race in races:
        status = append_observation(race, phase, observed_at or fetched_at, directory)
        if status == ADDED:
            report["added"] += 1
        else:
            report["skipped"] += 1
            report["reasons"][status] = report["reasons"].get(status, 0) + 1
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="単勝オッズ観測履歴を保存")
    parser.add_argument("--raw", required=True, help="raw/{week_id}.json")
    parser.add_argument("--phase", default="pipeline")
    args = parser.parse_args()

    with open(args.raw, encoding="utf-8") as f:
        raw = json.load(f)

    report = append_current_run(raw, phase=args.phase)
    print(f"odds history: added={report['added']} skipped={report['skipped']} "
          f"reasons={report['reasons']}")


if __name__ == "__main__":
    main()
