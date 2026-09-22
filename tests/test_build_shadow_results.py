from __future__ import annotations

import json
from pathlib import Path

from results import build_shadow_results


def test_shadow_results_use_pre_race_snapshot_and_write_comparison(tmp_path, monkeypatch):
    challenger_root = tmp_path / "challengers"
    comparison_path = tmp_path / "model_comparison.json"
    model_id = "challenger-test"
    snapshot_dir = challenger_root / model_id / "snapshots"
    snapshot_dir.mkdir(parents=True)

    snapshot = {
        "race_id": "r1",
        "week_id": "2026-W99",
        "frozen_at": "2026-09-22T14:00:00+09:00",
        "pre_race": True,
        "model_id": model_id,
        "model_role": "challenger",
        "git_commit": "abc123",
        "config_hash": "deadbeef",
        "cards": [{
            "char": "kei",
            "total": 500,
            "model_version": model_id,
            "model_role": "challenger",
            "bets": [{"type": "ワイド", "horses": [1, 2], "amt": 500}],
        }],
        "marks": [
            {"num": 1, "odds": 2.0},
            {"num": 2, "odds": 4.0},
        ],
    }
    (snapshot_dir / "r1.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )

    race_results = {
        "r1": {
            "finish": [1, 2, 3],
            "dividends": {
                "ワイド": [{"horses": [1, 2], "pay": 300}],
                "馬連": [],
                "3連複": [],
            },
        }
    }
    champion_results = {
        "results": [{
            "race_id": "r1",
            "model_id": "champ-test",
            "cards": [{
                "char": "kei", "hit": False, "spent": 500, "payout": 0, "bets": []
            }],
        }]
    }
    registry = {
        "champion": {"id": "champ-test", "role": "champion"},
        "challengers": [{
            "id": model_id,
            "role": "challenger",
            "enabled": True,
            "use_top3_partner": True,
        }],
    }

    monkeypatch.setattr(build_shadow_results, "CHALLENGER_ROOT", challenger_root)
    monkeypatch.setattr(build_shadow_results, "COMPARISON_PATH", comparison_path)

    comparison = build_shadow_results.build_all(
        race_results, champion_results, registry=registry
    )

    result_path = challenger_root / model_id / "results.json"
    assert result_path.exists()
    settled = json.loads(result_path.read_text(encoding="utf-8"))
    assert settled["results"][0]["cards"][0]["payout"] == 1500
    assert settled["results"][0]["model_id"] == model_id

    assert comparison_path.exists()
    row = comparison["comparisons"][0]
    assert row["common_race_ids"] == ["r1"]
    assert row["challenger"]["payout"] == 1500
    assert row["champion"]["payout"] == 0


def test_disabled_challenger_is_not_settled(tmp_path, monkeypatch):
    challenger_root = tmp_path / "challengers"
    comparison_path = tmp_path / "model_comparison.json"
    monkeypatch.setattr(build_shadow_results, "CHALLENGER_ROOT", challenger_root)
    monkeypatch.setattr(build_shadow_results, "COMPARISON_PATH", comparison_path)

    registry = {
        "champion": {"id": "champ", "role": "champion"},
        "challengers": [{
            "id": "off-model", "role": "challenger",
            "enabled": False, "use_top3_partner": True,
        }],
    }
    comparison = build_shadow_results.build_all(
        {}, {"results": []}, registry=registry
    )

    assert comparison["comparisons"] == []
    assert not (challenger_root / "off-model").exists()
