"""発走前の監査 manifest（odds provenance / audit evidence v2）。

「その予想・買い目が、いつ取得された、何時点の、どの市場価格を使って作られたか」を
後から確かめられるように、発走前の観測ごとに次を SHA-256 付きで1ファイルへ束ねる。

  - Prediction snapshot（data/snapshots/{race_id}.json）… frozen_at
  - Context snapshot（data/context_snapshots/{race_id}/…）… observed_at
  - Odds observation（data/odds_history/{race_id}/…）… observed_at と、券種別の source_time

--- 時刻3種（混同しない）---
  observed_at … こちら（GitHub Actions）が HTTP 取得を終えた時刻。「いつ取りに行ったか」の証拠。
  source_time … netkeiba のレスポンスに入っていた official_datetime そのもの
                （provider returned official_datetime）。**価格の鮮度はこちらで判定する。**
                これが本当に「その券種の価格の更新時刻」を意味するかは、まだ完全には確かめていない。
  post_at     … 発走予定時刻（race_id の日付＋post_time、JST）。

source_time が無いときは freshness_basis=unavailable / status=unknown にする。
observed_at を価格時刻として代用しない（15:29 に取っても、中身が 14:40 の価格ならそれは 14:40 の価格）。

--- 価格の鮮度（監査用の分類。予想にはつながない）---
  発走まで 5〜15分 … target_window   5分未満 … very_late
  15〜30分       … acceptable      30分超 … stale
  source_time 不明 … unknown        発走後 … after_post
  その券種を取れていない … missing（理由は fetch_status: fetch_failed / empty / unparsed）
  その観測では取りに行っていない券種 … not_in_scope（直前の単勝だけの観測など）

--- 予想に使った価格（prediction_price_basis）---
通常pipelineは「rawを作る（オッズ取得）→ odds観測を保存 → 予想を作る」の順で動く。
そこで、予想 snapshot の frozen_at 以前で最後の pipeline 観測を「その予想が使った価格」とみなし、
単勝オッズが snapshot の印のオッズと一致するかも確かめて残す（prediction_odds_match）。
直前の単勝だけの観測（late）は、予想を作り直さないので basis にはしない。

--- 守ること ---
- 予想・選定・買い目・妙味・鳳ゲート・予算には一切つなげない（audit_only）。stale でも止めない。
- 1観測1ファイルで追記し、過去の manifest を書き換えない。
- 発走時刻を過ぎたら新しい pre-race manifest を作らない。発走後の時刻を持つ観測は証跡に入れない。
- これはリポジトリ内の整合性を確かめる内部の監査証跡で、外部タイムスタンプ機関による第三者証明ではない。

PR #12（context-audit-manifest-v1）を参考に、最新の main 上で作り直したもの。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from logic import odds_history, snapshots
from scraper.fetchers import b2_odds

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "raw"
CONFIG_PATH = ROOT / "config" / "audit_manifest.json"
PREDICTION_SNAPSHOT_DIR = ROOT / "data" / "snapshots"
CONTEXT_SNAPSHOT_DIR = ROOT / "data" / "context_snapshots"
ODDS_HISTORY_DIR = ROOT / "data" / "odds_history"
MANIFEST_DIR = ROOT / "data" / "audit_manifests"
JST = snapshots.JST

MANIFEST_VERSION = "audit-manifest-v2"
BASIS_PHASE = "pipeline"

TARGET_WINDOW = "target_window"
VERY_LATE = "very_late"
ACCEPTABLE = "acceptable"
STALE = "stale"
UNKNOWN = "unknown"
AFTER_POST = "after_post"
MISSING = "missing"
NOT_IN_SCOPE = "not_in_scope"
FRESH_STATUSES = (TARGET_WINDOW, VERY_LATE, ACCEPTABLE)

TIME_DEFINITIONS = {
    "observed_at": "こちら（GitHub Actions）がHTTP取得を終えた時刻。いつ取りに行ったかの証拠で、価格の時刻ではない",
    "source_time": "provider returned official_datetime（netkeibaが返した official_datetime をそのまま保存）。価格の鮮度はこれで判定する",
    "post_at": "発走予定時刻（JST）",
    "frozen_at": "予想 snapshot のビルド時刻",
}
SOURCE_TIME_CAVEAT = (
    "netkeiba の official_datetime が本当に『その券種の価格の更新時刻』を意味するかは、"
    "まだ完全には確かめていない。source_time = provider returned official_datetime として保存・監査しているだけ"
)


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as f:
        return json.load(f)


def _parse_dt(value: Any) -> datetime.datetime | None:
    # official_datetime（"2026-10-04 14:40:27"、tz無し＝JST）も ISO 形式もこれで読める
    return odds_history.parse_source_time(value)


def _minutes_to_post(post_at: datetime.datetime, at: datetime.datetime | None) -> float | None:
    if at is None:
        return None
    return round((post_at - at).total_seconds() / 60.0, 3)


def _freshness_cfg(config: dict[str, Any] | None) -> dict[str, float]:
    cfg = (config or {}).get("freshness") or {}
    return {
        "target_min_minutes": float(cfg.get("target_min_minutes", 5)),
        "target_max_minutes": float(cfg.get("target_max_minutes", 15)),
        "stale_after_minutes": float(cfg.get("stale_after_minutes", 30)),
    }


def classify_freshness(source_time: Any, post_at: datetime.datetime,
                       config: dict[str, Any] | None = None) -> dict[str, Any]:
    """source_time（provider の official_datetime）と発走時刻の差で価格の鮮度を分類する。

    source_time が読めなければ unknown。observed_at では**代用しない**。
    """
    th = _freshness_cfg(config)
    source_at = _parse_dt(source_time)
    if source_at is None:
        return {
            "status": UNKNOWN,
            "freshness_basis": "unavailable",
            "source_time": source_time or None,
            "minutes_to_post": None,
            "fresh": False,
            "target_window": False,
        }
    minutes = _minutes_to_post(post_at, source_at)
    if source_at >= post_at:
        status = AFTER_POST
    elif minutes < th["target_min_minutes"]:
        status = VERY_LATE
    elif minutes <= th["target_max_minutes"]:
        status = TARGET_WINDOW
    elif minutes <= th["stale_after_minutes"]:
        status = ACCEPTABLE
    else:
        status = STALE
    return {
        "status": status,
        "freshness_basis": "source_time",
        "source_time": source_time,
        "minutes_to_post": minutes,
        "fresh": status in FRESH_STATUSES,
        "target_window": status == TARGET_WINDOW,
    }


def _not_classified(status: str, reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "freshness_basis": "unavailable",
        "reason": reason,
        "source_time": None,
        "minutes_to_post": None,
        "fresh": False,
        "target_window": False,
    }


def market_evidence(record: dict[str, Any] | None, post_at: datetime.datetime,
                    config: dict[str, Any] | None = None) -> dict[str, Any]:
    """券種1つぶんの証跡（取得時刻・元データ時刻・取得状態・鮮度）。"""
    if not record:
        # 券種別の記録が無い旧形式の観測。式別の価格時刻は証明できない
        return {
            "source_time": None, "observed_at": None, "fetch_status": "provenance_unavailable",
            "rows": None,
            "freshness": _not_classified(UNKNOWN, "provenance_unavailable"),
        }
    fetch_status = record.get("status")
    out = {
        "source_time": record.get("source_time"),
        "observed_at": record.get("observed_at"),
        "observed_minutes_to_post": _minutes_to_post(post_at, _parse_dt(record.get("observed_at"))),
        "fetch_status": fetch_status,
        "rows": record.get("rows"),
    }
    if fetch_status == b2_odds.STATUS_NOT_FETCHED:
        out["freshness"] = _not_classified(NOT_IN_SCOPE, fetch_status)
    elif fetch_status != b2_odds.STATUS_OK:
        out["freshness"] = _not_classified(MISSING, fetch_status or "unknown_status")
    else:
        out["freshness"] = classify_freshness(record.get("source_time"), post_at, config)
    return out


def _odds_markets(payload: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """観測ファイルから券種別の記録を取り出す。旧形式は単勝だけを observation 本体から復元する。"""
    meta = payload.get("market_meta")
    if isinstance(meta, dict) and meta:
        return {key: meta.get(key) for key in b2_odds.MARKET_KEYS}
    rows = len(payload.get("odds") or [])
    legacy_win = {
        "source_time": payload.get("source_time"),
        "observed_at": payload.get("observed_at"),
        "status": b2_odds.STATUS_OK if rows else b2_odds.STATUS_EMPTY,
        "rows": rows,
    }
    return {key: (legacy_win if key == b2_odds.MARKET_WIN else None)
            for key in b2_odds.MARKET_KEYS}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# ---- 発走前の成果物を選ぶ（すべて fail-closed）---------------------------------

def select_prediction(race_id: str, post_at: datetime.datetime, as_of: datetime.datetime,
                      directory: Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    path = snapshots.snapshot_path(race_id, directory or PREDICTION_SNAPSHOT_DIR)
    payload = _read_json(path)
    if not payload or payload.get("pre_race") is not True:
        return None
    frozen_at = _parse_dt(payload.get("frozen_at"))
    if frozen_at is None or frozen_at >= post_at or frozen_at > as_of:
        return None
    return path, payload


def select_context(race_id: str, post_at: datetime.datetime, as_of: datetime.datetime,
                   directory: Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    race_dir = (directory or CONTEXT_SNAPSHOT_DIR) / race_id
    if not race_dir.is_dir():
        return None
    candidates = []
    for path in sorted(race_dir.glob("*.json")):
        payload = _read_json(path)
        if not payload or payload.get("pre_race") is not True:
            continue
        observed = _parse_dt(payload.get("observed_at"))
        if observed is None or observed >= post_at or observed > as_of:
            continue
        candidates.append((observed, path, payload))
    if not candidates:
        return None
    _, path, payload = max(candidates, key=lambda row: row[0])
    return path, payload


def _is_pre_race_odds(payload: dict[str, Any], post_at: datetime.datetime,
                      as_of: datetime.datetime) -> datetime.datetime | None:
    """発走前の観測と言えるなら observed_at を返す。どれか1つでも発走後の時刻を持てば除外。"""
    observed = _parse_dt(payload.get("observed_at"))
    if observed is None or observed >= post_at or observed > as_of:
        return None
    times = [payload.get("source_time")]
    for record in (payload.get("market_meta") or {}).values():
        times += [(record or {}).get("source_time"), (record or {}).get("observed_at")]
    for value in times:
        at = _parse_dt(value)
        if at is not None and at >= post_at:
            return None
    return observed


def pre_race_odds(race_id: str, post_at: datetime.datetime, as_of: datetime.datetime,
                  directory: Path | None = None) -> list[tuple[datetime.datetime, Path, dict[str, Any]]]:
    race_dir = (directory or ODDS_HISTORY_DIR) / race_id
    if not race_dir.is_dir():
        return []
    rows = []
    for path in sorted(race_dir.glob("*.json")):
        payload = _read_json(path)
        if not payload:
            continue
        observed = _is_pre_race_odds(payload, post_at, as_of)
        if observed is not None:
            rows.append((observed, path, payload))
    return sorted(rows, key=lambda row: row[0])


# ---- 証跡ブロック ---------------------------------------------------------------

def _odds_artifact(path: Path, payload: dict[str, Any], post_at: datetime.datetime,
                   config: dict[str, Any] | None) -> dict[str, Any]:
    observed = _parse_dt(payload.get("observed_at"))
    markets = _odds_markets(payload)
    return {
        "path": _display_path(path),
        "sha256": _sha256(path),
        "phase": payload.get("phase"),
        "observed_at": payload.get("observed_at"),
        "observed_minutes_to_post": _minutes_to_post(post_at, observed),
        # 後方互換の source_time は単勝のもの
        "source_time": payload.get("source_time"),
        "source_time_market": b2_odds.MARKET_WIN,
        "market_scope": payload.get("market_scope") or odds_history.SCOPE_LEGACY,
        "rows": len(payload.get("odds") or []),
        "markets": {key: market_evidence(markets[key], post_at, config) for key in b2_odds.MARKET_KEYS},
    }


def _prediction_artifact(path: Path, payload: dict[str, Any],
                         post_at: datetime.datetime) -> dict[str, Any]:
    return {
        "path": _display_path(path),
        "sha256": _sha256(path),
        "frozen_at": payload.get("frozen_at"),
        "minutes_to_post": _minutes_to_post(post_at, _parse_dt(payload.get("frozen_at"))),
        "model_id": payload.get("model_id"),
        "config_hash": payload.get("config_hash"),
    }


def _context_artifact(path: Path, payload: dict[str, Any],
                      post_at: datetime.datetime) -> dict[str, Any]:
    return {
        "path": _display_path(path),
        "sha256": _sha256(path),
        "phase": payload.get("phase"),
        "observed_at": payload.get("observed_at"),
        "minutes_to_post": _minutes_to_post(post_at, _parse_dt(payload.get("observed_at"))),
    }


def _prediction_odds_match(prediction: dict[str, Any], odds_payload: dict[str, Any]) -> bool | None:
    """予想 snapshot の印のオッズと、basis 観測の単勝オッズが一致するか。比べられなければ None。"""
    marks = {m.get("num"): m.get("odds") for m in prediction.get("marks") or []
             if m.get("num") is not None and m.get("odds") is not None}
    observed = {row.get("num"): row.get("win_odds") for row in odds_payload.get("odds") or []
                if row.get("num") is not None and row.get("win_odds") is not None}
    if not marks or not observed:
        return None
    return all(num in observed and abs(float(observed[num]) - float(odds)) < 1e-9
               for num, odds in marks.items())


def _price_basis(prediction: tuple[Path, dict[str, Any]] | None,
                 odds_rows: list[tuple[datetime.datetime, Path, dict[str, Any]]],
                 post_at: datetime.datetime, config: dict[str, Any] | None) -> dict[str, Any]:
    rule = "latest_pipeline_odds_observation_at_or_before_prediction_frozen_at"
    if prediction is None:
        return {"status": "no_prediction", "link_rule": rule, "odds": None}
    _, payload = prediction
    frozen_at = _parse_dt(payload.get("frozen_at"))
    candidates = [row for row in odds_rows
                  if row[2].get("phase") == BASIS_PHASE and frozen_at is not None
                  and row[0] <= frozen_at]
    if not candidates:
        return {"status": "no_pipeline_odds_observation", "link_rule": rule, "odds": None}
    _, path, odds_payload = candidates[-1]
    return {
        "status": "linked",
        "link_rule": rule,
        "prediction_odds_match": _prediction_odds_match(payload, odds_payload),
        "odds": _odds_artifact(path, odds_payload, post_at, config),
    }


def card_price_evidence(prediction: dict[str, Any] | None, basis: dict[str, Any],
                        post_at: datetime.datetime,
                        config: dict[str, Any] | None = None) -> dict[str, Any]:
    """各カードが買った券種について、その価格が何時点のものだったか。

    カード生成・EV・鳳ゲートは変えない。価格が古い／不明でも、その事実を記録するだけ。
    """
    basis_markets = ((basis.get("odds") or {}).get("markets")) or {}

    def _market(key: str) -> dict[str, Any]:
        if key in basis_markets:
            return basis_markets[key]
        return {"source_time": None, "fetch_status": "basis_unavailable",
                "freshness": _not_classified(UNKNOWN, "basis_unavailable")}

    markets = {key: _market(key) for key in b2_odds.MARKET_KEYS}
    cards = []
    for card in (prediction or {}).get("cards") or []:
        bet_types = sorted({bet.get("type") for bet in card.get("bets") or [] if bet.get("type")})
        used = {}
        for bet_type in bet_types:
            key = b2_odds.MARKET_BY_BET_TYPE.get(bet_type)
            if key is None:
                continue
            evidence = markets[key]
            used[key] = {
                "bet_type": bet_type,
                "source_time": evidence.get("source_time"),
                "freshness": (evidence.get("freshness") or {}).get("status"),
            }
        not_fresh = sorted(k for k, v in used.items() if v["freshness"] not in FRESH_STATUSES)
        cards.append({
            "char": card.get("char"),
            "market_ev_computed": bool(card.get("market_ev")),
            # p・q（勝率と市場確率）の土台は単勝
            "pq_basis_win": {
                "source_time": markets[b2_odds.MARKET_WIN].get("source_time"),
                "freshness": (markets[b2_odds.MARKET_WIN].get("freshness") or {}).get("status"),
            },
            "markets_used": used,
            "prices_not_fresh": not_fresh,
        })
    otori_card = next((c for c in cards if c["char"] == "otori"), None)
    return {
        "basis_status": basis.get("status"),
        "markets": {key: {"source_time": markets[key].get("source_time"),
                          "fetch_status": markets[key].get("fetch_status"),
                          "freshness": (markets[key].get("freshness") or {}).get("status")}
                    for key in b2_odds.MARKET_KEYS},
        "cards": cards,
        "otori": {
            "legendary": (prediction or {}).get("legendary"),
            "card_present": otori_card is not None,
            "otori_card_ev_recorded": (prediction or {}).get("otori_card_ev") is not None,
            "prices_not_fresh": (otori_card or {}).get("prices_not_fresh", []),
            "note": "記録のみ。EV の再計算や鳳ゲートの変更はしない",
        },
    }


def _context_completeness(payload: dict[str, Any] | None) -> dict[str, Any]:
    context = (payload or {}).get("context_layers") or {}
    layer2 = context.get("layer2") or {}
    horses = layer2.get("horses") or []
    current = [row for row in horses if ((row.get("body_weight") or {}).get("current") is not None)]
    total = len(horses)
    return {
        "horse_rows": total,
        "body_weight_current": len(current),
        "body_weight_current_coverage": round(len(current) / total, 4) if total else None,
        "track_metrics_present": layer2.get("track_metrics") is not None,
        "odds_movement_horses": len(((layer2.get("odds_movement") or {}).get("horses") or [])),
        "paddock_observed": ((context.get("layer3") or {}).get("paddock")) is not None,
    }


def _speed(prediction: dict[str, Any] | None) -> dict[str, Any] | None:
    quality = (prediction or {}).get("speed_quality")
    if not isinstance(quality, dict):
        return None
    return {key: quality.get(key) for key in
            ("used", "coverage", "qualified_horses", "total_horses", "reason")}


def collect_evidence(race_id: str, post_at: datetime.datetime, as_of: datetime.datetime,
                     prediction_directory: Path | None = None,
                     context_directory: Path | None = None,
                     odds_directory: Path | None = None,
                     config: dict[str, Any] | None = None) -> dict[str, Any]:
    """as_of 時点（かつ発走前）に存在した成果物だけで証跡を組み立てる。"""
    prediction = select_prediction(race_id, post_at, as_of, prediction_directory)
    context = select_context(race_id, post_at, as_of, context_directory)
    odds_rows = pre_race_odds(race_id, post_at, as_of, odds_directory)

    pred_info = _prediction_artifact(*prediction, post_at) if prediction else None
    context_info = _context_artifact(*context, post_at) if context else None
    latest_odds = None
    if odds_rows:
        _, path, payload = odds_rows[-1]
        latest_odds = _odds_artifact(path, payload, post_at, config)

    # 一番新しい観測の単勝価格の鮮度。式別は markets / card_price_evidence を見る
    win_freshness = (
        ((latest_odds or {}).get("markets") or {}).get(b2_odds.MARKET_WIN, {}).get("freshness")
        or classify_freshness(None, post_at, config)
    )
    basis = _price_basis(prediction, odds_rows, post_at, config)
    completeness = _context_completeness(context[1] if context else None)
    completeness.update({
        "prediction_snapshot_present": pred_info is not None,
        "context_snapshot_present": context_info is not None,
        "odds_snapshot_present": latest_odds is not None,
    })
    return {
        "artifacts": {"prediction": pred_info, "context": context_info, "odds": latest_odds},
        "odds_freshness": {"market": b2_odds.MARKET_WIN, **win_freshness},
        "prediction_price_basis": basis,
        "card_price_evidence": card_price_evidence(
            prediction[1] if prediction else None, basis, post_at, config
        ),
        "context_completeness": completeness,
        "speed": _speed(prediction[1] if prediction else None),
    }


def _header(race: dict[str, Any], post_at: datetime.datetime,
            config: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "version": MANIFEST_VERSION,
        "mode": "audit_only",
        "race_id": race.get("id"),
        "post_time": race.get("post_time"),
        "post_at": post_at.isoformat(timespec="seconds"),
        "time_definitions": TIME_DEFINITIONS,
        "source_time_caveat": SOURCE_TIME_CAVEAT,
        "proof_scope": "internal_hash_manifest",
        "freshness_rules": {"basis": "source_time", **_freshness_cfg(config)},
    }


def build_manifest(race: dict[str, Any], now: datetime.datetime, phase: str,
                   prediction_directory: Path | None = None,
                   context_directory: Path | None = None,
                   odds_directory: Path | None = None,
                   config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """発走前の1観測ぶんの manifest。発走時刻を過ぎていたら作らない（None）。"""
    post_at = snapshots.post_datetime(race)
    if post_at is None or now >= post_at:
        return None
    manifest = _header(race, post_at, config)
    manifest.update({
        "observed_at": now.isoformat(timespec="seconds"),
        "phase": phase,
        "pre_race": True,
    })
    manifest.update(collect_evidence(
        race.get("id") or "", post_at, now,
        prediction_directory, context_directory, odds_directory, config,
    ))
    return manifest


def evidence_from_files(race: dict[str, Any],
                        prediction_directory: Path | None = None,
                        context_directory: Path | None = None,
                        odds_directory: Path | None = None,
                        config: dict[str, Any] | None = None) -> dict[str, Any]:
    """結果後の監査用。manifest が無いレースも、発走前に存在したファイルだけから証跡を組み直す。

    新しい観測は作らない。発走時刻より前の時刻を持つものだけを選ぶ。
    """
    post_at = snapshots.post_datetime(race)
    if post_at is None:
        return {"status": "unknown_post_time"}
    evidence = collect_evidence(
        race.get("id") or "", post_at, post_at - datetime.timedelta(microseconds=1),
        prediction_directory, context_directory, odds_directory, config,
    )
    return {"status": "derived_from_pre_race_artifacts", **_header(race, post_at, config), **evidence}


def _artifact_hashes(manifest: dict[str, Any]) -> tuple[Any, ...]:
    artifacts = manifest.get("artifacts") or {}
    basis = (manifest.get("prediction_price_basis") or {}).get("odds") or {}
    return tuple((artifacts.get(k) or {}).get("sha256") for k in ("prediction", "context", "odds")) + (
        basis.get("sha256"),
    )


def load_manifests(race_id: str, directory: Path | None = None) -> list[dict[str, Any]]:
    race_dir = (directory or MANIFEST_DIR) / race_id
    if not race_dir.is_dir():
        return []
    rows = [payload for path in sorted(race_dir.glob("*.json")) if (payload := _read_json(path))]
    return sorted(rows, key=lambda row: row.get("observed_at") or "")


def latest_manifest(race_id: str, directory: Path | None = None) -> dict[str, Any] | None:
    """発走前と確認できる最後の manifest。"""
    rows = []
    for payload in load_manifests(race_id, directory):
        post_at = snapshots.post_datetime({"id": race_id, "post_time": payload.get("post_time")})
        observed = _parse_dt(payload.get("observed_at"))
        if (payload.get("pre_race") is True and post_at is not None
                and observed is not None and observed < post_at):
            rows.append(payload)
    return rows[-1] if rows else None


def capture(raw: dict[str, Any], now: datetime.datetime | None = None, phase: str = "audit",
            output_directory: Path | None = None,
            prediction_directory: Path | None = None,
            context_directory: Path | None = None,
            odds_directory: Path | None = None,
            config: dict[str, Any] | None = None) -> dict[str, Any]:
    """raw の各レースについて、発走前なら manifest を1ファイル追加する（既存は書き換えない）。"""
    now = now or datetime.datetime.now(JST)
    output_directory = output_directory or MANIFEST_DIR
    config = config or load_config()
    report: dict[str, Any] = {"added": [], "skipped": []}

    for race in raw.get("races") or []:
        race_id = race.get("id")
        if not race_id:
            continue
        manifest = build_manifest(race, now, phase, prediction_directory, context_directory,
                                  odds_directory, config)
        if manifest is None:
            report["skipped"].append({"race_id": race_id, "reason": "not_pre_race"})
            continue
        previous = latest_manifest(race_id, output_directory)
        if previous is not None and _artifact_hashes(previous) == _artifact_hashes(manifest):
            # 束ねる成果物がどれも前回と同じなら、同じ証跡をもう1枚作らない
            report["skipped"].append({"race_id": race_id, "reason": "unchanged"})
            continue
        race_dir = output_directory / race_id
        race_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.astimezone(JST).strftime("%Y%m%dT%H%M%S")
        path = race_dir / f"{stamp}_{phase}.json"
        if path.exists():
            report["skipped"].append({"race_id": race_id, "reason": "duplicate_time"})
            continue
        with path.open("w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.write("\n")
        report["added"].append(race_id)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="発走前の監査manifest（odds provenance）を保存")
    parser.add_argument("--week", required=True)
    parser.add_argument("--phase", default="audit")
    args = parser.parse_args()

    path = RAW_DIR / f"{args.week}.json"
    if not path.exists():
        print(f"{path} がありません。manifest は作りません")
        return
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    report = capture(raw, phase=args.phase)
    print(f"audit manifest: added={len(report['added'])} skipped={len(report['skipped'])}")


if __name__ == "__main__":
    main()
