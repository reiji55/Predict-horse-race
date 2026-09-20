from __future__ import annotations

import json
from pathlib import Path

from logic import base_score
from logic import speed_index as speed_mod

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "speed_index.json").read_text(encoding="utf-8"))

BASE_TIMES = {
    "京都": {
        "芝": {"1200": 68.0},
        "ダ": {"1200": 72.0},
    }
}


def test_target_surface_blocks_cross_surface_only_speed():
    """
    芝の予想で、たまたまbase_timeがあるダート走だけがspeed材料になる事故を防ぐ。
    同じ馬に芝・ダート両方の指数化可能走があっても、target_surface=芝なら芝だけ使う。
    """
    runs = [
        {"venue": "京都", "surface": "ダ", "dist": 1200, "going": "良",
         "class": "op", "time_sec": 75.0, "impost": 55},
        {"venue": "京都", "surface": "芝", "dist": 1200, "going": "良",
         "class": "op", "time_sec": 68.3, "impost": 55},
    ]

    all_surfaces = speed_mod.compute_horse_speed(runs, CONFIG, BASE_TIMES)
    turf_only = speed_mod.compute_horse_speed(
        runs, CONFIG, BASE_TIMES, target_surface="芝"
    )

    assert all_surfaces is not None and all_surfaces["n_usable"] == 2
    assert turf_only is not None and turf_only["n_usable"] == 1

    turf_index = speed_mod.compute_run_index(runs[1], CONFIG, BASE_TIMES)
    assert abs(turf_only["latest"] - turf_index) < 1e-9
    assert abs(turf_only["best"] - turf_index) < 1e-9


def test_sparse_race_coverage_disables_speed_for_every_horse():
    """
    一部の馬だけspeedがあり、他馬は欠損という非対称をそのまま合成しない。
    qualified coverageが50%未満なら①speedをレース全体でOFFにする。
    """
    horses = [
        {"speed_raw": 85.0, "n_usable": 2, "uncertain": False},
        {"speed_raw": 110.0, "n_usable": 1, "uncertain": True},  # 走数不足で失格
        {"speed_raw": None, "n_usable": 0, "uncertain": True},
        {"speed_raw": None, "n_usable": 0, "uncertain": True},
    ]

    report = speed_mod.apply_race_speed_guard(horses, CONFIG)

    assert report["qualified_horses"] == 1
    assert report["coverage"] == 0.25
    assert report["used"] is False
    assert report["reason"] == "insufficient_race_coverage"
    assert all(h["speed_raw"] is None for h in horses)


def test_missing_speed_is_neutral_when_race_coverage_is_sufficient():
    """
    coverageが十分あるレースでは欠損馬を観測平均で補完する。
    z標準化後、その馬のspeed寄与がちょうど0になることを固定する。
    """
    horses = [
        {"speed_raw": 90.0, "n_usable": 3, "uncertain": False},
        {"speed_raw": 110.0, "n_usable": 2, "uncertain": False},
        {"speed_raw": None, "n_usable": 0, "uncertain": True},
        {"speed_raw": 130.0, "n_usable": 1, "uncertain": True},  # min_usable_runs未満→欠損扱い
    ]

    report = speed_mod.apply_race_speed_guard(horses, CONFIG)

    assert report["used"] is True
    assert report["qualified_horses"] == 2
    assert report["coverage"] == 0.5
    assert report["imputed_horses"] == 2
    assert horses[2]["speed_raw"] == 100.0
    assert horses[3]["speed_raw"] == 100.0
    assert horses[2]["speed_imputed"] is True
    assert horses[3]["speed_imputed"] is True

    z = base_score.z_standardize([h["speed_raw"] for h in horses])
    assert abs(z[2]) < 1e-12
    assert abs(z[3]) < 1e-12


if __name__ == "__main__":
    tests = [
        test_target_surface_blocks_cross_surface_only_speed,
        test_sparse_race_coverage_disables_speed_for_every_horse,
        test_missing_speed_is_neutral_when_race_coverage_is_sufficient,
    ]
    for test in tests:
        test()
        print(test.__name__ + ": OK")
    print(f"\nすべてのテストが通りました（{len(tests)}件）。")
