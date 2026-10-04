"""PR-A：オッズの券種別 provenance と発走前監査 manifest のテスト。

時刻3種（observed_at / source_time / post_at）を混同しないこと、
単勝の鮮度で式別まで新しいと扱わないこと、発走後の観測が発走前の証跡に混ざらないこと、
そして**予想・買い目が一切変わらないこと**を確かめる。
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
from pathlib import Path

from logic import audit_manifest, build_predictions, odds_history, snapshots
from results import anomaly_review
from scraper import capture_late_odds
from scraper.fetchers import b2_odds

JST = odds_history.JST
ROOT = Path(__file__).resolve().parent.parent
CONFIG = audit_manifest.load_config()

TOKYO = "20261004-tokyo-11"


def _at(hour, minute, second=0, day=4):
    return datetime.datetime(2026, 10, day, hour, minute, second, tzinfo=JST)


def _post(race_id=TOKYO, post_time="15:45"):
    return snapshots.post_datetime({"id": race_id, "post_time": post_time})


def _record(source_time, observed_at, status="ok", rows=17, type_code="1"):
    return {
        "source_time": source_time,
        "observed_at": observed_at,
        "source_ref": {"race_id": "202605040211", "type": type_code},
        "status": status,
        "rows": rows,
    }


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- 1〜3. 鮮度は source_time で判定する ------------------------------------------------

def test_stale_case_observed_late_but_source_time_old():
    """10/4 東京：15:29 に取ったが、中身は 14:40 の価格。15分前の直前オッズ扱いにしない。"""
    post = _post()
    win = audit_manifest.market_evidence(
        _record("2026-10-04 14:40:27", "2026-10-04T15:29:50+09:00"), post, CONFIG
    )
    assert win["observed_minutes_to_post"] == 15.167
    assert win["freshness"]["status"] == "stale"
    assert win["freshness"]["freshness_basis"] == "source_time"
    assert win["freshness"]["minutes_to_post"] == 64.55
    assert win["freshness"]["target_window"] is False
    assert win["freshness"]["fresh"] is False


def test_unknown_source_time_never_falls_back_to_observed_at():
    post = _post()
    win = audit_manifest.market_evidence(
        _record(None, "2026-10-04T15:35:00+09:00"), post, CONFIG
    )
    assert win["freshness"]["status"] == "unknown"
    assert win["freshness"]["freshness_basis"] == "unavailable"
    assert win["freshness"]["target_window"] is False
    # observed_at だけなら target_window（10分前）になってしまうが、それを価格時刻として使わない
    assert audit_manifest.classify_freshness(None, post, CONFIG)["status"] == "unknown"


def test_each_market_keeps_its_own_time():
    post = _post()
    markets = {
        "win": _record("2026-10-04 14:40:00", "2026-10-04T15:35:10+09:00"),
        "wide": _record("2026-10-04 15:35:00", "2026-10-04T15:35:20+09:00", type_code="5"),
    }
    win = audit_manifest.market_evidence(markets["win"], post, CONFIG)
    wide = audit_manifest.market_evidence(markets["wide"], post, CONFIG)
    assert win["freshness"]["status"] == "stale"
    assert wide["freshness"]["status"] == "target_window"
    assert win["source_time"] != wide["source_time"]


def test_freshness_boundaries():
    post = _post()
    cases = {
        "2026-10-04 15:42:00": "very_late",
        "2026-10-04 15:40:00": "target_window",
        "2026-10-04 15:30:00": "target_window",
        "2026-10-04 15:20:00": "acceptable",
        "2026-10-04 15:15:00": "acceptable",
        "2026-10-04 15:14:00": "stale",
        "2026-10-04 15:45:00": "after_post",
    }
    for source_time, expected in cases.items():
        assert audit_manifest.classify_freshness(source_time, post, CONFIG)["status"] == expected


# ---- 4. 一部の券種だけ取得失敗 ------------------------------------------------------------

def _win_body(official="2026-10-04 14:40:27"):
    return {"official_datetime": official,
            "odds": {"1": {"01": ["11.2", 0, 5], "02": ["2.9", 0, 1], "03": ["19.6", 0, 7]}}}


def _combo_body(type_code, official):
    tables = {
        "4": {"0102": ["30.1", 0, 1]},
        "5": {"0102": ["8.0", "9.1", 1]},
        "7": {"010203": ["120.5", 0, 1]},
    }
    return {"official_datetime": official, "odds": {type_code: tables[type_code]}}


def _fake_fetcher(fail_types=(), times=None, empty_types=()):
    times = times or {}

    def fetch(race_ref, type_code):
        if type_code in fail_types:
            raise RuntimeError("boom")
        if type_code == "1":
            return _win_body(times.get("1", "2026-10-04 14:40:27"))
        if type_code in empty_types:
            return {"official_datetime": times.get(type_code), "odds": {}}
        return _combo_body(type_code, times.get(type_code, "2026-10-04 14:52:00"))
    return fetch


def test_partial_market_failure_is_recorded_not_dropped(monkeypatch):
    monkeypatch.setattr(b2_odds, "_fetch_type_body", _fake_fetcher(fail_types=("7",)))
    result = b2_odds.fetch_odds("202605040211", clock=lambda: _at(15, 29, 50))
    meta = result["market_meta"]
    assert meta["win"]["status"] == "ok" and meta["win"]["rows"] == 3
    assert meta["win"]["source_time"] == "2026-10-04 14:40:27"
    assert meta["win"]["observed_at"] == "2026-10-04T15:29:50+09:00"
    assert meta["umaren"]["status"] == "ok" and meta["wide"]["status"] == "ok"
    assert meta["sanrenpuku"]["status"] == "fetch_failed"
    assert meta["sanrenpuku"]["source_time"] is None
    assert "3連複" not in result["combo_odds"]

    post = _post()
    evidence = audit_manifest.market_evidence(meta["sanrenpuku"], post, CONFIG)
    assert evidence["fetch_status"] == "fetch_failed"
    assert evidence["freshness"]["status"] == "missing"
    assert audit_manifest.market_evidence(meta["win"], post, CONFIG)["freshness"]["status"] == "stale"


def test_empty_and_unparsed_market_statuses(monkeypatch):
    monkeypatch.setattr(b2_odds, "_fetch_type_body", _fake_fetcher(empty_types=("4",)))
    meta = b2_odds.fetch_odds("x", clock=lambda: _at(15, 0))["market_meta"]
    assert meta["umaren"]["status"] == "empty"

    def unparsed(race_ref, type_code):
        if type_code == "1":
            return _win_body()
        return {"official_datetime": "2026-10-04 14:52:00", "odds": {type_code: {"bad": ["x"]}}}
    monkeypatch.setattr(b2_odds, "_fetch_type_body", unparsed)
    meta = b2_odds.fetch_odds("x", clock=lambda: _at(15, 0))["market_meta"]
    assert {meta[k]["status"] for k in ("umaren", "wide", "sanrenpuku")} == {"unparsed"}


def test_odds_updated_at_stays_win_source_time_even_if_combos_are_newer(monkeypatch):
    monkeypatch.setattr(b2_odds, "_fetch_type_body", _fake_fetcher(times={
        "1": "2026-10-04 14:40:27", "4": "2026-10-04 15:30:00",
        "5": "2026-10-04 15:31:00", "7": "2026-10-04 15:32:00",
    }))
    race = {"entries": [{"num": 1}, {"num": 2}, {"num": 3}]}
    b2_odds.merge_odds_into_race(race, b2_odds.fetch_odds("x", clock=lambda: _at(15, 33)))
    assert race["odds_updated_at"] == "2026-10-04 14:40:27"
    block = race["odds_market_meta"]
    assert block["odds_updated_at_is"] == "win.source_time"
    assert block["source_time_definition"] == "provider_returned_official_datetime"
    assert block["markets"]["sanrenpuku"]["source_time"] == "2026-10-04 15:32:00"


def test_win_fetch_failure_leaves_a_fetch_failed_record():
    block = b2_odds.win_fetch_failed_meta("x", clock=lambda: _at(15, 0))
    assert block["markets"]["win"]["status"] == "fetch_failed"
    assert block["markets"]["wide"]["status"] == "not_fetched"


# ---- 5. 直前の単勝観測は win だけ ----------------------------------------------------------

def _raw_race(race_id=TOKYO, post_time="15:45"):
    return {"id": race_id, "post_time": post_time,
            "source_refs": {"netkeiba": "202605040211"}, "entries": []}


def test_late_capture_is_win_only_and_does_not_prove_combo_freshness(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(capture_late_odds.b2_odds, "_fetch_type_body", _fake_fetcher(
        times={"1": "2026-10-04 15:33:00"}
    ))
    monkeypatch.setattr(capture_late_odds.b_shutuba, "fetch_shutuba",
                        lambda ref: {"entries": []})
    odds_dir = tmp_path / "odds"
    report = capture_late_odds.capture(
        {"races": [_raw_race()]}, "2026-10-04", now=_at(15, 34),
        directory=odds_dir, condition_directory=tmp_path / "cond",
    )
    assert report["captured"][0]["market_scope"] == "win"
    [obs] = odds_history.load_observations(TOKYO, odds_dir)
    assert obs["market_scope"] == "win"
    assert obs["odds_rows_market"] == "win"
    assert obs["market_meta"]["win"]["source_time"] == "2026-10-04 15:33:00"
    assert obs["market_meta"]["win"]["observed_at"] == obs["observed_at"]
    for key in ("umaren", "wide", "sanrenpuku"):
        assert obs["market_meta"][key]["status"] == "not_fetched"

    post = _post()
    artifact = audit_manifest._odds_artifact(
        next((odds_dir / TOKYO).glob("*.json")), obs, post, CONFIG
    )
    assert artifact["markets"]["win"]["freshness"]["status"] == "target_window"
    for key in ("umaren", "wide", "sanrenpuku"):
        assert artifact["markets"][key]["freshness"]["status"] == "not_in_scope"
        assert artifact["markets"][key]["freshness"]["fresh"] is False


# ---- 6. 発走後の取得は発走前の証跡に混ざらない ---------------------------------------------

def test_after_post_observations_are_excluded(tmp_path: Path):
    post = _post()
    odds_dir = tmp_path / "odds"
    _write(odds_dir / TOKYO / "a.json", {
        "race_id": TOKYO, "observed_at": "2026-10-04T15:20:00+09:00",
        "source_time": "2026-10-04 15:19:00", "phase": "pipeline", "odds": [{"num": 1, "win_odds": 2.0}],
    })
    # observed_at が発走後
    _write(odds_dir / TOKYO / "b.json", {
        "race_id": TOKYO, "observed_at": "2026-10-04T15:50:00+09:00",
        "source_time": "2026-10-04 15:40:00", "phase": "late", "odds": [{"num": 1, "win_odds": 2.1}],
    })
    # 取得は発走前でも、式別の source_time が発走後
    _write(odds_dir / TOKYO / "c.json", {
        "race_id": TOKYO, "observed_at": "2026-10-04T15:44:00+09:00",
        "source_time": "2026-10-04 15:43:00", "phase": "pipeline", "odds": [{"num": 1, "win_odds": 2.2}],
        "market_meta": {"win": _record("2026-10-04 15:43:00", "2026-10-04T15:43:30+09:00"),
                        "wide": _record("2026-10-04 15:46:00", "2026-10-04T15:43:40+09:00")},
    })
    rows = audit_manifest.pre_race_odds(TOKYO, post, post, odds_dir)
    assert [path.name for _, path, _ in rows] == ["a.json"]

    # 保存側も fail-closed：式別の source_time が発走後なら観測として残さない
    race = {"id": TOKYO, "post_time": "15:45", "odds_updated_at": "2026-10-04 15:40:00",
            "entries": [{"num": 1, "win_odds": 2.0}],
            "odds_market_meta": b2_odds.market_meta_block({
                "win": _record("2026-10-04 15:40:00", "2026-10-04T15:41:00+09:00"),
                "sanrenpuku": _record("2026-10-04 15:45:30", "2026-10-04T15:41:10+09:00"),
            })}
    assert odds_history.append_observation(race, "pipeline", _at(15, 42), tmp_path / "x") \
        == odds_history.AFTER_POST

    # 発走後には新しい pre-race manifest を作らない
    assert audit_manifest.build_manifest(_raw_race(), _at(15, 45), "late",
                                         tmp_path, tmp_path, odds_dir, CONFIG) is None


# ---- 7. manifest は3つの成果物の hash を持つ（10/4 東京の再現） -------------------------------

def _tokyo_fixture(tmp_path: Path) -> dict[str, Path]:
    """10/4 東京11R：13時の通常pipeline、15:29:50 の取得（中身は 14:40:27 の単勝）、15:30 の予想。"""
    pred_dir, ctx_dir, odds_dir = tmp_path / "pred", tmp_path / "ctx", tmp_path / "odds"
    marks = [{"num": 1, "odds": 11.2, "score": 64.5}, {"num": 2, "odds": 2.9, "score": 76.2}]
    snapshot = _write(pred_dir / f"{TOKYO}.json", {
        "race_id": TOKYO, "frozen_at": "2026-10-04T15:30:01+09:00", "pre_race": True,
        "post_time": "15:45", "model_id": "win-v1-speed-guard", "config_hash": "abc",
        "speed_quality": {"used": False, "coverage": 0.1765, "qualified_horses": 3,
                          "total_horses": 17, "reason": "insufficient_race_coverage"},
        "legendary": False, "otori_card_ev": None,
        "marks": marks,
        "cards": [
            {"char": "chappy", "bets": [{"type": "ワイド"}, {"type": "馬連"}, {"type": "3連複"}],
             "market_ev": {"complete": True}},
            {"char": "kei", "bets": [{"type": "ワイド"}], "market_ev": {"complete": True}},
        ],
    })
    early = _write(odds_dir / TOKYO / "20261004T104525_pipeline.json", {
        "race_id": TOKYO, "post_time": "15:45", "observed_at": "2026-10-04T10:45:25+09:00",
        "source_time": "2026-10-04 10:40:52", "phase": "pipeline",
        "odds": [{"num": 1, "win_odds": 12.0}, {"num": 2, "win_odds": 2.7}],
    })
    basis = _write(odds_dir / TOKYO / "20261004T152950_pipeline.json", {
        "race_id": TOKYO, "post_time": "15:45", "observed_at": "2026-10-04T15:29:50+09:00",
        "source_time": "2026-10-04 14:40:27", "phase": "pipeline", "market_scope": "all",
        "odds_rows_market": "win",
        "odds": [{"num": 1, "win_odds": 11.2}, {"num": 2, "win_odds": 2.9}],
        "market_meta": {
            "win": _record("2026-10-04 14:40:27", "2026-10-04T15:29:41+09:00"),
            "umaren": _record("2026-10-04 14:52:00", "2026-10-04T15:29:44+09:00", type_code="4"),
            "wide": _record("2026-10-04 15:35:00", "2026-10-04T15:29:47+09:00", type_code="5"),
            "sanrenpuku": _record(None, "2026-10-04T15:29:50+09:00", status="fetch_failed",
                                  rows=0, type_code="7"),
        },
    })
    context = _write(ctx_dir / TOKYO / "20261004T153002_pipeline.json", {
        "race_id": TOKYO, "post_time": "15:45", "observed_at": "2026-10-04T15:30:02+09:00",
        "phase": "pipeline", "pre_race": True,
        "context_layers": {"layer2": {"horses": [{"num": 1, "body_weight": {"current": 480}},
                                                 {"num": 2, "body_weight": {"current": None}}]}},
    })
    return {"pred": pred_dir, "ctx": ctx_dir, "odds": odds_dir, "snapshot": snapshot,
            "early": early, "basis": basis, "context": context}


def test_tokyo_1004_manifest_reproduces_stale_price_and_keeps_hashes(tmp_path: Path):
    fx = _tokyo_fixture(tmp_path)
    out_dir = tmp_path / "manifests"
    report = audit_manifest.capture(
        {"races": [_raw_race()]}, now=_at(15, 31), phase="pipeline", output_directory=out_dir,
        prediction_directory=fx["pred"], context_directory=fx["ctx"], odds_directory=fx["odds"],
        config=CONFIG,
    )
    assert report["added"] == [TOKYO]
    [path] = list((out_dir / TOKYO).glob("*.json"))
    manifest = json.loads(path.read_text(encoding="utf-8"))

    assert manifest["pre_race"] is True and manifest["mode"] == "audit_only"
    assert set(manifest["time_definitions"]) >= {"observed_at", "source_time", "post_at"}
    assert manifest["freshness_rules"]["basis"] == "source_time"
    arts = manifest["artifacts"]
    assert arts["prediction"]["sha256"] == _sha(fx["snapshot"])
    assert arts["prediction"]["frozen_at"] == "2026-10-04T15:30:01+09:00"
    assert arts["context"]["sha256"] == _sha(fx["context"])
    assert arts["context"]["observed_at"] == "2026-10-04T15:30:02+09:00"
    assert arts["odds"]["sha256"] == _sha(fx["basis"])

    # 15:29:50 に取得（発走15分前）でも、価格は 14:40:27 → stale
    assert arts["odds"]["observed_minutes_to_post"] == 15.167
    assert manifest["odds_freshness"]["status"] == "stale"
    assert manifest["odds_freshness"]["freshness_basis"] == "source_time"

    basis = manifest["prediction_price_basis"]
    assert basis["status"] == "linked"
    assert basis["odds"]["path"].endswith("20261004T152950_pipeline.json")
    assert basis["prediction_odds_match"] is True

    cards = manifest["card_price_evidence"]
    assert cards["markets"]["win"]["freshness"] == "stale"
    assert cards["markets"]["umaren"]["freshness"] == "stale"
    assert cards["markets"]["wide"]["freshness"] == "target_window"   # 券種ごとに別の時刻
    assert cards["markets"]["sanrenpuku"]["freshness"] == "missing"
    assert cards["markets"]["sanrenpuku"]["fetch_status"] == "fetch_failed"
    chappy = next(c for c in cards["cards"] if c["char"] == "chappy")
    assert chappy["market_ev_computed"] is True
    assert chappy["prices_not_fresh"] == ["sanrenpuku", "umaren"]
    kei = next(c for c in cards["cards"] if c["char"] == "kei")
    assert kei["prices_not_fresh"] == []
    assert kei["pq_basis_win"]["freshness"] == "stale"
    assert manifest["context_completeness"]["body_weight_current_coverage"] == 0.5
    assert manifest["speed"]["used"] is False


def test_manifest_is_append_only_and_skips_unchanged(tmp_path: Path):
    fx = _tokyo_fixture(tmp_path)
    out_dir = tmp_path / "manifests"
    kwargs = dict(output_directory=out_dir, prediction_directory=fx["pred"],
                  context_directory=fx["ctx"], odds_directory=fx["odds"], config=CONFIG)
    audit_manifest.capture({"races": [_raw_race()]}, now=_at(15, 31), phase="pipeline", **kwargs)
    first = next((out_dir / TOKYO).glob("*.json"))
    before = first.read_bytes()

    again = audit_manifest.capture({"races": [_raw_race()]}, now=_at(15, 35), phase="late", **kwargs)
    assert again["skipped"] == [{"race_id": TOKYO, "reason": "unchanged"}]

    # 直前の単勝観測が増えたら新しいファイルとして追加し、前の manifest は書き換えない
    _write(fx["odds"] / TOKYO / "20261004T153600_late.json", {
        "race_id": TOKYO, "post_time": "15:45", "observed_at": "2026-10-04T15:36:00+09:00",
        "source_time": "2026-10-04 15:35:30", "phase": "late", "market_scope": "win",
        "odds": [{"num": 1, "win_odds": 10.5}],
        "market_meta": {"win": _record("2026-10-04 15:35:30", "2026-10-04T15:36:00+09:00"),
                        "umaren": b2_odds.not_fetched_record(), "wide": b2_odds.not_fetched_record(),
                        "sanrenpuku": b2_odds.not_fetched_record()},
    })
    added = audit_manifest.capture({"races": [_raw_race()]}, now=_at(15, 37), phase="late", **kwargs)
    assert added["added"] == [TOKYO]
    assert first.read_bytes() == before
    latest = audit_manifest.latest_manifest(TOKYO, out_dir)
    # 一番新しい単勝は target_window。でも買い目の価格（basis）は pipeline の観測のまま
    assert latest["odds_freshness"]["status"] == "target_window"
    assert latest["prediction_price_basis"]["odds"]["path"].endswith("20261004T152950_pipeline.json")
    assert latest["card_price_evidence"]["markets"]["umaren"]["freshness"] == "stale"
    assert latest["card_price_evidence"]["markets"]["win"]["freshness"] == "stale"


def test_legacy_observation_without_market_meta_is_win_only(tmp_path: Path):
    fx = _tokyo_fixture(tmp_path)
    payload = json.loads(fx["basis"].read_text(encoding="utf-8"))
    payload.pop("market_meta")
    payload.pop("market_scope")
    _write(fx["basis"], payload)
    evidence = audit_manifest.evidence_from_files(
        _raw_race(), fx["pred"], fx["ctx"], fx["odds"], CONFIG
    )
    assert evidence["status"] == "derived_from_pre_race_artifacts"
    basis = evidence["prediction_price_basis"]["odds"]
    assert basis["market_scope"] == "legacy_unknown"
    assert basis["markets"]["win"]["freshness"]["status"] == "stale"
    for key in ("umaren", "wide", "sanrenpuku"):
        assert basis["markets"][key]["fetch_status"] == "provenance_unavailable"
        assert basis["markets"][key]["freshness"]["status"] == "unknown"


def test_anomaly_report_has_race_audit_from_manifest_or_files(tmp_path: Path):
    fx = _tokyo_fixture(tmp_path)
    results = {"results": [{
        "race_id": TOKYO, "finish": [1, 2, 3],
        "meta": {"venue": "東京", "race_no": 11, "name": "毎日王冠", "post_time": "15:45",
                 "marks": [{"num": 1, "score": 64.5, "odds": 11.2},
                           {"num": 2, "score": 76.2, "odds": 2.9}]},
        "cards": [],
    }]}
    kwargs = dict(context_snapshot_directory=fx["ctx"], prediction_snapshot_directory=fx["pred"],
                  odds_history_directory=fx["odds"], audit_config=CONFIG)

    derived = anomaly_review.build_report(results, audit_manifest_directory=tmp_path / "none", **kwargs)
    [row] = derived["race_audits"]
    assert row["evidence_source"] == "derived_without_manifest"
    assert row["present"] == {"prediction_snapshot": True, "context_snapshot": True,
                              "odds_observation": True}
    assert row["hashes"]["prediction"] == _sha(fx["snapshot"])
    assert row["odds_freshness_latest_win"]["status"] == "stale"
    assert row["price_times_by_market"]["sanrenpuku"]["fetch_status"] == "fetch_failed"
    assert row["body_weight_current_coverage"] == 0.5
    assert row["speed"]["coverage"] == 0.1765
    assert derived["summary"]["audit"]["derived_without_manifest"] == 1

    out_dir = tmp_path / "manifests"
    audit_manifest.capture({"races": [_raw_race()]}, now=_at(15, 31), phase="pipeline",
                           output_directory=out_dir, prediction_directory=fx["pred"],
                           context_directory=fx["ctx"], odds_directory=fx["odds"], config=CONFIG)
    with_manifest = anomaly_review.build_report(results, audit_manifest_directory=out_dir, **kwargs)
    assert with_manifest["race_audits"][0]["evidence_source"] == "pre_race_manifest"
    assert with_manifest["summary"]["audit"]["with_manifest"] == 1


# ---- 8. 予想・買い目は PR 前後で完全一致 -------------------------------------------------

def _strip_volatile(predictions: dict) -> dict:
    out = copy.deepcopy(predictions)
    out.pop("generated_at", None)
    return out


def test_predictions_are_identical_with_or_without_odds_market_meta():
    raw = json.loads((ROOT / "docs" / "samples" / "raw.sample.json").read_text(encoding="utf-8"))
    base_times = json.loads((ROOT / "tests" / "fixtures" / "base_times.sample.json")
                            .read_text(encoding="utf-8"))
    configs = build_predictions.load_configs()
    before = build_predictions.build_predictions(copy.deepcopy(raw), configs, base_times)

    with_meta = copy.deepcopy(raw)
    for race in with_meta["races"]:
        race["odds_market_meta"] = b2_odds.market_meta_block({
            "win": _record("2026-10-04 14:40:27", "2026-10-04T15:29:41+09:00"),
            "sanrenpuku": _record(None, "2026-10-04T15:29:50+09:00", status="fetch_failed", rows=0),
        })
    after = build_predictions.build_predictions(with_meta, configs, base_times)
    assert _strip_volatile(after) == _strip_volatile(before)

    # snapshot（採点の根拠）の中身も同じ。provenance は snapshot に入らない
    for race_before, race_after in zip(before["races"], after["races"]):
        snap_before = snapshots.build_snapshot(race_before, "W", "2026-10-04T15:30:01+09:00", True)
        snap_after = snapshots.build_snapshot(race_after, "W", "2026-10-04T15:30:01+09:00", True)
        assert snap_before == snap_after
        assert "odds_market_meta" not in snap_after


def test_fetch_odds_market_values_are_unchanged_by_provenance(monkeypatch):
    """by_num・combo_odds・official_datetime（＝予想の入力）は従来の解析と同じ。"""
    monkeypatch.setattr(b2_odds, "_fetch_type_body", _fake_fetcher())
    result = b2_odds.fetch_odds("x", clock=lambda: _at(15, 0))
    assert result["by_num"] == b2_odds.extract_win_place_odds(_win_body())
    expected_combo = {}
    for code in ("4", "5", "7"):
        expected_combo.update(b2_odds.extract_combo_odds(_combo_body(code, "t")))
    assert result["combo_odds"] == expected_combo
    assert result["official_datetime"] == "2026-10-04 14:40:27"

    race_new = {"entries": [{"num": 1}, {"num": 2}, {"num": 3}]}
    race_old = copy.deepcopy(race_new)
    b2_odds.merge_odds_into_race(race_new, result)
    legacy = {k: result[k] for k in ("official_datetime", "by_num", "combo_odds")}
    b2_odds.merge_odds_into_race(race_old, legacy)
    race_new.pop("odds_market_meta")
    assert race_new == race_old


def test_audit_config_is_not_part_of_the_prediction_config_hash():
    from logic import model_registry
    assert "audit_manifest.json" not in model_registry.HASH_CONFIGS


# ---- レビュー対応：同じ価格の再取得も「取りに行った事実」を残す ----------------------------

def test_refetch_with_same_price_is_kept_as_separate_attempts(monkeypatch, tmp_path: Path):
    """15:29 と 15:35 に取りに行き、provider がどちらも 14:40 の同じ単勝を返した。

    研究用の時系列は同じ価格を1件にまとめるが、取得の事実は2件とも残り、
    15:35 時点でもまだ 14:40 の価格だったことが後から証明できる。
    """
    monkeypatch.setattr(capture_late_odds.b2_odds, "_fetch_type_body", _fake_fetcher(
        times={"1": "2026-10-04 14:40:00"}
    ))
    monkeypatch.setattr(capture_late_odds.b_shutuba, "fetch_shutuba", lambda ref: {"entries": []})
    odds_dir = tmp_path / "odds"
    for minute in (29, 35):
        capture_late_odds.capture(
            {"races": [_raw_race()]}, "2026-10-04", now=_at(15, minute),
            directory=odds_dir, condition_directory=tmp_path / "cond",
        )

    # 研究用の時系列は1件（同じ価格の重複は保存しない）
    assert len(odds_history.load_observations(TOKYO, odds_dir)) == 1
    # 取得の記録は2件。どちらも provider は 14:40 を返していた
    attempts = [payload for _, payload in odds_history.load_attempts(TOKYO, odds_dir)]
    assert [a["observed_at"] for a in attempts] == [
        "2026-10-04T15:29:00+09:00", "2026-10-04T15:35:00+09:00"]
    assert [a["source_time"] for a in attempts] == ["2026-10-04 14:40:00"] * 2
    assert [a["result"] for a in attempts] == ["added", "duplicate"]
    assert attempts[0]["odds_sha256"] == attempts[1]["odds_sha256"]
    assert attempts[1]["observation_file"] == attempts[0]["observation_file"]
    assert all(a["market_scope"] == "win" for a in attempts)

    # manifest でも 15:35 の取得が見え、そのとき返っていた価格は 14:40（stale）だと分かる
    post = _post()
    block = audit_manifest.fetch_attempts(TOKYO, post, _at(15, 40), odds_dir, CONFIG)
    assert block["count"] == 2
    assert block["latest"]["observed_at"] == "2026-10-04T15:35:00+09:00"
    assert block["latest"]["observed_minutes_to_post"] == 10.0
    assert block["latest"]["returned_source_time"] == "2026-10-04 14:40:00"
    assert block["latest"]["win_freshness"] == "stale"


def test_new_attempt_with_same_price_produces_a_new_manifest(tmp_path: Path):
    fx = _tokyo_fixture(tmp_path)
    out_dir = tmp_path / "manifests"
    kwargs = dict(output_directory=out_dir, prediction_directory=fx["pred"],
                  context_directory=fx["ctx"], odds_directory=fx["odds"], config=CONFIG)
    race = {"id": TOKYO, "post_time": "15:45", "source_refs": {"netkeiba": "202605040211"},
            "odds_updated_at": "2026-10-04 14:40:27",
            "entries": [{"num": 1, "win_odds": 11.2}, {"num": 2, "win_odds": 2.9}],
            "odds_market_meta": b2_odds.market_meta_block({
                "win": _record("2026-10-04 14:40:27", "2026-10-04T15:33:00+09:00")})}
    assert odds_history.append_observation(race, "late", _at(15, 33), fx["odds"]) == odds_history.ADDED
    audit_manifest.capture({"races": [_raw_race()]}, now=_at(15, 34), phase="late", **kwargs)

    race["odds_market_meta"] = b2_odds.market_meta_block({
        "win": _record("2026-10-04 14:40:27", "2026-10-04T15:37:00+09:00")})
    assert odds_history.append_observation(race, "late", _at(15, 37), fx["odds"]) \
        == odds_history.DUPLICATE
    again = audit_manifest.capture({"races": [_raw_race()]}, now=_at(15, 38), phase="late", **kwargs)
    assert again["added"] == [TOKYO]     # 値は同じでも、新しい取得があれば manifest を残す
    latest = audit_manifest.latest_manifest(TOKYO, out_dir)
    assert latest["fetch_attempts"]["latest"]["observed_at"] == "2026-10-04T15:37:00+09:00"
    assert latest["fetch_attempts"]["latest"]["result"] == "duplicate"


def test_late_fetch_failure_is_recorded_as_attempt(monkeypatch, tmp_path: Path):
    def broken(race_ref, type_code):
        raise ValueError("malformed")
    monkeypatch.setattr(capture_late_odds.b2_odds, "_fetch_type_body", broken)
    odds_dir = tmp_path / "odds"
    report = capture_late_odds.capture(
        {"races": [_raw_race()]}, "2026-10-04", now=_at(15, 30),
        directory=odds_dir, condition_directory=tmp_path / "cond",
    )
    assert report["failed"] == [{"race_id": TOKYO}]
    [(_, attempt)] = odds_history.load_attempts(TOKYO, odds_dir)
    assert attempt["result"] == "fetch_failed"
    assert attempt["source_time"] is None
    assert attempt["market_meta"]["win"]["status"] == "fetch_failed"
    assert attempt["error"] == "ValueError"
    assert odds_history.load_observations(TOKYO, odds_dir) == []


def test_attempts_after_post_are_not_recorded(tmp_path: Path):
    race = {"id": TOKYO, "post_time": "15:45", "odds_updated_at": "2026-10-04 15:40:00",
            "entries": [{"num": 1, "win_odds": 2.0}]}
    assert odds_history.record_attempt(race, "late", _at(15, 46), "added", tmp_path) is None
    assert odds_history.load_attempts(TOKYO, tmp_path) == []


# ---- レビュー対応：observed_at は HTTP 完了時刻、raw 構築完了は recorded_at -------------------

def _pipeline_raw(win_http="2026-10-04T15:29:41+09:00", fetched_at="2026-10-04T15:29:50+09:00"):
    return {
        "fetched_at": fetched_at,
        "collection_report": {"built": [{"race_id": TOKYO}]},
        "races": [{
            "id": TOKYO, "post_time": "15:45", "source_refs": {"netkeiba": "202605040211"},
            "odds_updated_at": "2026-10-04 14:40:27",
            "entries": [{"num": 1, "win_odds": 11.2}, {"num": 2, "win_odds": 2.9}],
            "odds_market_meta": b2_odds.market_meta_block({
                "win": _record("2026-10-04 14:40:27", win_http),
                "umaren": _record("2026-10-04 14:52:00", "2026-10-04T15:29:44+09:00", type_code="4"),
            }),
        }],
    }


def test_pipeline_observation_observed_at_is_win_http_time(tmp_path: Path):
    odds_dir = tmp_path / "odds"
    report = odds_history.append_current_run(_pipeline_raw(), phase="pipeline",
                                             directory=odds_dir, now=_at(15, 30))
    assert report["added"] == 1
    [obs] = odds_history.load_observations(TOKYO, odds_dir)
    assert obs["observed_at"] == "2026-10-04T15:29:41+09:00"        # 単勝の HTTP 完了
    assert obs["observed_at_basis"] == "win_http_completed"
    assert obs["recorded_at"] == "2026-10-04T15:29:50+09:00"        # raw 全体の構築完了
    assert obs["observed_at"] == obs["market_meta"]["win"]["observed_at"]
    [(_, attempt)] = odds_history.load_attempts(TOKYO, odds_dir)
    assert attempt["observed_at"] == obs["observed_at"]
    assert attempt["recorded_at"] == obs["recorded_at"]


def test_observed_at_falls_back_to_recorded_at_only_with_explicit_basis(tmp_path: Path):
    """単勝APIが失敗し、出馬表の別ページの値で埋まったとき。HTTP 時刻ではないと明示する。"""
    race = _pipeline_raw()["races"][0]
    race["odds_market_meta"] = b2_odds.win_fetch_failed_meta("x", clock=lambda: _at(15, 29))
    obs = odds_history.build_observation(race, "pipeline", _at(15, 29, 50))
    assert obs["observed_at"] == "2026-10-04T15:29:50+09:00"
    assert obs["observed_at_basis"] == "recorded_at"
    assert obs["recorded_at"] == obs["observed_at"]


def test_price_basis_link_uses_recorded_at_with_new_observed_at_definition(tmp_path: Path):
    fx = _tokyo_fixture(tmp_path)
    odds_dir = tmp_path / "odds_new"
    odds_history.append_current_run(_pipeline_raw(), phase="pipeline",
                                    directory=odds_dir, now=_at(15, 30))
    # 予想は 15:30:01 に凍結：recorded_at（15:29:50）<= frozen_at なのでリンクする
    linked = audit_manifest.evidence_from_files(_raw_race(), fx["pred"], fx["ctx"], odds_dir, CONFIG)
    basis = linked["prediction_price_basis"]
    assert basis["status"] == "linked"
    assert basis["odds"]["observed_at"] == "2026-10-04T15:29:41+09:00"
    assert basis["odds"]["observed_at_basis"] == "win_http_completed"
    assert basis["odds"]["recorded_at"] == "2026-10-04T15:29:50+09:00"
    assert basis["prediction_odds_match"] is True

    # HTTP は frozen_at より前でも、観測の保存（recorded_at）が frozen_at より後なら、その予想の入力ではない
    late_dir = tmp_path / "odds_late"
    odds_history.append_current_run(
        _pipeline_raw(win_http="2026-10-04T15:29:58+09:00", fetched_at="2026-10-04T15:30:30+09:00"),
        phase="pipeline", directory=late_dir, now=_at(15, 31))
    unlinked = audit_manifest.evidence_from_files(_raw_race(), fx["pred"], fx["ctx"], late_dir, CONFIG)
    assert unlinked["prediction_price_basis"]["status"] == "no_pipeline_odds_observation"
