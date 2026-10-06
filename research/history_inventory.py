"""過去走の棚卸し（history-feature-inventory-v1、observe-only）。

予想ロジックは変えない。各出走馬について次を研究用 artifact に記録する。

1. いまの予想が見ている過去走（current history = raw の past_runs、通常は過去5走）から何が見えているか
2. 長期履歴（long history）を取れたときは、current との差分（増えた走・増えた条件実績）
3. 取得済みなのに、いまの着順系の評価（recent / condition / aptitude / top3）が使っていない情報
   （class / margin_sec / last3f など）

保存先: data/research/history_inventory/{race_id}/{stamp}_{retrieval_timing}.json（1観測1ファイル、上書きしない）

--- 守ること ---
- 本番の raw / past_runs / config/scraper.json の past_runs=5 は変えない。Champion・Challenger・snapshot の入力も変えない。
- cutoff は run.date < race.date。レース後に長期履歴を取ると当該レースの結果が先頭に入るので、
  当日・未来・日付不明の走は除外し、件数を excluded_by_cutoff に残す。
- 長期履歴を取れなければ status（fetch_failed / blocked / unavailable / not_requested）を残し、推測で埋めない。
  取得失敗で通常の予想 workflow を止めない。
- 「玄人の根拠」の候補タグ（expert_evidence）はオッズを一切入力にしない。根拠を先に作り、価格は後から別軸で見る。
- 前走不利・出遅れ・調教・パドック・陣営の気配などは構造化データが無いので作らない（status: unavailable）。
- class・margin を点数化しない。閾値は config のタグ付け専用で、本番ルールにしない。
- 「長期履歴を増やしたら精度が上がる」とは、ここでは結論しない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import statistics
from pathlib import Path
from typing import Any, Callable

from logic import chappy, snapshots

logger = logging.getLogger("research.history_inventory")

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
CONFIG_PATH = ROOT / "config" / "history_inventory.json"
OUTPUT_DIR = ROOT / "data" / "research" / "history_inventory"
JST = snapshots.JST

PRE_RACE = "pre_race"
RETROSPECTIVE = "retrospective"

LONG_OK = "ok"
LONG_FETCH_FAILED = "fetch_failed"
LONG_BLOCKED = "blocked"
LONG_UNAVAILABLE = "unavailable"
LONG_NOT_REQUESTED = "not_requested"

LongFetcher = Callable[[str, int], dict[str, Any]]


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as f:
        return json.load(f)


# ---- 小道具 -------------------------------------------------------------------------

def race_date(race: dict[str, Any]) -> datetime.date | None:
    race_id = race.get("id") or ""
    if len(race_id) < 8 or not race_id[:8].isdigit():
        return None
    return datetime.datetime.strptime(race_id[:8], "%Y%m%d").date()


def _run_date(run: dict[str, Any]) -> datetime.date | None:
    value = str(run.get("date") or "").replace("/", "-")
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def apply_cutoff(runs: list[dict[str, Any]], cutoff: datetime.date | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """run.date < race.date の走だけを残す。当日・未来・日付不明は除外して数える。"""
    kept, same_day, future, undated = [], [], [], []
    for run in runs:
        d = _run_date(run)
        if d is None or cutoff is None:
            undated.append(run.get("date"))
        elif d == cutoff:
            same_day.append(run.get("date"))
        elif d > cutoff:
            future.append(run.get("date"))
        else:
            kept.append(run)
    excluded = {
        "count": len(same_day) + len(future) + len(undated),
        "same_day": len(same_day),
        "future": len(future),
        "undated": len(undated),
        "dates": same_day + future,
    }
    return kept, excluded


def _today(race: dict[str, Any]) -> dict[str, Any]:
    course = race.get("course") or {}
    return {
        "surface": course.get("surface"),
        "dist": course.get("dist"),
        "venue": race.get("venue"),
        "going": race.get("going"),
    }


def _top3(run: dict[str, Any]) -> bool:
    return run.get("finish") is not None and run["finish"] <= 3


def _class_rank(klass: Any, order: list[str]) -> int | None:
    return order.index(klass) if klass in order else None


def _run_key(run: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
    return (run.get("date"), run.get("venue"), run.get("surface"), run.get("dist"))


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    keys = ("date", "venue", "surface", "dist", "going", "class", "finish", "heads",
            "margin_sec", "time_sec", "last3f", "race_name")
    return {k: run.get(k) for k in keys if k in run}


def _dates_block(runs: list[dict[str, Any]]) -> dict[str, Any]:
    dates = [r.get("date") for r in runs]
    valid = sorted(d for d in dates if d)
    return {
        "available_runs": len(runs),
        "dates": dates,
        "earliest_date": valid[0] if valid else None,
        "latest_date": valid[-1] if valid else None,
    }


# ---- 条件・クラス・着差の棚卸し（点数にはしない） -----------------------------------

def _condition_sets(runs: list[dict[str, Any]], today: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    # chappy.condition_profile と同じ切り方（同surface → 同距離 → 同場、同surface × 同馬場）
    same_surface = [r for r in runs if r.get("surface") == today["surface"]]
    same_dist = [r for r in same_surface if today["dist"] is not None and r.get("dist") == today["dist"]]
    same_course_dist = [r for r in same_dist if r.get("venue") == today["venue"]]
    same_going = [r for r in same_surface if today["going"] and r.get("going") == today["going"]]
    return {"surface": same_surface, "distance": same_dist,
            "course_distance": same_course_dist, "going": same_going}


def condition_stats(runs: list[dict[str, Any]], today: dict[str, Any],
                    class_order: list[str]) -> dict[str, Any]:
    sets = _condition_sets(runs, today)
    cd = sets["course_distance"]
    cd_finished = [r for r in cd if r.get("finish") is not None]
    cd_margins = [r["margin_sec"] for r in cd if r.get("margin_sec") is not None]
    cd_classes = [r.get("class") for r in cd if _class_rank(r.get("class"), class_order) is not None]
    return {
        "same_surface_runs": len(sets["surface"]),
        "same_distance_runs": len(sets["distance"]),
        "same_distance_top3": sum(_top3(r) for r in sets["distance"]),
        "same_course_distance_runs": len(cd),
        "same_course_distance_top3": sum(_top3(r) for r in cd),
        "same_going_runs": len(sets["going"]),
        "same_going_top3": sum(_top3(r) for r in sets["going"]),
        "best_finish_same_course_distance": min((r["finish"] for r in cd_finished), default=None),
        "best_margin_same_course_distance": min(cd_margins, default=None),
        "best_class_same_course_distance": (
            max(cd_classes, key=lambda c: class_order.index(c)) if cd_classes else None
        ),
    }


def class_stats(runs: list[dict[str, Any]], class_order: list[str], high_class_min: str) -> dict[str, Any]:
    ranked = [r.get("class") for r in runs if _class_rank(r.get("class"), class_order) is not None]
    high_rank = class_order.index(high_class_min)
    return {
        "class_available_runs": len(ranked),
        "best_class": max(ranked, key=lambda c: class_order.index(c)) if ranked else None,
        "recent_classes": [r.get("class") for r in runs],
        "high_class_runs": sum(1 for c in ranked if class_order.index(c) >= high_rank),
        "g1_runs": sum(1 for c in ranked if c == "g1"),
        "g2plus_runs": sum(1 for c in ranked if c in ("g2", "g1")),
        "note": "記録のみ。クラスを点数化しない",
    }


def margin_stats(runs: list[dict[str, Any]], thresholds: list[float]) -> dict[str, Any]:
    margins = [float(r["margin_sec"]) for r in runs if r.get("margin_sec") is not None]
    return {
        "margin_available_runs": len(margins),
        "best_margin": min(margins) if margins else None,
        "median_margin": round(statistics.median(margins), 3) if margins else None,
        # 閾値ごとの件数。比較用の記録で、「何秒以内なら強い」という本番ルールではない
        "close_finish_runs": {f"{t:.1f}": sum(1 for m in margins if m <= t + 1e-9) for t in thresholds},
    }


def _present(value: Any) -> bool:
    if value is None:
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def coverage_stats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(runs)
    return {
        "time_sec_coverage": round(sum(_present(r.get("time_sec")) for r in runs) / n, 4) if n else None,
        "last3f_coverage": round(sum(_present(r.get("last3f")) for r in runs) / n, 4) if n else None,
    }


def window_inventory(runs: list[dict[str, Any]], today: dict[str, Any],
                     config: dict[str, Any]) -> dict[str, Any]:
    order = config["class_order"]
    return {
        "conditions": condition_stats(runs, today, order),
        "class": class_stats(runs, order, config["high_class_min"]),
        "margin": margin_stats(runs, config["close_finish_margins_sec"]),
        "coverage": coverage_stats(runs),
    }


# ---- 生の証拠と「玄人の根拠」候補（オッズは見ない） ------------------------------------

def notable_runs(runs: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    rule = config["notable_runs"]["high_class_close_finish"]
    order = config["class_order"]
    min_rank = order.index(rule["min_class"])
    out = []
    for run in runs:
        rank = _class_rank(run.get("class"), order)
        margin = run.get("margin_sec")
        if rank is None or margin is None or run.get("finish") is None:
            continue
        if rank >= min_rank and float(margin) <= float(rule["max_margin_sec"]) + 1e-9:
            out.append({"reason": "high_class_close_finish", "evidence": _run_summary(run),
                        "status": "hypothesis_candidate"})
    return out


def _going_rule_applies(today: dict[str, Any], cfg: dict[str, Any]) -> bool:
    return today.get("going") in cfg["going_proven_only_when_today_going_in"]


def expert_evidence(runs: list[dict[str, Any]], today: dict[str, Any], config: dict[str, Any],
                    source: str) -> list[dict[str, Any]]:
    """根拠候補のタグ。**引数にオッズを取らない**（人気薄かどうかは後から別軸で見る）。"""
    cfg = config["expert_evidence"]
    sets = _condition_sets(runs, today)
    tags: list[dict[str, Any]] = []
    for note in notable_runs(runs, config):
        tags.append({"type": "high_class_close_finish", "source": source, "evidence": [note["evidence"]]})
    cd_hits = [r for r in sets["course_distance"] if _top3(r)]
    if len(cd_hits) >= cfg["course_distance_proven_min_top3"]:
        tags.append({"type": "course_distance_proven", "source": source,
                     "evidence": [_run_summary(r) for r in cd_hits]})
    d_hits = [r for r in sets["distance"] if _top3(r)]
    if len(d_hits) >= cfg["distance_proven_min_top3"]:
        tags.append({"type": "distance_proven", "source": source,
                     "evidence": [_run_summary(r) for r in d_hits]})
    if _going_rule_applies(today, cfg):
        g_hits = [r for r in sets["going"] if _top3(r)]
        if len(g_hits) >= cfg["going_proven_min_top3"]:
            tags.append({"type": "going_proven", "source": source,
                         "evidence": [_run_summary(r) for r in g_hits]})
    return tags


def unavailable_evidence(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": t, "status": "unavailable", "reason": "no_structured_source"}
            for t in config["expert_evidence"]["unavailable_types"]]


# ---- 同じレース名の実績（長期履歴でレース名が取れたときだけ） ------------------------------

_GRADE_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")


def normalize_race_name(name: Any) -> str | None:
    if not name:
        return None
    text = _GRADE_PAREN_RE.sub("", str(name))
    text = re.sub(r"[\s　]+", "", text)
    return text or None


def same_named_race(runs: list[dict[str, Any]], today_name: Any, long_status: str) -> dict[str, Any]:
    target = normalize_race_name(today_name)
    if long_status != LONG_OK or target is None:
        return {"status": "unavailable",
                "reason": "race_name_not_in_current_past_runs" if long_status != LONG_OK else "no_race_name"}
    named = [r for r in runs if r.get("race_name")]
    if not named:
        return {"status": "unavailable", "reason": "no_race_name_in_long_history"}
    hits = [r for r in named if normalize_race_name(r.get("race_name")) == target]
    return {
        "status": "ok",
        "race_name": target,
        "same_named_race_runs": len(hits),
        "same_named_race_top3": sum(_top3(r) for r in hits),
        "evidence": [_run_summary(r) for r in hits],
    }


# ---- 馬ごと・レースごと --------------------------------------------------------------

def _recent_profile_view(runs: list[dict[str, Any]], today: dict[str, Any]) -> dict[str, Any]:
    """いまの chappy.recent_profile が見ている走と、その走にあるのに使われていない値。"""
    profile = chappy.recent_profile(runs, today)
    used_runs = [r for r in runs if r.get("surface") == today["surface"]][:3]
    return {
        "profile": profile,
        "uses": ["finish", "heads"],
        "runs_seen": [_run_summary(r) for r in used_runs],
        "present_but_unused": {
            "class": sum(1 for r in used_runs if r.get("class") is not None),
            "margin_sec": sum(1 for r in used_runs if r.get("margin_sec") is not None),
            "last3f": sum(1 for r in used_runs if _present(r.get("last3f"))),
        },
    }


def _coverage_delta(current: list[dict[str, Any]], long_runs: list[dict[str, Any]],
                    today: dict[str, Any]) -> dict[str, Any]:
    current_keys = {_run_key(r) for r in current}
    added = [r for r in long_runs if _run_key(r) not in current_keys]
    sets = _condition_sets(added, today)
    return {
        "added_runs": len(added),
        "added_same_surface_runs": len(sets["surface"]),
        "added_same_distance_runs": len(sets["distance"]),
        "added_same_course_distance_runs": len(sets["course_distance"]),
        "added_same_going_runs": len(sets["going"]),
        "added_run_dates": [r.get("date") for r in added],
    }, added


def horse_inventory(entry: dict[str, Any], race: dict[str, Any], config: dict[str, Any],
                    long_result: dict[str, Any] | None) -> dict[str, Any]:
    today = _today(race)
    cutoff = race_date(race)
    current_raw = entry.get("past_runs") or []
    current, current_excluded = apply_cutoff(current_raw, cutoff)

    current_block = {"requested_runs": config["current_window"], **_dates_block(current),
                     "excluded_by_cutoff": current_excluded}
    out: dict[str, Any] = {
        "num": entry.get("num"),
        "name": entry.get("name"),
        "horse_ref": (entry.get("horse_ref") or {}).get("netkeiba"),
        "current_history": current_block,
        "current": window_inventory(current, today, config),
        "current_recent_profile_view": _recent_profile_view(current, today),
        "performance_evidence": {
            "recent_runs": [_run_summary(r) for r in current],
            "notable_runs": notable_runs(current, config),
            "note": "将来の仮説候補。Champion が使う特徴量ではない",
        },
    }

    long_status = (long_result or {}).get("status") or LONG_NOT_REQUESTED
    evidence = expert_evidence(current, today, config, "current")
    if long_status == LONG_OK:
        long_runs, long_excluded = apply_cutoff(long_result.get("runs") or [], cutoff)
        delta, added = _coverage_delta(current, long_runs, today)
        out["long_history"] = {"status": LONG_OK, "requested_runs": config["long_window"],
                               **_dates_block(long_runs), "source": config["long_history"]["source"],
                               "excluded_by_cutoff": long_excluded}
        out["long"] = window_inventory(long_runs, today, config)
        out["coverage_delta"] = delta
        out["performance_evidence"]["notable_runs_long_only"] = notable_runs(added, config)
        older = expert_evidence(added, today, config, "long_only")
        if older:
            evidence.append({"type": "older_form_not_visible_in_current_window", "source": "long_only",
                             "evidence": [e for tag in older for e in tag["evidence"]],
                             "underlying_types": sorted({tag["type"] for tag in older})})
        out["same_named_race"] = same_named_race(long_runs, race.get("name"), long_status)
    else:
        out["long_history"] = {"status": long_status, "requested_runs": config["long_window"],
                               "available_runs": 0, "dates": [], "earliest_date": None,
                               "latest_date": None, "source": config["long_history"]["source"]}
        out["long"] = None
        out["coverage_delta"] = None
        out["same_named_race"] = same_named_race([], race.get("name"), long_status)
    out["expert_evidence"] = evidence
    out["expert_evidence_unavailable"] = unavailable_evidence(config)
    return out


def feature_usage(horses: list[dict[str, Any]], races_runs: list[dict[str, Any]],
                  config: dict[str, Any]) -> dict[str, Any]:
    """「データが無い」のか「あるが着順系の評価が使っていない」のかを分ける（current history 基準）。"""
    out = {}
    for field, usage in config["feature_usage"].items():
        if field in ("time_sec", "last3f"):
            present = [r for r in races_runs if _present(r.get(field))]
        else:
            present = [r for r in races_runs if r.get(field) is not None]
        out[field] = {
            "available": bool(present),
            "available_runs": len(present),
            "total_runs": len(races_runs),
            "used_by": usage["used_by"],
            "used_by_form_evaluation": usage["used_by_form_evaluation"],
            "available_but_unused_by_form_evaluation": (
                len(present) if not usage["used_by_form_evaluation"] else 0
            ),
        }
        if usage.get("note"):
            out[field]["note"] = usage["note"]
    return out


def build_inventory(race: dict[str, Any], config: dict[str, Any], now: datetime.datetime,
                    long_fetcher: LongFetcher | None = None,
                    allow_retrospective: bool = False) -> dict[str, Any] | None:
    """1レースの棚卸し。発走後は allow_retrospective=True のときだけ作る（retrieval_timing で区別）。"""
    post_at = snapshots.post_datetime(race)
    if post_at is None:
        return None
    timing = PRE_RACE if now < post_at else RETROSPECTIVE
    if timing == RETROSPECTIVE and not allow_retrospective:
        return None

    horses, long_statuses = [], {}
    for entry in race.get("entries") or []:
        long_result = None
        if long_fetcher is not None:
            ref = (entry.get("horse_ref") or {}).get("netkeiba")
            if not ref:
                long_result = {"status": LONG_UNAVAILABLE, "runs": []}
            else:
                try:
                    long_result = long_fetcher(ref, config["long_window"])
                except Exception:  # noqa: BLE001 — 研究用。どんな失敗でも推測せず status に残して続ける
                    logger.warning("長期履歴を取得できませんでした: %s", ref, exc_info=True)
                    long_result = {"status": LONG_FETCH_FAILED, "runs": []}
        horse = horse_inventory(entry, race, config, long_result)
        long_statuses[horse["long_history"]["status"]] = long_statuses.get(horse["long_history"]["status"], 0) + 1
        horses.append(horse)

    all_current_runs = []
    for entry in race.get("entries") or []:
        kept, _ = apply_cutoff(entry.get("past_runs") or [], race_date(race))
        all_current_runs.extend(kept)
    requested = sum(1 for _ in horses) if long_fetcher is not None else 0
    ok = long_statuses.get(LONG_OK, 0)
    return {
        "version": config["version"],
        "mode": "observe_only",
        "race_id": race.get("id"),
        "race_name": race.get("name"),
        "race_date": race_date(race).isoformat(),
        "post_at": post_at.isoformat(timespec="seconds"),
        "observed_at": now.isoformat(timespec="seconds"),
        "retrieval_timing": timing,
        "cutoff_rule": config["cutoff_rule"],
        "today": _today(race),
        "source": {
            "current_history": "raw/{week}.json entries[].past_runs（いまの予想の入力そのまま）",
            "long_history": config["long_history"]["source"] if long_fetcher is not None else None,
        },
        "source_status": {
            "long_history_requested": long_fetcher is not None,
            "long_history_status_counts": long_statuses,
            "long_history_success_rate": round(ok / requested, 4) if requested else None,
        },
        "feature_usage": feature_usage(horses, all_current_runs, config),
        "horses": horses,
        "interpretation": "observe_only。ここから『長期履歴を増やせば精度が上がる』とは結論しない",
    }


def write_inventory(inventory: dict[str, Any], directory: Path | None = None) -> Path:
    race_dir = (directory or OUTPUT_DIR) / inventory["race_id"]
    race_dir.mkdir(parents=True, exist_ok=True)
    observed = datetime.datetime.fromisoformat(inventory["observed_at"])
    stem = f"{observed.astimezone(JST).strftime('%Y%m%dT%H%M%S')}_{inventory['retrieval_timing']}"
    path = race_dir / f"{stem}.json"
    n = 2
    while path.exists():
        path = race_dir / f"{stem}_{n}.json"
        n += 1
    with path.open("w", encoding="utf-8") as f:
        json.dump(inventory, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def capture(raw: dict[str, Any], config: dict[str, Any] | None = None,
            now: datetime.datetime | None = None, long_fetcher: LongFetcher | None = None,
            allow_retrospective: bool = False, race_ids: list[str] | None = None,
            directory: Path | None = None) -> dict[str, Any]:
    config = config or load_config()
    now = now or datetime.datetime.now(JST)
    report: dict[str, Any] = {"written": [], "skipped": []}
    for race in raw.get("races") or []:
        if race_ids and race.get("id") not in race_ids:
            continue
        inventory = build_inventory(race, config, now, long_fetcher, allow_retrospective)
        if inventory is None:
            report["skipped"].append({"race_id": race.get("id"), "reason": "posted_or_unknown_post_time"})
            continue
        report["written"].append(str(write_inventory(inventory, directory)))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="過去走の棚卸し（observe-only）")
    parser.add_argument("--week", required=True)
    parser.add_argument("--race-id", action="append", help="対象レース（複数可）。省略で raw の全レース")
    parser.add_argument("--long", action="store_true", help="研究用に長期履歴（db.netkeiba.com）も取りに行く")
    parser.add_argument("--retrospective", action="store_true",
                        help="発走後のレースも retrospective として作る（forward 成績には混ぜない）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    path = RAW_DIR / f"{args.week}.json"
    if not path.exists():
        print(f"{path} がありません。棚卸しはしません")
        return
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    fetcher = None
    if args.long:
        from scraper.fetchers import c_horse_history
        cache: dict[str, dict[str, Any]] = {}

        def fetcher(ref: str, n_runs: int) -> dict[str, Any]:
            if ref not in cache:
                cache[ref] = c_horse_history.fetch_horse_history_for_research(ref, n_runs)
            return cache[ref]

    report = capture(raw, long_fetcher=fetcher, allow_retrospective=args.retrospective,
                     race_ids=args.race_id)
    print(f"history inventory: written={len(report['written'])} skipped={len(report['skipped'])}")


if __name__ == "__main__":
    main()
