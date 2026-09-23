from __future__ import annotations

import sys
from pathlib import Path

# run_pipeline.yml / run_results.yml は `python tests/xxx.py` と**単体スクリプトとして**呼ぶ。
# リポジトリルートを import パスに入れておかないと本番パイプラインのテスト段階で
# ModuleNotFoundError になる（PR #2 でも同じ事故があった）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def test_config_hash_changes_when_base_times_change(tmp_path: Path):
    """基準タイムが変われば同じmodel idでも別の予測前提として識別する。"""
    src = Path(__file__).resolve().parent.parent / "config"
    for name in model_registry.HASH_CONFIGS:
        (tmp_path / name).write_bytes((src / name).read_bytes())

    before = model_registry.config_hash(tmp_path)
    base_times = json.loads((tmp_path / "base_times.json").read_text(encoding="utf-8"))
    base_times.setdefault("東京", {}).setdefault("芝", {})["2000"] = 119.9
    (tmp_path / "base_times.json").write_text(
        json.dumps(base_times, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    after = model_registry.config_hash(tmp_path)

    assert before != after

if __name__ == "__main__":
    test_registry_has_one_champion_and_unique_ids()
    print("test_registry_has_one_champion_and_unique_ids: OK")
    test_top3_is_shadow_challenger_not_champion()
    print("test_top3_is_shadow_challenger_not_champion: OK")
    print("\nすべてのテストが通りました（2件）。")
