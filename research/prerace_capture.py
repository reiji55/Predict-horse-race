"""発走前の shadow 記録（data/shadow/prerace/{race_id}/{時刻}_{phase}.json）。

通常パイプラインで Champion の予想と snapshot が作られた**直後**に動き、発走前のレースだけについて
「後で採点するための入力」を凍結する。1観測1ファイルの追記で、既存ファイルは書き換えない
（別workflowと同時にpushしても衝突しない。data/odds_history と同じ方式）。

- 入力は、今まさに凍結された Champion snapshot（SHA-256 を記録）と、同じ raw。
- Championの出力・snapshot・predictions.json は**読むだけ**。書き換えない。
- キャラ別の選定順は、本番と同じ関数（build_predictions.prepare_horses → cards.*）で再計算する。
  再計算した印順・スコアが snapshot と一致しない場合は fidelity=false を残し、選定順は採点に使わない。
- 発走時刻を過ぎたレースは記録しない（fail-closed）。
"""
from __future__ import annotations

import argparse
import datetime
import logging
from pathlib import Path
from typing import Any

from logic import build_predictions as bp, cards, model_registry, prob_model, snapshots
from logic import speed_index as speed_mod
from research import chappy_shadow, common, diversity, rank_gap

logger = logging.getLogger("research.prerace_capture")

SCHEMA = "shadow-prerace-v1"


