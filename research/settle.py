"""結果確定後の shadow 採点と累積集計（observe-only）。

入力（すべて読むだけ）:
    data/snapshots/{race_id}.json          … 発走前に凍結された Champion 予想
    data/shadow/prerace/{race_id}/*.json   … 発走前の shadow 記録（あれば優先）
    data/race_results.json                 … 着順と配当
    data/chappy_decisions/{race_id}/*.json … チャットChappy decision log（あれば）

出力（研究専用。data/results.json・UIには混ぜない）:
    data/shadow/races/{race_id}.json … レース単位
    data/shadow/summary.json         … 累積（forward と retrospective を分ける）

phase:
    forward        … config.registered_at 以降に発走したレース（事前登録後の前向き検証）
    retrospective  … それ以前。仮説を作るのに使ったデータを含むので、判定には使わない
evidence:
    prerace_record_matched   … 発走前の shadow 記録があり、採点時の snapshot と SHA-256 が一致
    prerace_record_unmatched … 発走前記録はあるが snapshot の版が違う（最後の発走前再生成で記録が落ちた等）
    derived_from_snapshot    … 発走前記録が無い。凍結 snapshot から同じ規則で再構成（選定順は取れない）
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import statistics
from pathlib import Path
from typing import Any

from logic import chappy, snapshots
from research import calibration, chappy_shadow, common, decision_log, diversity, rank_gap
from results import build_results

logger = logging.getLogger("research.settle")

SCHEMA = "shadow-race-result-v1"
SUMMARY_SCHEMA = "shadow-summary-v1"


def _prerace_record(race_id: str, post: datetime.datetime, snapshot_sha: str,
                    directory: Path) -> tuple[dict[str, Any] | None, str]:
    race_dir = directory / race_id
    rows = []
    for path in sorted(race_dir.glob("*.json")) if race_dir.is_dir() else []:
        row = common.read_json(path)
        captured = common.parse_dt((row or {}).get("captured_at")) if isinstance(row, dict) else None
        if isinstance(row, dict) and row.get("pre_race") is True and captured and captured < post:
            rows.append((captured, row))
    if not rows:
        return None, "derived_from_snapshot"
    matched = [r for c, r in rows
               if ((r.get("provenance") or {}).get("snapshot") or {}).get("sha256") == snapshot_sha]
    if matched:
        return max(matched, key=lambda r: r["captured_at"]), "prerace_record_matched"
    return max(rows, key=lambda cr: cr[0])[1], "prerace_record_unmatched"


def _settle_decision_logs(race_id: str, finish: list[int], dividends: dict[str, Any],
                          config: dict[str, Any], decision_dir: Path, repo: Path,
                          snapshot_dir: Path) -> list[dict[str, Any]]:
    out = []
    top3 = {int(n) for n in (finish or [])[:3]}
    for path in decision_log.log_paths(race_id, decision_dir):
        result = decision_log.verify(path, config, repo=repo, snapshot_dir=snapshot_dir)
        row: dict[str, Any] = {k: result[k] for k in (
            "path", "valid", "errors", "created_at", "git_first_commit_at",
            "snapshot_version_found", "facts_checked_against", "prerace_verified")}
        log = common.read_json(path) or {}
        bets = [{k: b[k] for k in ("type", "horses", "amt")} for b in log.get("bets") or []
                if b.get("type") in decision_log.BET_SIZE and b.get("horses") and b.get("amt")]
        if bets:
            settled = [build_results.settle_bet(b, dividends) for b in bets]
            horses = {int(n) for b in bets for n in b["horses"]}
            row.update({
                "spent": sum(b["amt"] for b in settled),
                "payout": sum(b["payout"] for b in settled),
                "hit_bets": sum(1 for b in settled if b["hit"]),
                "top3_capture": len(top3 & horses),
                "dropped_top3_finishers": sorted(
                    int(c["num"]) for c in log.get("candidates") or []
                    if c.get("decision") == "drop" and int(c.get("num", -1)) in top3
                ),
                "bets": settled,
            })
        out.append(row)
    return out


def settle_race(snapshot: dict[str, Any], snapshot_path: Path, result: dict[str, Any],
                history: list[tuple[datetime.datetime, dict[str, Any]]],
                config: dict[str, Any], chappy_config: dict[str, Any], temperature_now: float,
                prerace_dir: Path, decision_dir: Path, repo: Path,
                snapshot_dir: Path) -> dict[str, Any]:
    race_id = snapshot["race_id"]
    post = common.post_at(race_id, snapshot.get("post_time"))
    snapshot_sha = common.sha256_file(snapshot_path)
    record, evidence = _prerace_record(race_id, post, snapshot_sha, prerace_dir)
    matched = evidence == "prerace_record_matched"
    finish = [int(n) for n in result.get("finish") or []]
    dividends = result.get("dividends") or {}
    winner = finish[0] if finish else None

    # --- 確率較正 ------------------------------------------------------
    if matched:
        calib_inputs = record["calibration_inputs"]
    else:
        calib_inputs = {
            "temperature_current": temperature_now,
            "temperature_source": "config_at_settlement",
            "score_source": "snapshot_marks_rounded",
            "horses": [{"num": m["num"], "score": m.get("score"), "odds": m.get("odds")}
                       for m in snapshot.get("marks") or []],
        }
    prior = [item for p, item in history if p < post]   # このレースより前に発走したレースだけ
    calib = calibration.evaluate_race(calib_inputs, winner, prior, config)
    history_item = calib.pop("history_item", None)

    # --- 順位ズレ / Chappy shadow / 多様性 --------------------------------
    registration = record["rank_gap"] if matched else rank_gap.register(snapshot.get("marks") or [], config)
    built = record["chappy_shadow"] if matched else chappy_shadow.build(snapshot, chappy_config, config)
    orders = ((record or {}).get("diversity") or {}).get("sel_orders") if matched else None
    sets = diversity.card_horse_sets(snapshot.get("cards") or [], config["diversity"]["characters"])

    payload = {
        "schema": SCHEMA,
        "race_id": race_id,
        "phase": common.phase_of(post, config),
        "evidence": evidence,
        "post_at": post.isoformat() if post else None,
        "snapshot": {"sha256": snapshot_sha, "frozen_at": snapshot.get("frozen_at"),
                     "model_id": snapshot.get("model_id"), "config_hash": snapshot.get("config_hash")},
        "prerace_record_captured_at": (record or {}).get("captured_at"),
        "finish": finish[:3],
        "official_results_untouched": True,
        "experiments": {
            "calibration": {**common.experiment_meta(config, "calibration"), **calib},
            "rank_gap": {**common.experiment_meta(config, "rank_gap"),
                         **rank_gap.evaluate(registration, finish)},
            "chappy_shadow": {**common.experiment_meta(config, "chappy_shadow"),
                              "chappy_version": built.get("chappy_version"),
                              "variants": chappy_shadow.evaluate(built, finish, dividends)},
            "diversity": {**common.experiment_meta(config, "diversity"),
                          **diversity.evaluate(orders, sets, config)},
            "chat_chappy": _settle_decision_logs(race_id, finish, dividends, config,
                                                 decision_dir, repo, snapshot_dir),
        },
    }
    return {"payload": payload, "history_item": history_item, "post": post}


# ------------------------------------------------------------------ 累積

def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _se(values: list[float]) -> float | None:
    return round(statistics.stdev(values) / len(values) ** 0.5, 6) if len(values) >= 2 else None


def summarize(rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    exps = [r["experiments"] for r in rows]

    # 較正: モデルごとの平均と、市場 q との対応差（同じレースだけで比べる）
    calib: dict[str, Any] = {}
    evaluated = [e["calibration"] for e in exps if e["calibration"].get("status") == "evaluated"]
    names = sorted({n for c in evaluated for n in c["models"]})
    for name in names:
        ll = [c["models"][name]["log_loss"] for c in evaluated if name in c["models"]]
        br = [c["models"][name]["brier"] for c in evaluated if name in c["models"]]
        diff = [c["models"][name]["log_loss"] - c["models"]["q_market"]["log_loss"]
                for c in evaluated if name in c["models"]]
        calib[name] = {"races": len(ll), "mean_log_loss": _mean(ll), "mean_brier": _mean(br),
                       "log_loss_minus_market_mean": _mean(diff),
                       "log_loss_minus_market_se": _se(diff)}

    # Chappy: 同じレースでの現行 vs shadow
    chappy_sum: dict[str, Any] = {}
    variants = sorted({v for e in exps for v in e["chappy_shadow"]["variants"]})
    for name in variants:
        items = [e["chappy_shadow"]["variants"][name] for e in exps if name in e["chappy_shadow"]["variants"]]
        spent = sum(i["spent"] for i in items)
        payout = sum(i["payout"] for i in items)
        payouts = sorted((i["payout"] for i in items), reverse=True)
        spent_ex = spent - (items[[i["payout"] for i in items].index(payouts[0])]["spent"] if items else 0)
        chappy_sum[name] = {
            "races": len(items), "spent": spent, "payout": payout,
            "roi": round(payout / spent, 6) if spent else None,
            "roi_ex_max_payout_race": round((payout - payouts[0]) / spent_ex, 6) if spent_ex else None,
            "hit_races": sum(1 for i in items if i["hit"]),
            "mean_top3_capture": _mean([i["top3_capture"] for i in items]),
            "mean_anchor_stake_share": _mean([i["anchor_stake_share"] for i in items
                                              if i.get("anchor_stake_share") is not None]),
        }
    paired = [(e["chappy_shadow"]["variants"][v]["payout"] - e["chappy_shadow"]["variants"]["current_auto"]["payout"])
              for e in exps for v in e["chappy_shadow"]["variants"]
              if v != "current_auto" and "current_auto" in e["chappy_shadow"]["variants"]]
    chappy_sum["_paired_payout_diff_vs_current"] = {"races": len(paired), "mean": _mean(paired), "se": _se(paired)}

    # 多様性
    div: dict[str, Any] = {}
    for e in exps:
        for pair in e["diversity"]["pairs"]:
            d = div.setdefault(pair["pair"], {"races": 0, "spearman": [], "axis_match": 0,
                                              "jaccard": [], "redundancy_flags": 0})
            d["races"] += 1
            if pair["sel_spearman"] is not None:
                d["spearman"].append(pair["sel_spearman"])
            d["axis_match"] += int(pair["axis_match"])
            if pair["jaccard"] is not None:
                d["jaccard"].append(pair["jaccard"])
            d["redundancy_flags"] += int(pair["redundancy_flag"])
    diversity_sum = {
        k: {"races": v["races"], "sel_spearman_races": len(v["spearman"]),
            "mean_sel_spearman": _mean(v["spearman"]),
            "axis_match_rate": round(v["axis_match"] / v["races"], 6) if v["races"] else None,
            "mean_jaccard": _mean(v["jaccard"]), "redundancy_flags": v["redundancy_flags"]}
        for k, v in sorted(div.items())
    }

    # 順位ズレ
    gaps = [e["rank_gap"] for e in exps]
    rank_gap_sum = {
        "races": len(gaps),
        "races_with_registrations": sum(1 for g in gaps if g["n"]),
        "horses": sum(g["n"] for g in gaps),
        "observed_top3": sum(g["observed_top3"] for g in gaps),
        "expected_top3": round(sum(g["expected_top3"] for g in gaps), 6),
        "observed_minus_expected": round(sum(g["observed_minus_expected"] for g in gaps), 6),
        "note": "expected は単勝 q からの Harville。人気薄の3着内率を低めに出す既知の偏りがある",
    }

    # チャットChappy
    logs = [log for e in exps for log in e["chat_chappy"]]
    verified = [log for log in logs if log["prerace_verified"]]
    chat_sum = {
        "logs": len(logs), "valid": sum(1 for log in logs if log["valid"]),
        "prerace_verified": len(verified),
        "verified_spent": sum(log.get("spent", 0) for log in verified),
        "verified_payout": sum(log.get("payout", 0) for log in verified),
    }

    return {"races": len(rows),
            "evidence": {k: sum(1 for r in rows if r["evidence"] == k)
                         for k in ("prerace_record_matched", "prerace_record_unmatched", "derived_from_snapshot")},
            "calibration": calib, "chappy_shadow": chappy_sum, "diversity": diversity_sum,
            "rank_gap": rank_gap_sum, "chat_chappy": chat_sum}


def settle(race_results: dict[str, Any], snapshot_dir: Path | None = None,
           prerace_dir: Path | None = None, decision_dir: Path | None = None,
           races_dir: Path | None = None, summary_path: Path | None = None,
           config: dict[str, Any] | None = None, chappy_config: dict[str, Any] | None = None,
           temperature_now: float | None = None, repo: Path | None = None,
           now: datetime.datetime | None = None) -> dict[str, Any]:
    snapshot_dir = snapshot_dir or snapshots.SNAPSHOT_DIR
    prerace_dir = prerace_dir or common.PRERACE_DIR
    decision_dir = decision_dir or common.DECISION_LOG_DIR
    races_dir = races_dir or common.RACES_DIR
    summary_path = summary_path or common.SUMMARY_PATH
    config = config or common.load_config()
    chappy_config = chappy_config or chappy.load_config()
    if temperature_now is None:
        myomi_cfg = common.read_json(common.ROOT / "config" / "myomi.json") or {}
        temperature_now = float(myomi_cfg["prob_model"]["temperature"])
    repo = repo or common.ROOT

    targets = []
    for path in sorted(snapshot_dir.glob("*.json")):
        snap = common.read_json(path)
        if not isinstance(snap, dict) or not common.is_model_race(snap):
            continue
        result = race_results.get(snap.get("race_id")) or {}
        if len(result.get("finish") or []) < 3:
            continue
        post = common.post_at(snap["race_id"], snap.get("post_time"))
        if post is None:
            continue
        targets.append((post, snap["race_id"], path, snap, result))
    targets.sort(key=lambda t: (t[0], t[1]))

    history: list[tuple[datetime.datetime, dict[str, Any]]] = []
    rows = []
    for post, race_id, path, snap, result in targets:
        settled = settle_race(snap, path, result, history, config, chappy_config, temperature_now,
                              prerace_dir, decision_dir, repo, snapshot_dir)
        if settled["history_item"] is not None:
            history.append((post, settled["history_item"]))
        common.write_json(races_dir / f"{race_id}.json", settled["payload"])
        rows.append(settled["payload"])

    summary = {
        "schema": SUMMARY_SCHEMA,
        "generated_at": (now or datetime.datetime.now(common.JST)).isoformat(timespec="seconds"),
        "research_version": config.get("version"),
        "research_config_hash": common.config_hash(config),
        "registered_at": config.get("registered_at"),
        "official_results_untouched": True,
        "note": "shadow研究の集計。Championの正式成績(data/results.json)とは別物。"
                "forward（事前登録後）だけを判定に使い、retrospective は参考。",
        "forward": summarize([r for r in rows if r["phase"] == "forward"], config),
        "retrospective": summarize([r for r in rows if r["phase"] == "retrospective"], config),
    }
    common.write_json(summary_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="shadow 研究の結果採点と累積集計（observe-only）")
    parser.add_argument("--results", default=str(common.ROOT / "data" / "race_results.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    race_results = common.read_json(Path(args.results))
    if not isinstance(race_results, dict):
        logger.warning("%s を読めないため shadow 採点をスキップします", args.results)
        return
    summary = settle(race_results)
    print(json.dumps({k: summary[k]["races"] for k in ("forward", "retrospective")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
