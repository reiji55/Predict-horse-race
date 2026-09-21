from __future__ import annotations

import json
from pathlib import Path

from logic import model_registry


def test_registry_has_one_champion_and_unique_ids():
    registry = model_registry.load_registry()
    assert registry["champion"]["role"] == "champion"
    ids = [registry["champion"]["id"]] + [m["id"] for m in registry["challengers"]]
    assert len(ids) == len(set(ids))


def test_top3_is_shadow_challenger_not_champion():
    registry = model_registry.load_registry()
    assert registry["champion"]["use_top3_partner"] is False
    challenger = next(m for m in registry["challengers"] if m["id"] == "top3-partner-v1")
    assert challenger["enabled"] is True
    assert challenger["use_top3_partner"] is True


def test_config_hash_changes_when_config_bytes_change(tmp_path: Path):
    src = Path(__file__).resolve().parent.parent / "config"
    for name in model_registry.HASH_CONFIGS:
        (tmp_path / name).write_bytes((src / name).read_bytes())

    before = model_registry.config_hash(tmp_path)
    cards = json.loads((tmp_path / "cards.json").read_text(encoding="utf-8"))
    cards["amt_unit"] = 999
    (tmp_path / "cards.json").write_text(json.dumps(cards), encoding="utf-8")
    after = model_registry.config_hash(tmp_path)

    assert before != after