def _recompute_horses(race: dict[str, Any], configs: dict[str, Any], base_times: dict[str, Any],
                      rates: tuple[float, float], model_spec: dict[str, Any] | None = None
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """build_race と同じ順序で、キャラ別選定に必要な列まで作る（Champion出力は作らない）。

    model_spec を渡すと、そのモデルの base_score の作り方で再計算する（事前登録した Race Performance を
    使う Challenger だけが違う。基準タイム表の違いは呼び出し側が base_times で渡す）。
    """
    horses, _quality = bp.prepare_horses(race, configs, base_times, *rates)
    loaded = model_registry.load_model_race_performance(model_spec) if model_spec else None
    bp.compute_model_base_scores(race, horses, configs["cards"],
                                 loaded[0] if loaded else None, loaded[1] if loaded else None)
    cards.assign_place_partner_ranks(horses)
    marks = cards.assign_marks(horses, configs["cards"])
    temperature = configs["myomi"]["prob_model"]["temperature"]
    p = prob_model.softmax_scores([h["score"] for h in horses], temperature)
    q = prob_model.market_support([h["odds"] for h in horses])
    for horse, p_i, q_i, info in zip(horses, p, q, cards.compute_value_and_myomi_rank(p, q)):
        horse["p"], horse["q"] = p_i, q_i
        horse["value"], horse["myomi_rank"] = info["value"], info["myomi_rank"]
    return horses, marks


# 選定順（sel / value）はスコアだけでなく単勝オッズにも依存する。オッズだけ更新された raw で
# 「一致」と誤判定しないよう、印の並び・スコア・オッズ・Top3 の列まで一致を求める。
FIDELITY_KEYS = ("num", "score", "odds", "top3_score", "top3_rank")


def _fidelity(recomputed_marks: list[dict[str, Any]], snapshot_marks: list[dict[str, Any]]) -> dict[str, Any]:
    """再計算した marks が snapshot と完全に一致するか。1列でも違えば false（fail-closed）。"""
    a = [tuple(m.get(k) for k in FIDELITY_KEYS) for m in recomputed_marks]
    b = [tuple(m.get(k) for k in FIDELITY_KEYS) for m in snapshot_marks]
    mismatches = []
    for i, (row_a, row_b) in enumerate(zip(a, b)):
        diff = [k for k, va, vb in zip(FIDELITY_KEYS, row_a, row_b) if va != vb]
        if diff:
            mismatches.append({"position": i + 1, "num": row_b[0], "fields": diff})
    if len(a) != len(b):
        mismatches.append({"position": None, "num": None, "fields": ["length"]})
    return {"recomputed_matches_snapshot": not mismatches,
            "compared_fields": list(FIDELITY_KEYS),
            "mismatches": mismatches,
            "recomputed_order": [r[0] for r in a], "snapshot_order": [r[0] for r in b]}


def _latest_record(race_dir: Path) -> dict[str, Any] | None:
    if not race_dir.is_dir():
        return None
    rows = [common.read_json(p) for p in sorted(race_dir.glob("*.json"))]
    rows = [r for r in rows if isinstance(r, dict)]
    return max(rows, key=lambda r: r.get("captured_at") or "") if rows else None


def build_record(race: dict[str, Any], snapshot: dict[str, Any], snapshot_path: Path,
                 now: datetime.datetime, phase: str, configs: dict[str, Any],
                 base_times: dict[str, Any], rates: tuple[float, float],
                 config: dict[str, Any], post: datetime.datetime) -> dict[str, Any]:
    temperature = configs["myomi"]["prob_model"]["temperature"]
    horses, marks = _recompute_horses(race, configs, base_times, rates)
    fidelity = _fidelity(marks, snapshot.get("marks") or [])

    if fidelity["recomputed_matches_snapshot"]:
        calib_horses = [{"num": h["num"], "score": h["score"], "odds": h["odds"]}
                        for h in horses if h.get("num") is not None]
        score_source = "prerace_full_precision"
        orders = diversity.character_orders(horses, configs["cards"], temperature,
                                            config["diversity"]["characters"])
    else:
        logger.warning("%s: 再計算が snapshot と一致しないため選定順は記録しません", race.get("id"))
        calib_horses = [{"num": m["num"], "score": m.get("score"), "odds": m.get("odds")}
                        for m in snapshot.get("marks") or []]
        score_source = "snapshot_marks_rounded"
        orders = None

    return {
        "schema": SCHEMA,
        "race_id": race.get("id"),
        "phase": phase,
        "pre_race": True,
        "captured_at": now.isoformat(timespec="seconds"),
        "post_time": race.get("post_time"),
        "minutes_to_post": round((post - now).total_seconds() / 60, 1),
        "provenance": {
            "git_commit": common.git_commit(),
            "research_version": config.get("version"),
            "research_config_hash": common.config_hash(config),
            "registered_at": config.get("registered_at"),
            "experiments": {k: (config.get(k) or {}).get("version")
                            for k in ("calibration", "chappy_shadow", "diversity", "rank_gap")},
            "snapshot": {
                "path": str(snapshot_path.relative_to(common.ROOT))
                if snapshot_path.is_relative_to(common.ROOT) else str(snapshot_path),
                "sha256": common.sha256_file(snapshot_path),
                "frozen_at": snapshot.get("frozen_at"),
                "model_id": snapshot.get("model_id"),
                "config_hash": snapshot.get("config_hash"),
                "base_times_hash": snapshot.get("base_times_hash"),
            },
        },
        "fidelity": fidelity,
        "calibration_inputs": {
            "temperature_current": temperature,
            "score_source": score_source,
            "horses": calib_horses,
        },
        "diversity": {"sel_orders": orders},
        "rank_gap": rank_gap.register(snapshot.get("marks") or [], config),
        "chappy_shadow": chappy_shadow.build(snapshot, configs["chappy"], config),
    }


def capture(raw: dict[str, Any], now: datetime.datetime | None = None, phase: str = "pipeline",
            snapshot_dir: Path | None = None, output_dir: Path | None = None,
            configs: dict[str, Any] | None = None, base_times: dict[str, Any] | None = None,
            config: dict[str, Any] | None = None) -> dict[str, list[Any]]:
    now = now or datetime.datetime.now(common.JST)
    snapshot_dir = snapshot_dir or snapshots.SNAPSHOT_DIR
    output_dir = output_dir or common.PRERACE_DIR
    configs = configs or bp.load_configs()
    base_times = base_times if base_times is not None else speed_mod.load_base_times()
    config = config or common.load_config()
    rates = bp._overall_rates(raw)
    report: dict[str, list[Any]] = {"added": [], "skipped": []}

    for race in raw.get("races") or []:
        race_id = race.get("id")
        if not race_id:
            continue
        post = common.post_at(race_id, race.get("post_time"))
        if post is None:
            report["skipped"].append({"race_id": race_id, "reason": "unknown_post_time"})
            continue
        if now >= post:
            report["skipped"].append({"race_id": race_id, "reason": "already_posted"})
            continue
        snapshot_path = snapshots.snapshot_path(race_id, snapshot_dir)
        snapshot = common.read_json(snapshot_path)
        if not isinstance(snapshot, dict) or not common.is_model_race(snapshot):
            report["skipped"].append({"race_id": race_id, "reason": "no_prerace_model_snapshot"})
            continue

        race_dir = output_dir / race_id
        latest = _latest_record(race_dir)
        snap_sha = common.sha256_file(snapshot_path)
        if latest and ((latest.get("provenance") or {}).get("snapshot") or {}).get("sha256") == snap_sha \
                and (latest.get("provenance") or {}).get("research_config_hash") == common.config_hash(config):
            report["skipped"].append({"race_id": race_id, "reason": "unchanged_snapshot"})
            continue

        record = build_record(race, snapshot, snapshot_path, now, phase, configs,
                              base_times, rates, config, post)
        stamp = now.astimezone(common.JST).strftime("%Y%m%dT%H%M%S")
        path = race_dir / f"{stamp}_{phase}.json"
        if path.exists():
            report["skipped"].append({"race_id": race_id, "reason": "duplicate_time"})
            continue
        common.write_json(path, record)
        report["added"].append(race_id)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="発走前の shadow 研究記録を保存（observe-only）")
    parser.add_argument("--week", required=True)
    parser.add_argument("--phase", default="pipeline")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    raw = common.read_json(bp.RAW_DIR / f"{args.week}.json")
    if not isinstance(raw, dict):
        logger.warning("raw/%s.json が無いため shadow 記録をスキップします", args.week)
        return
    report = capture(raw, phase=args.phase)
    print(f"shadow prerace: added={len(report['added'])} skipped={len(report['skipped'])}")


if __name__ == "__main__":
    main()
