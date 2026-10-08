"""speed-v2 Challenger の forward 評価（speed-v2-forward-eval-v1）。evaluation-only / observe-only。

仕様: docs/audit/SPEED_V2_FORWARD_EVALUATION_SPEC_20261002.md

Champion と speed-v2 Challenger を、**同じレース・同じ発走前情報**で対にして比べる物差しを、
10/3 以降の結果を見る前に固定する。昇格の閾値は決めない（config の promotion は not_registered）。

2つの段階に分かれる。

    capture（発走前・通常パイプラインの直後）
        両モデルの発走前 snapshot を読み、本番と同じ関数で再計算した全精度 score とキャラ別選定順を
        data/shadow/model_evaluation/prerace/{model_id}/{race_id}/{時刻}_{phase}.json に追記する。
        再計算が snapshot と一致しないとき（fidelity=false）は選定順を残さない。発走後は記録しない。

    evaluate（結果確定後）
        data/shadow/model_evaluation/{challenger_id}/races/{race_id}.json … レース単位
        data/shadow/model_evaluation/{challenger_id}/summary.json         … 累積

対にする条件（どれか1つでも欠ければ評価しないで理由を残す。黙って落とさない）:
    - post_time が max(評価式の registered_at, Challenger の registered_at) より後（forward）
    - 確定着順（3着まで）がある
    - 両モデルの snapshot が発走前に凍結されている（pre_race=true、frozen_at < post）
    - 同じビルドで作られた（frozen_at・config_hash が同じ）、出走馬と単勝オッズが同じ
    - Challenger の snapshot が登録どおりの凍結表（artifact_id・lookup_sha256）で作られている
登録前のレースは excluded_pre_registration に分けて出し、coverage の分母にも入れない。

事前登録から始めた評価（race-performance-v1：config/model_evaluation_race_performance_v1.json）も、
同じ関数を `--config` で使う。その評価の capture は設定の capture_dir（自分のフォルダ）に置き、
capture についての照合（capture_problem）と、事前登録の有効日時（prereg_effective_at）を足す。

このモジュールは予想経路（logic/・scraper/）から読まれない。出力は予想の入力にならない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import statistics
from pathlib import Path
from typing import Any

from logic import build_predictions as bp
from logic import cards, model_registry, race_performance, snapshots
from logic import speed_index as speed_mod
from research import calibration, common, diversity, forward_diagnostics, prerace_capture
from results import build_results

logger = logging.getLogger("research.model_evaluation")

CAPTURE_SCHEMA = "model-eval-prerace-v1"
RACE_SCHEMA = "model-eval-race-v1"
SUMMARY_SCHEMA = "model-eval-summary-v1"
CONFIG_PATH = common.ROOT / "config" / "model_evaluation.json"
EVAL_DIR = common.SHADOW_DIR / "model_evaluation"
CAPTURE_DIR = EVAL_DIR / "prerace"
CHALLENGER_ROOT = common.ROOT / "data" / "challengers"
# registry に登録した凍結表の項目。snapshot の base_times_ref と全部一致したときだけ「登録どおり」とみなす
ARTIFACT_PIN_KEYS = ("artifact_id", "lookup_sha256", "meta_content_sha256", "method_version", "cutoff_date")
# 事前登録した Race Performance を使う Challenger は、snapshot の race_performance_ref を registry と照合する
RACE_PERFORMANCE_PIN_KEYS = ("version", "config_sha256")

# paired の向き。lower/higher は良し悪しの向き、descriptive は向きを決めない観察値（多様性）
LOWER_IS_BETTER = "lower_is_better"
HIGHER_IS_BETTER = "higher_is_better"
DESCRIPTIVE = "descriptive_only"


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as f:
        return json.load(f)


def capture_dir_for(cfg: dict[str, Any]) -> Path:
    """
    その評価式の発走前 capture の置き場所。設定に capture_dir が無ければ CAPTURE_DIR（speed-v2 の評価は従来どおり）。

    Champion の capture は評価式ごとに取る。同じフォルダに置くと、2つの評価が同じ秒に取ったときに
    ファイル名（時刻_phase）が重なり、後の方が duplicate_time で捨てられる。なので評価ごとに分ける。
    """
    relative = cfg.get("capture_dir")
    if not relative:
        return CAPTURE_DIR
    path = (common.ROOT / relative).resolve()
    if EVAL_DIR.resolve() not in path.parents:
        raise ValueError(f"capture_dir は data/shadow/model_evaluation/ の下だけです: {relative}")
    return path


def model_specs(cfg: dict[str, Any], registry: dict[str, Any] | None = None,
                require_registered: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    """評価対象の (Champion, Challenger) の登録内容。config と registry が食い違えば止める。

    require_registered=False は発走前の capture だけ（記録するだけで、forward の境界はまだ使わない）。
    Challenger の registered_at（マージの時刻）は、マージ後に記録するまで無いことがあるため。
    採点（evaluate）は常に registered_at を求める。
    """
    registry = registry or model_registry.load_registry()
    champion = registry["champion"]
    if champion["id"] != cfg["champion_model_id"]:
        raise ValueError(f"Champion が評価式の登録と違います: {champion['id']} != {cfg['champion_model_id']}")
    challenger = next((m for m in registry.get("challengers", []) if m["id"] == cfg["challenger_model_id"]), None)
    if challenger is None:
        raise ValueError(f"Challenger {cfg['challenger_model_id']} が registry にありません")
    if require_registered and not challenger.get("registered_at"):
        raise ValueError(f"{challenger['id']} に registered_at がありません")
    return champion, challenger


def forward_start(cfg: dict[str, Any], challenger: dict[str, Any]) -> datetime.datetime:
    """forward の境界。評価式・Challenger・（あれば）事前登録の有効日時のうち、いちばん遅いもの。

    これより後に発走したレースだけを数える。事前登録の有効日時（prereg_effective_at）を持つのは
    事前登録から始めた評価（race-performance-v1 など）だけで、speed-v2 の評価は従来どおり2つの遅い方。
    """
    times = [common.parse_dt(cfg.get("registered_at")), common.parse_dt(challenger.get("registered_at"))]
    if cfg.get("prereg_effective_at") is not None:
        times.append(common.parse_dt(cfg.get("prereg_effective_at")))
    if any(t is None for t in times):
        raise ValueError("registered_at が読めません")
    return max(times)  # type: ignore[type-var]


def snapshot_dir_for(spec: dict[str, Any], champion_dir: Path, challenger_root: Path) -> Path:
    return champion_dir if spec.get("role") == "champion" else challenger_root / spec["id"] / "snapshots"


# ================================================================== capture（発走前）

def _base_times_for(spec: dict[str, Any]) -> dict[str, Any]:
    own = model_registry.load_model_base_times(spec)
    return own[0] if own is not None else speed_mod.load_base_times()


def _latest_capture(race_dir: Path, cfg_hash: str | None = None) -> dict[str, Any] | None:
    """そのレースの最新の capture。cfg_hash を渡すと、その評価式で取ったものの中の最新。

    Champion の capture は評価ごとに同じフォルダへ追記される（speed-v2 と race-performance-v1）。
    評価式ごとに見ないと、別の評価の capture を「最新」と見て、変わっていない snapshot を毎回取り直してしまう。
    """
    if not race_dir.is_dir():
        return None
    rows = [r for r in (common.read_json(p) for p in sorted(race_dir.glob("*.json"))) if isinstance(r, dict)]
    if cfg_hash is not None:
        rows = [r for r in rows if (r.get("provenance") or {}).get("evaluation_config_hash") == cfg_hash]
    return max(rows, key=lambda r: r.get("captured_at") or "") if rows else None


def capture_record(race: dict[str, Any], spec: dict[str, Any], snapshot: dict[str, Any], snapshot_path: Path,
                   now: datetime.datetime, phase: str, configs: dict[str, Any], base_times: dict[str, Any],
                   rates: tuple[float, float], cfg: dict[str, Any], post: datetime.datetime) -> dict[str, Any]:
    temperature = configs["myomi"]["prob_model"]["temperature"]
    # そのモデルの作り方で再計算する（事前登録した Race Performance を使う Challenger は4因子）
    horses, marks = prerace_capture._recompute_horses(race, configs, base_times, rates, model_spec=spec)
    fidelity = prerace_capture._fidelity(marks, snapshot.get("marks") or [])
    ok = fidelity["recomputed_matches_snapshot"]
    if not ok:
        logger.warning("%s/%s: 再計算が snapshot と一致しないため全精度 score・選定順は記録しません",
                       spec["id"], race.get("id"))
    return {
        "schema": CAPTURE_SCHEMA,
        "model_id": spec["id"],
        "race_id": race.get("id"),
        "phase": phase,
        "pre_race": True,
        "captured_at": now.isoformat(timespec="seconds"),
        "post_time": race.get("post_time"),
        "minutes_to_post": round((post - now).total_seconds() / 60, 1),
        "provenance": {
            "git_commit": common.git_commit(),
            "evaluation_version": cfg.get("version"),
            "evaluation_config_hash": common.config_hash(cfg),
            # 両モデルが同じ raw のレースから作られたことを、モデル間で照合するための指紋
            "input_raw_sha256": race_performance.canonical_sha256(race),
            "snapshot": {
                "path": str(snapshot_path.relative_to(common.ROOT))
                if snapshot_path.is_relative_to(common.ROOT) else str(snapshot_path),
                "sha256": common.sha256_file(snapshot_path),
                "frozen_at": snapshot.get("frozen_at"),
                "model_id": snapshot.get("model_id"),
                "config_hash": snapshot.get("config_hash"),
                "base_times_hash": snapshot.get("base_times_hash"),
                "base_times_ref": snapshot.get("base_times_ref"),
                "race_performance_ref": snapshot.get("race_performance_ref"),
            },
        },
        "fidelity": fidelity,
        "temperature": temperature,
        # capture 時点の本番設定の指紋。snapshot の config_hash と同じなら、この temperature は凍結時と同じ
        "model_config_hash": model_registry.config_hash(),
        "scores": ([{"num": h["num"], "score": h["score"], "odds": h["odds"]}
                    for h in horses if h.get("num") is not None] if ok else None),
        "sel_orders": (diversity.character_orders(horses, configs["cards"], temperature,
                                                  cfg["diversity"]["fixed_three"]) if ok else None),
    }


def capture(raw: dict[str, Any], now: datetime.datetime | None = None, phase: str = "pipeline",
            champion_dir: Path | None = None, challenger_root: Path | None = None,
            output_dir: Path | None = None, configs: dict[str, Any] | None = None,
            cfg: dict[str, Any] | None = None, registry: dict[str, Any] | None = None) -> dict[str, list[Any]]:
    """発走前のレースについて、両モデルの評価用入力を追記する（既存ファイルは書き換えない）。"""
    now = now or datetime.datetime.now(common.JST)
    champion_dir = champion_dir or snapshots.SNAPSHOT_DIR
    challenger_root = challenger_root or CHALLENGER_ROOT
    configs = configs or bp.load_configs()
    cfg = cfg or load_config()
    output_dir = output_dir or capture_dir_for(cfg)
    specs = model_specs(cfg, registry, require_registered=False)
    tables = {spec["id"]: _base_times_for(spec) for spec in specs}
    cfg_hash = common.config_hash(cfg)
    rates = bp._overall_rates(raw)
    report: dict[str, list[Any]] = {"added": [], "skipped": []}

    for race in raw.get("races") or []:
        race_id = race.get("id")
        post = common.post_at(race_id, race.get("post_time")) if race_id else None
        if post is None:
            report["skipped"].append({"race_id": race_id, "reason": "unknown_post_time"})
            continue
        if now >= post:
            report["skipped"].append({"race_id": race_id, "reason": "already_posted"})
            continue
        for spec in specs:
            path = snapshots.snapshot_path(race_id, snapshot_dir_for(spec, champion_dir, challenger_root))
            snap = common.read_json(path)
            if not isinstance(snap, dict) or not common.is_model_race(snap):
                report["skipped"].append({"race_id": race_id, "model_id": spec["id"],
                                          "reason": "no_prerace_model_snapshot"})
                continue
            race_dir = output_dir / spec["id"] / race_id
            latest = _latest_capture(race_dir, cfg_hash)
            prov = (latest or {}).get("provenance") or {}
            if latest and (prov.get("snapshot") or {}).get("sha256") == common.sha256_file(path) \
                    and prov.get("evaluation_config_hash") == cfg_hash:
                report["skipped"].append({"race_id": race_id, "model_id": spec["id"], "reason": "unchanged_snapshot"})
                continue
            record = capture_record(race, spec, snap, path, now, phase, configs, tables[spec["id"]],
                                    rates, cfg, post)
            out = race_dir / f"{now.astimezone(common.JST).strftime('%Y%m%dT%H%M%S')}_{phase}.json"
            if out.exists():
                report["skipped"].append({"race_id": race_id, "model_id": spec["id"], "reason": "duplicate_time"})
                continue
            common.write_json(out, record)
            report["added"].append({"race_id": race_id, "model_id": spec["id"]})
    return report


# ================================================================== evaluate（結果確定後）

def matched_capture(model_id: str, race_id: str, post: datetime.datetime, snapshot_sha: str,
                    capture_dir: Path, cfg_hash: str) -> dict[str, Any] | None:
    """採点時の snapshot と SHA-256 が一致し、発走前に、いまと同じ評価式で取られた capture のうち最新のもの。

    評価式（config）が変わったあとは、旧い評価式で取った capture を新しい式に混ぜない。
    """
    race_dir = capture_dir / model_id / race_id
    if not race_dir.is_dir():
        return None
    matched = []
    for path in sorted(race_dir.glob("*.json")):
        row = common.read_json(path)
        if not isinstance(row, dict):
            continue
        captured = common.parse_dt(row.get("captured_at"))
        prov = row.get("provenance") or {}
        sha = (prov.get("snapshot") or {}).get("sha256")
        if captured is not None and captured < post and sha == snapshot_sha \
                and prov.get("evaluation_config_hash") == cfg_hash:
            matched.append((captured, row))
    return max(matched, key=lambda t: t[0])[1] if matched else None


def pairing_problem(champ: dict[str, Any], chall: dict[str, Any], post: datetime.datetime,
                    challenger: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """2つの snapshot を同じ発走前情報の対として扱えないなら理由を返す。"""
    rules = cfg["pairing"]
    for snap, who in ((champ, "champion"), (chall, "challenger")):
        frozen = common.parse_dt(snap.get("frozen_at"))
        if rules["require_frozen_before_post"] and (frozen is None or frozen >= post):
            return f"{who}_frozen_after_post"
    if rules["require_same_frozen_at"] and champ.get("frozen_at") != chall.get("frozen_at"):
        return "frozen_at_mismatch"
    if rules["require_same_config_hash"] and champ.get("config_hash") != chall.get("config_hash"):
        return "config_hash_mismatch"
    if chall.get("model_id") != challenger["id"]:
        return "challenger_model_id_mismatch"
    if rules["require_registered_artifact"]:
        if challenger.get("race_performance"):
            # 事前登録した Race Performance の定義（version・設定の canonical SHA-256）が登録どおりか
            ref, pin = chall.get("race_performance_ref") or {}, challenger["race_performance"]
            if any(ref.get(k) != pin.get(k) for k in RACE_PERFORMANCE_PIN_KEYS):
                return "challenger_artifact_mismatch"
        else:
            ref, pin = chall.get("base_times_ref") or {}, challenger.get("base_times") or {}
            if any(ref.get(k) != pin.get(k) for k in ARTIFACT_PIN_KEYS):
                return "challenger_artifact_mismatch"
    if rules["require_same_horses_and_odds"]:
        odds = [{int(m["num"]): m.get("odds") for m in s.get("marks") or [] if m.get("num") is not None}
                for s in (champ, chall)]
        if odds[0] != odds[1]:
            return "prerace_inputs_differ"
    return None


def capture_problem(captures: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """
    発走前 capture についての照合（事前登録から始めた評価だけが使う規則。speed-v2 の評価には無い）。

    - require_captures：両モデルの capture が揃うレースだけを対にする
    - require_capture_fidelity：各モデルの capture が、そのモデル自身の snapshot と再計算で一致している
    - require_same_input_raw_hash：両モデルが同じ raw のレースから作られている（モデル間の共通入力）
    """
    rules = cfg["pairing"]
    if rules.get("require_captures") and any(c is None for c in captures.values()):
        return "capture_missing"
    present = [c for c in captures.values() if c is not None]
    if rules.get("require_capture_fidelity") and any(
            not (c.get("fidelity") or {}).get("recomputed_matches_snapshot") for c in present):
        return "capture_fidelity_mismatch"
    if rules.get("require_same_input_raw_hash"):
        hashes = {(c.get("provenance") or {}).get("input_raw_sha256") for c in present}
        if len(present) != len(captures) or len(hashes) != 1 or None in hashes:
            return "input_raw_mismatch"
    return None


def _scores(snapshot: dict[str, Any], capture_row: dict[str, Any] | None, full: bool) -> dict[int, float]:
    if full:
        return {int(h["num"]): float(h["score"]) for h in capture_row["scores"]  # type: ignore[index]
                if h.get("score") is not None}
    return {int(m["num"]): float(m["score"]) for m in snapshot.get("marks") or []
            if m.get("num") is not None and m.get("score") is not None}


def resolve_temperature(captures: dict[str, Any], snapshot_config_hash: str | None,
                        current_config_hash: str, current_temperature: float) -> tuple[float | None, str]:
    """
    log loss / Brier に使う T を、**凍結時の値**として確かめられる経路だけで決める。

    1. 両モデルの capture がある → capture に残した T（両モデル同じで、capture 時の設定が snapshot と同じこと）
    2. capture が無い → snapshot の config_hash がいまの本番設定と同じときだけ、いまの T
       （config_hash は myomi.json を含むので、同じなら T も同じ）
    それ以外は T を確かめられないので確率評価をしない。後から T を変えても過去の数字は書き換わらない。
    """
    if all(c is not None for c in captures.values()):
        temps = {c.get("temperature") for c in captures.values()}
        hashes = {c.get("model_config_hash") for c in captures.values()}
        if len(temps) != 1 or None in temps:
            return None, "capture_temperature_mismatch"
        if hashes != {snapshot_config_hash}:
            return None, "capture_config_differs_from_snapshot"
        return float(temps.pop()), "capture"
    if snapshot_config_hash is not None and snapshot_config_hash == current_config_hash:
        return float(current_temperature), "current_config_verified"
    return None, "temperature_unverifiable"


def probability_block(champ: dict[str, Any], chall: dict[str, Any], captures: dict[str, Any],
                      winner: int, temperature: float | None, temperature_source: str,
                      cfg: dict[str, Any]) -> dict[str, Any]:
    """
    単勝 p の log loss / Brier。

    モデルの p は本番と同じく **score のある全馬**で softmax(score/T) する。単勝オッズの有無で馬を落とさない。
    両モデルで score のある馬の集合が違えば比べない（fail-closed）。
    市場 q は参考の基準線で、出走馬全員の正の単勝オッズが揃うときだけ出す（q のためにモデルの p を変えない）。
    """
    pcfg = cfg["probability"]
    full = all(c is not None and c.get("scores") for c in captures.values())
    scores = {"champion": _scores(champ, captures["champion"], full),
              "challenger": _scores(chall, captures["challenger"], full)}
    field = [int(m["num"]) for m in champ.get("marks") or [] if m.get("num") is not None]
    scored = sorted(n for n in field if n in scores["champion"])
    coverage = len(scored) / len(field) if field else 0.0
    out: dict[str, Any] = {
        "score_source": "capture_full_precision" if full else "snapshot_marks_rounded",
        "temperature": temperature,
        "temperature_source": temperature_source,
        "evaluation_set": {"field_size": len(field), "scored_horses": len(scored), "coverage": round(coverage, 6)},
        "winner": winner,
    }
    if temperature is None:
        out["status"] = temperature_source
        return out
    if set(scores["champion"]) != set(scores["challenger"]):
        out["status"] = "score_set_mismatch"
        return out
    if len(scored) < 2 or coverage < float(pcfg["min_score_coverage"]):
        out["status"] = "insufficient_coverage"
        return out
    if winner not in scored:
        out["status"] = "winner_not_evaluable"
        return out
    eps = float(pcfg["eps"])
    probs = {name: calibration._softmax({n: s[n] for n in scored}, temperature) for name, s in scores.items()}
    out["models"] = {name: calibration.win_metrics(prob, winner, eps) for name, prob in probs.items()}

    odds = {int(m["num"]): m.get("odds") for m in champ.get("marks") or [] if m.get("num") is not None}
    total = (champ.get("speed_quality") or {}).get("total_horses")
    if total is not None and int(total) != len(odds):
        # marks が出走馬全員を含んでいない（score の無い馬がいる等）。全頭の q とは言えないので出さない
        q = None
        out["market"] = {"status": "market_field_incomplete", "marks": len(odds), "total_horses": int(total)}
    elif all(isinstance(o, (int, float)) and o > 0 for o in odds.values()) and winner in odds:
        q = calibration._normalise({n: 1.0 / float(o) for n, o in odds.items()})
        out["market"] = {"status": "available", **calibration.win_metrics(q, winner, eps)}
    else:
        q = None
        out["market"] = {"status": "incomplete_market_odds",
                         "missing_odds": sorted(n for n, o in odds.items()
                                                if not (isinstance(o, (int, float)) and o > 0))}
    out["horses"] = [{"num": n, "y": int(n == winner),
                      "p_champion": round(probs["champion"][n], 6), "p_challenger": round(probs["challenger"][n], 6),
                      "p_q_market": round(q[n], 6) if q is not None and n in q else None} for n in scored]
    out["status"] = "evaluated"
    return out


def _order_from_capture(capture_row: dict[str, Any] | None, char: str) -> dict[str, Any]:
    orders = (capture_row or {}).get("sel_orders") or {}
    if not orders.get(char):
        return {"status": "unavailable", "reason": "no_matched_capture" if capture_row is None
                else "sel_order_not_recorded"}
    return {"status": "available", "order": [int(r["num"]) for r in orders[char]]}


def model_orders(snapshot: dict[str, Any], capture_row: dict[str, Any] | None, cfg: dict[str, Any]
                 ) -> dict[str, dict[str, Any]]:
    marks = [m for m in snapshot.get("marks") or [] if m.get("num") is not None]
    out = {"base": ({"status": "available", "order": [int(m["num"]) for m in marks]} if marks
                    else {"status": "unavailable", "reason": "no_marks"})}
    ranked = sorted((m for m in marks if m.get("top3_rank") is not None), key=lambda m: (m["top3_rank"], int(m["num"])))
    out["top3"] = ({"status": "available", "order": [int(m["num"]) for m in ranked]} if ranked
                   else {"status": "unavailable", "reason": "no_top3_rank"})
    for char in cfg["diversity"]["fixed_three"]:
        order = _order_from_capture(capture_row, char)
        card = forward_diagnostics._card(snapshot, char)
        if order["status"] == "available" and card and not forward_diagnostics._is_pass(card) \
                and int(card["bets"][0]["horses"][0]) != order["order"][0]:
            order = {"status": "unavailable", "reason": "sel_order_axis_mismatch"}
        out[f"{char}_sel"] = order
    return out


def ranking_block(snapshot: dict[str, Any], orders: dict[str, dict[str, Any]], top3: list[int],
                  cfg: dict[str, Any]) -> dict[str, Any]:
    rcfg = cfg["ranking"]
    recall = forward_diagnostics.recall(orders, top3, {"rank_sources": rcfg["order_sources"],
                                                       "recall_k": rcfg["recall_k"]})
    card_selection: dict[str, Any] = {}
    actual = set(top3)
    for char in rcfg["card_selection_chars"]:
        card = forward_diagnostics._card(snapshot, char)
        if card is None:
            card_selection[char] = {"status": "missing"}
        elif forward_diagnostics._is_pass(card):
            card_selection[char] = {"status": "pass"}
        else:
            horses = sorted({int(n) for b in card["bets"] for n in b["horses"]})
            hit = len(set(horses) & actual)
            card_selection[char] = {"status": "available", "selected_horses": horses,
                                    "count": hit, "recall": round(hit / len(top3), 4)}
    return {"order_recall": recall, "card_selection_recall": card_selection}


def speed_block(snapshot: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    sq = snapshot.get("speed_quality")
    return {
        "surface": (snapshot.get("course") or {}).get("surface"),
        "status": "available" if isinstance(sq, dict) else "missing",
        **({k: sq.get(k) for k in cfg["speed_quality"]["keys"]} if isinstance(sq, dict) else {}),
    }


def diversity_block(snapshot: dict[str, Any], orders: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    return forward_diagnostics.fixed_three(snapshot, orders, cfg["diversity"])


def secondary_block(snapshot: dict[str, Any], result: dict[str, Any], top3: list[int],
                    cfg: dict[str, Any]) -> dict[str, Any]:
    """成績系（判定には使わない）。採点は正式成績と同じ results.build_results の関数で行う。"""
    scfg = cfg["secondary"]
    try:
        settled = build_results.build_race_result({**snapshot, "id": snapshot.get("race_id")}, result)
    except Exception as exc:  # noqa: BLE001 — 成績系が採点できなくても Primary の評価は残す
        logger.warning("%s: secondary を採点できませんでした: %s", snapshot.get("race_id"), exc)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    by_char = {c["char"]: c for c in settled["cards"]}

    def card_row(char: str) -> dict[str, Any]:
        card = by_char.get(char)
        if card is None:
            return {"status": "missing"}
        if card.get("action") == "pass" or not card.get("bets"):
            return {"status": "pass"}
        return {"status": "bet", "hit": bool(card["hit"]), "spent": int(card["spent"]),
                "payout": int(card["payout"])}

    fixed = {char: card_row(char) for char in scfg["fixed_three"]}
    chappy_char = next((c for c in scfg["auto_chappy_chars"] if c in by_char), None)
    conversion = {}
    union_bets: list[tuple[str, tuple[int, ...], int]] = []
    for char in scfg["fixed_three"]:
        card = forward_diagnostics._card(snapshot, char)
        if card is None or forward_diagnostics._is_pass(card):
            continue
        conv = forward_diagnostics.conversion(card, top3)
        conversion[char] = {k: conv[k] for k in ("actual_top3_selected_count", "winning_pair_present_but_not_ticketed",
                                                 "winning_trio_selected_but_not_ticketed")}
        union_bets.extend(forward_diagnostics._bets(card))
    wide_pairs = forward_diagnostics._pairs(top3)
    held_wide = {h for t, h, _ in union_bets if t == cards.WIDE}
    return {
        "status": "evaluated",
        "fixed_three": fixed,
        "auto_chappy": {"char": chappy_char, **card_row(chappy_char)} if chappy_char else {"status": "missing"},
        "conversion": conversion,
        "winning_coverage": {
            "wide_pairs_covered": sum(1 for p in wide_pairs if p in held_wide),
            "wide_pairs_total": len(wide_pairs),
            "umaren_covered": any(t == cards.UMAREN and h == tuple(sorted(top3[:2])) for t, h, _ in union_bets),
            "trio_covered": any(t == cards.SANRENPUKU and h == tuple(sorted(top3)) for t, h, _ in union_bets),
        },
    }


def evaluate_race(race_id: str, post: datetime.datetime, snaps: dict[str, Any], paths: dict[str, Path],
                  result: dict[str, Any], challenger: dict[str, Any], capture_dir: Path,
                  current_temperature: float, current_config_hash: str, cfg: dict[str, Any]) -> dict[str, Any]:
    finish = [int(n) for n in result.get("finish") or []]
    top3, winner = finish[:3], finish[0]
    base = {"schema": RACE_SCHEMA, "race_id": race_id, "post_time": snaps["champion"].get("post_time"),
            "official_results_untouched": True, "used_for_prediction": False,
            "evaluation_version": cfg.get("version"), "evaluation_config_hash": common.config_hash(cfg)}
    problem = pairing_problem(snaps["champion"], snaps["challenger"], post, challenger, cfg)
    if problem:
        return {**base, "status": "not_evaluated", "reason": problem}

    shas = {who: common.sha256_file(paths[who]) for who in ("champion", "challenger")}
    model_ids = {"champion": snaps["champion"].get("model_id"), "challenger": challenger["id"]}
    cfg_hash = common.config_hash(cfg)
    captures = {who: matched_capture(model_ids[who], race_id, post, shas[who], capture_dir, cfg_hash)
                for who in ("champion", "challenger")}
    problem = capture_problem(captures, cfg)
    if problem:
        return {**base, "status": "not_evaluated", "reason": problem}
    temperature, temperature_source = resolve_temperature(
        captures, snaps["champion"].get("config_hash"), current_config_hash, current_temperature)
    # キャラ別選定順は両モデルとも揃ったときだけ使う（片方だけの比較にしない）
    both_sel = all(c is not None and c.get("sel_orders") for c in captures.values())
    models = {}
    for who in ("champion", "challenger"):
        snap = snaps[who]
        orders = model_orders(snap, captures[who] if both_sel else None, cfg)
        models[who] = {
            "model_id": model_ids[who],
            "snapshot": {"path": str(paths[who].relative_to(common.ROOT)) if paths[who].is_relative_to(common.ROOT)
                         else str(paths[who]), "sha256": shas[who], "frozen_at": snap.get("frozen_at"),
                         "base_times_hash": snap.get("base_times_hash"), "base_times_ref": snap.get("base_times_ref")},
            "capture": {"matched": captures[who] is not None,
                        "fidelity": ((captures[who] or {}).get("fidelity") or {}).get("recomputed_matches_snapshot")},
            "ranking": ranking_block(snap, orders, top3, cfg),
            "speed_quality": speed_block(snap, cfg),
            "diversity": diversity_block(snap, orders, cfg),
            "secondary": secondary_block(snap, result, top3, cfg),
        }
    return {**base, "status": "evaluated", "phase": "forward",
            "frozen_at": snaps["champion"].get("frozen_at"), "config_hash": snaps["champion"].get("config_hash"),
            "top3": top3, "sel_orders_paired": both_sel,
            "probability": probability_block(snaps["champion"], snaps["challenger"], captures, winner,
                                             temperature, temperature_source, cfg),
            "models": models}


# ------------------------------------------------------------------ 累積

def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def _se(values: list[float]) -> float | None:
    return round(statistics.stdev(values) / math.sqrt(len(values)), 6) if len(values) >= 2 else None


def paired(champion: list[float | None], challenger: list[float | None], direction: str) -> dict[str, Any]:
    """同じレースで両方そろった値だけの差（challenger − champion）。勝ち負けは数えるだけで判定しない。"""
    pairs = [(a, b) for a, b in zip(champion, challenger) if a is not None and b is not None]
    deltas = [b - a for a, b in pairs]
    higher = sum(1 for d in deltas if d > 0)
    lower = sum(1 for d in deltas if d < 0)
    out = {"n": len(pairs), "direction": direction,
           "champion_mean": _mean([a for a, _ in pairs]), "challenger_mean": _mean([b for _, b in pairs]),
           "mean_delta_challenger_minus_champion": _mean(deltas), "se_delta": _se(deltas),
           "ties": len(deltas) - higher - lower}
    if direction == DESCRIPTIVE:
        return {**out, "challenger_higher": higher, "challenger_lower": lower}
    better, worse = (lower, higher) if direction == LOWER_IS_BETTER else (higher, lower)
    return {**out, "challenger_better": better, "champion_better": worse}


def calibration_buckets(rows: list[dict[str, Any]], key: str, edges: list[float]) -> list[dict[str, Any]]:
    buckets = [{"lo": lo, "hi": hi, "n": 0, "sum_p": 0.0, "wins": 0} for lo, hi in zip(edges, edges[1:])]
    for row in rows:
        for horse in row["probability"].get("horses") or []:
            p = horse.get(key)
            if p is None:
                continue
            for i, b in enumerate(buckets):
                last = i == len(buckets) - 1
                if b["lo"] <= p < b["hi"] or (last and p == b["hi"]):
                    b["n"] += 1
                    b["sum_p"] += p
                    b["wins"] += horse["y"]
                    break
    return [{"lo": b["lo"], "hi": b["hi"], "n": b["n"],
             "mean_p": round(b["sum_p"] / b["n"], 6) if b["n"] else None,
             "observed_win_rate": round(b["wins"] / b["n"], 6) if b["n"] else None} for b in buckets]


def _speed_summary(rows: list[dict[str, Any]], who: str) -> dict[str, Any]:
    def agg(sub: list[dict[str, Any]]) -> dict[str, Any]:
        sq = [r["models"][who]["speed_quality"] for r in sub]
        sq = [s for s in sq if s.get("status") == "available"]
        qualified = sum(int(s.get("qualified_horses") or 0) for s in sq)
        total = sum(int(s.get("total_horses") or 0) for s in sq)
        used = sum(1 for s in sq if s.get("used"))
        return {"races": len(sq), "used_races": used, "used_rate": round(used / len(sq), 4) if sq else None,
                "qualified_horses": qualified, "total_horses": total,
                "qualified_horse_rate": round(qualified / total, 4) if total else None,
                "raw_available_horses": sum(int(s.get("raw_available_horses") or 0) for s in sq),
                "mean_coverage": _mean([float(s.get("coverage") or 0.0) for s in sq])}
    surfaces = sorted({r["models"][who]["speed_quality"].get("surface") or "unknown" for r in rows})
    return {"all": agg(rows),
            "by_surface": {s: agg([r for r in rows if (r["models"][who]["speed_quality"].get("surface") or "unknown") == s])
                           for s in surfaces}}


def _diversity_values(row: dict[str, Any], who: str) -> dict[str, float | None]:
    d = row["models"][who]["diversity"]
    if len(d.get("active_chars") or []) < 2:
        return {"axis_agreement": None, "exposure_ratio": None, "spearman": None, "jaccard": None}
    sp = [v for v in (d.get("pairwise_rank_spearman") or {}).values() if v is not None]
    jc = [v for v in (d.get("bet_horse_jaccard") or {}).values() if v is not None]
    return {"axis_agreement": float(d["max_axis_agreement"]), "exposure_ratio": d.get("dominant_axis_exposure_ratio"),
            "spearman": statistics.fmean(sp) if sp else None, "jaccard": statistics.fmean(jc) if jc else None}


def _diversity_summary(rows: list[dict[str, Any]], who: str, cfg: dict[str, Any]) -> dict[str, Any]:
    ds = [r["models"][who]["diversity"] for r in rows]
    pair_keys = sorted({k for d in ds for k in (d.get("pairwise_rank_spearman") or {})})
    return {
        "races": len(ds),
        "mean_axis_agreement": _mean([float(d["max_axis_agreement"]) for d in ds if d.get("active_chars")]),
        "axis_all_same_races": sum(1 for d in ds if d.get("axis_all_same")),
        "mean_dominant_axis_exposure_ratio": _mean([d["dominant_axis_exposure_ratio"] for d in ds
                                                    if d.get("dominant_axis_exposure_ratio") is not None]),
        "pairwise_rank_spearman": {k: _mean([d["pairwise_rank_spearman"][k] for d in ds
                                             if (d.get("pairwise_rank_spearman") or {}).get(k) is not None])
                                   for k in pair_keys},
        "bet_horse_jaccard": {k: _mean([d["bet_horse_jaccard"][k] for d in ds
                                        if (d.get("bet_horse_jaccard") or {}).get(k) is not None]) for k in pair_keys},
    }


def _secondary_summary(rows: list[dict[str, Any]], who: str, cfg: dict[str, Any]) -> dict[str, Any]:
    def money(cells: list[dict[str, Any]]) -> dict[str, Any]:
        bets = [c for c in cells if c.get("status") == "bet"]
        spent = sum(c["spent"] for c in bets)
        payout = sum(c["payout"] for c in bets)
        hits = sum(1 for c in bets if c["hit"])
        return {"cards": len(bets), "passes": sum(1 for c in cells if c.get("status") == "pass"),
                "hits": hits, "hit_rate": round(hits / len(bets), 4) if bets else None,
                "spent": spent, "payout": payout, "roi": round(payout / spent, 4) if spent else None}

    secs = [r["models"][who]["secondary"] for r in rows]
    errors = sum(1 for s in secs if s.get("status") != "evaluated")
    secs = [s for s in secs if s.get("status") == "evaluated"]
    chars = cfg["secondary"]["fixed_three"]
    race_payouts = [sum(s["fixed_three"][c].get("payout", 0) for c in chars if s["fixed_three"][c].get("status") == "bet")
                    for s in secs]
    total_payout = sum(race_payouts)
    conv = [v for s in secs for v in s["conversion"].values()]
    return {
        "note": "Secondary。少数レースでは分散が大きいので判定には使わない。",
        "races": len(secs),
        "not_settled_races": errors,
        "fixed_three": money([s["fixed_three"][c] for s in secs for c in chars]),
        "by_char": {c: money([s["fixed_three"][c] for s in secs]) for c in chars},
        "auto_chappy": money([s["auto_chappy"] for s in secs]),
        # 払戻が1レースに偏っていないか（大当たり1回で勝ちと読まないための目印）
        "largest_single_race_payout_share": round(max(race_payouts) / total_payout, 4) if total_payout else None,
        "conversion_loss": {
            "cards": len(conv),
            "winning_pair_present_but_not_ticketed": sum(1 for c in conv if c["winning_pair_present_but_not_ticketed"]),
            "winning_trio_selected_but_not_ticketed": sum(1 for c in conv if c["winning_trio_selected_but_not_ticketed"]),
        },
        "winning_coverage": {
            "wide_pairs_covered": sum(s["winning_coverage"]["wide_pairs_covered"] for s in secs),
            "wide_pairs_total": sum(s["winning_coverage"]["wide_pairs_total"] for s in secs),
            "umaren_covered_races": sum(1 for s in secs if s["winning_coverage"]["umaren_covered"]),
            "trio_covered_races": sum(1 for s in secs if s["winning_coverage"]["trio_covered"]),
        },
    }


def summarize(evaluated: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    prob_rows = [r for r in evaluated if r["probability"]["status"] == "evaluated"]
    prob = {who: [r["probability"]["models"][who] for r in prob_rows] for who in ("champion", "challenger")}
    # 市場 q は出走馬全員の正の単勝オッズが揃ったレースだけ（基準線と calibration を同じ集合にそろえる）
    market_rows = [r for r in prob_rows if r["probability"]["market"]["status"] == "available"]
    market = [r["probability"]["market"] for r in market_rows]
    edges = cfg["probability"]["calibration_bucket_edges"]
    ks = cfg["ranking"]["recall_k"]

    def recall_values(who: str, source: str, k: int) -> list[float | None]:
        out = []
        for r in evaluated:
            row = r["models"][who]["ranking"]["order_recall"].get(source) or {}
            out.append(row.get(f"at{k}") if row.get("status") == "available" else None)
        return out

    def card_recall(who: str, char: str) -> list[float | None]:
        return [(r["models"][who]["ranking"]["card_selection_recall"].get(char) or {}).get("recall") for r in evaluated]

    ranking_paired = {f"{source}_recall@{k}": paired(recall_values("champion", source, k),
                                                     recall_values("challenger", source, k), HIGHER_IS_BETTER)
                      for source in cfg["ranking"]["order_sources"] for k in ks}
    ranking_paired.update({f"{char}_card_selection_recall": paired(card_recall("champion", char),
                                                                  card_recall("challenger", char), HIGHER_IS_BETTER)
                           for char in cfg["ranking"]["card_selection_chars"]})
    div_values = {who: [_diversity_values(r, who) for r in evaluated] for who in ("champion", "challenger")}
    return {
        "races": len(evaluated),
        "primary": {
            "probability": {
                "races": len(prob_rows),
                "score_sources": sorted({r["probability"]["score_source"] for r in prob_rows}),
                "temperature_sources": sorted({r["probability"]["temperature_source"] for r in prob_rows}),
                "not_evaluated": [{"race_id": r["race_id"], "reason": r["probability"]["status"]}
                                  for r in evaluated if r["probability"]["status"] != "evaluated"],
                "log_loss": paired([m["log_loss"] for m in prob["champion"]], [m["log_loss"] for m in prob["challenger"]],
                                   LOWER_IS_BETTER),
                "brier": paired([m["brier"] for m in prob["champion"]], [m["brier"] for m in prob["challenger"]], LOWER_IS_BETTER),
                "q_market_reference": {"races": len(market),
                                       "log_loss": _mean([m["log_loss"] for m in market]),
                                       "brier": _mean([m["brier"] for m in market]),
                                       "note": "参考の基準線。出走馬全員の正の単勝オッズが揃うレースだけ。"},
                "calibration_buckets": {
                    "champion": calibration_buckets(prob_rows, "p_champion", edges),
                    "challenger": calibration_buckets(prob_rows, "p_challenger", edges),
                    "q_market": calibration_buckets(market_rows, "p_q_market", edges),
                },
            },
            "ranking": ranking_paired,
            "speed_quality": {who: _speed_summary(evaluated, who) for who in ("champion", "challenger")},
            "diversity": {
                **{who: _diversity_summary(evaluated, who, cfg) for who in ("champion", "challenger")},
                "paired": {metric: paired([v[metric] for v in div_values["champion"]],
                                          [v[metric] for v in div_values["challenger"]], DESCRIPTIVE)
                           for metric in ("axis_agreement", "exposure_ratio", "spearman", "jaccard")},
                "note": "多様性は高ければ良いとは限らない。speed 復活で本来のキャラ差が自然に戻るかを見る観察値。",
            },
        },
        "secondary": {who: _secondary_summary(evaluated, who, cfg) for who in ("champion", "challenger")},
    }


def run(race_results: dict[str, Any], champion_dir: Path | None = None, challenger_root: Path | None = None,
        capture_dir: Path | None = None, output_dir: Path | None = None, cfg: dict[str, Any] | None = None,
        registry: dict[str, Any] | None = None, temperature: float | None = None,
        current_config_hash: str | None = None, now: datetime.datetime | None = None) -> dict[str, Any]:
    champion_dir = champion_dir or snapshots.SNAPSHOT_DIR
    challenger_root = challenger_root or CHALLENGER_ROOT
    cfg = cfg or load_config()
    capture_dir = capture_dir or capture_dir_for(cfg)
    champion, challenger = model_specs(cfg, registry)
    start = forward_start(cfg, challenger)
    output_dir = output_dir or EVAL_DIR / challenger["id"]
    if temperature is None:
        temperature = float((common.read_json(common.ROOT / "config" / "myomi.json") or {})["prob_model"]["temperature"])
    current_config_hash = current_config_hash or model_registry.config_hash()
    chall_dir = snapshot_dir_for(challenger, champion_dir, challenger_root)

    race_ids = sorted({p.stem for d in (champion_dir, chall_dir) if d.is_dir() for p in d.glob("*.json")})
    evaluated, not_evaluated, pre_registration, pending, errors = [], [], [], [], []
    for race_id in race_ids:
        paths = {"champion": champion_dir / f"{race_id}.json", "challenger": chall_dir / f"{race_id}.json"}
        snaps = {who: common.read_json(p) for who, p in paths.items()}
        ref = next((s for s in snaps.values() if isinstance(s, dict)), {}) or {}
        post = common.post_at(race_id, ref.get("post_time"))
        if post is None:
            not_evaluated.append({"race_id": race_id, "reason": "unknown_post_time"})
            continue
        if post <= start:
            pre_registration.append(race_id)
            continue
        result = race_results.get(race_id) or {}
        if len(result.get("finish") or []) < 3:
            pending.append(race_id)
            continue
        if not isinstance(snaps["champion"], dict) or not common.is_model_race(snaps["champion"]):
            not_evaluated.append({"race_id": race_id, "reason": "champion_snapshot_missing_or_not_prerace"})
            continue
        if not isinstance(snaps["challenger"], dict):
            row = {"race_id": race_id, "reason": "challenger_missing"}
        elif not common.is_model_race(snaps["challenger"]):
            row = {"race_id": race_id, "reason": "challenger_not_prerace"}
        else:
            try:
                row = evaluate_race(race_id, post, snaps, paths, result, challenger, capture_dir,
                                    temperature, current_config_hash, cfg)
            except Exception as exc:  # noqa: BLE001 — 1レースの失敗で全体を止めないが、黙らない
                logger.error("%s: model evaluation failed: %s: %s", race_id, type(exc).__name__, exc)
                errors.append({"race_id": race_id, "error": f"{type(exc).__name__}: {exc}"})
                row = {"race_id": race_id, "reason": "evaluation_error"}
        if row.get("status") == "evaluated":
            evaluated.append(row)
        else:
            not_evaluated.append({"race_id": race_id, "reason": row["reason"]})
            row = {"schema": RACE_SCHEMA, "race_id": race_id, "status": "not_evaluated", "reason": row["reason"],
                   "official_results_untouched": True, "used_for_prediction": False}
        common.write_json(output_dir / "races" / f"{race_id}.json", row)

    eligible = len(evaluated) + len(not_evaluated)
    summary = {
        "schema": SUMMARY_SCHEMA,
        "generated_at": (now or datetime.datetime.now(common.JST)).isoformat(timespec="seconds"),
        "evaluation_version": cfg.get("version"),
        "evaluation_config_hash": common.config_hash(cfg),
        "spec": cfg.get("spec"),
        "champion_model_id": champion["id"],
        "challenger_model_id": challenger["id"],
        "challenger_base_times": challenger.get("base_times"),
        "forward_start": start.isoformat(timespec="seconds"),
        "official_results_untouched": True,
        "used_for_prediction": False,
        "promotion": cfg.get("promotion"),
        "note": "Champion と speed-v2 Challenger を同じレース・同じ発走前情報で対にした比較。"
                "昇格の判定はしない。Secondary（成績）は判定に使わない。",
        "coverage": {
            "forward_races_with_result": eligible,
            "evaluated_pairs": len(evaluated),
            "coverage_rate": round(len(evaluated) / eligible, 4) if eligible else None,
            "not_evaluated": not_evaluated,
            "excluded_pre_registration": pre_registration,
            "pending_result": pending,
        },
        "forward": summarize(evaluated, cfg),
        "errors": errors,
    }
    common.write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Champion / Challenger の forward 評価（evaluation-only）")
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="発走前に両モデルの評価用入力を記録する")
    cap.add_argument("--week", required=True)
    cap.add_argument("--phase", default="pipeline")
    ev = sub.add_parser("evaluate", help="結果確定後に対の評価と累積を書く")
    ev.add_argument("--results", default=str(common.ROOT / "data" / "race_results.json"))
    for command in (cap, ev):
        # 評価式ごとの設定ファイル。省略時は speed-v2 の評価（config/model_evaluation.json）
        command.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    cfg = load_config(Path(args.config))

    if args.command == "capture":
        raw = common.read_json(bp.RAW_DIR / f"{args.week}.json")
        if not isinstance(raw, dict):
            logger.warning("raw/%s.json が無いため評価用の発走前記録をスキップします", args.week)
            return
        report = capture(raw, phase=args.phase, cfg=cfg)
        print(f"model evaluation capture: added={len(report['added'])} skipped={len(report['skipped'])}")
        return

    race_results = common.read_json(Path(args.results))
    if not isinstance(race_results, dict):
        logger.warning("%s を読めないため評価をスキップします", args.results)
        return
    summary = run(race_results, cfg=cfg)
    print(json.dumps({"evaluated_pairs": summary["coverage"]["evaluated_pairs"],
                      "not_evaluated": len(summary["coverage"]["not_evaluated"]),
                      "errors": len(summary["errors"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
