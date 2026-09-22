from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import pytest

# run_pipeline.yml は `python tests/test_speed_guard.py` と**単体スクリプトとして**呼ぶので、
# 他のテストと同じくリポジトリルートを import パスに足しておく。
# これが無いと本番パイプラインのテスト段階が ModuleNotFoundError で落ちる。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


# --- 統合レビュー（2026-09-21）で見つかった2つの副作用 ------------------------

def test_disabling_speed_also_clears_the_confidence_evidence():
    """
    ★ ①をレース全体で切ったのに、妙味の信頼度だけ「時計が揃っている」と言い続けない。

    conf は「n_usable >= usable_run_min の馬の割合」で決まる。guardが speed_raw を
    捨てても n_usable を残していたため、①の寄与がゼロなのに conf が下限より高く出ていた
    （13頭中5頭が4走ぶん指数化できる例で conf=0.692）。conf は妙味の掛け算に入るので、
    そのままだと鳳の降臨判定まで甘くなる。
    """
    from logic import myomi

    myomi_config = json.loads((ROOT / "config" / "myomi.json").read_text(encoding="utf-8"))
    horses = [{"speed_raw": 100.0 + i, "n_usable": 4, "uncertain": False} for i in range(5)]
    horses += [{"speed_raw": None, "n_usable": 0, "uncertain": True} for _ in range(8)]

    report = speed_mod.apply_race_speed_guard(horses, CONFIG)

    assert report["used"] is False
    assert all(h["n_usable"] == 0 for h in horses)
    conf = myomi.compute_confidence([h["n_usable"] for h in horses], myomi_config)
    assert conf == myomi_config["confidence"]["conf_floor"]


def test_a_horse_that_fails_the_run_minimum_stops_counting_toward_confidence():
    """走数不足で speed を捨てた馬も、信頼度の分子に残さない。"""
    from logic import myomi

    myomi_config = json.loads((ROOT / "config" / "myomi.json").read_text(encoding="utf-8"))
    horses = [
        {"speed_raw": 90.0, "n_usable": 4, "uncertain": False},
        {"speed_raw": 110.0, "n_usable": 3, "uncertain": False},
        {"speed_raw": 130.0, "n_usable": 1, "uncertain": True},   # 走数不足→speedを捨てる
        {"speed_raw": None, "n_usable": 0, "uncertain": True},
    ]

    speed_mod.apply_race_speed_guard(horses, CONFIG)

    assert horses[2]["n_usable"] == 0
    # 時計が揃っているのは4頭中2頭
    assert myomi.compute_confidence([h["n_usable"] for h in horses], myomi_config) == 0.75


def test_imputed_horses_do_not_amplify_the_speed_of_observed_horses():
    """
    ★ 中立補完が、観測できている馬のzを増幅しないこと。

    平均ちょうどの点を母集団に足すと標準偏差が縮む。観測7頭のsd 6.07 に補完6頭を足すと
    4.45 になり、観測馬のzが1.36倍に膨らんでいた。**データが薄いレースほど①が強く効く**
    という、このガードが直そうとした非対称と逆向きの歪み。
    統計量は観測馬だけで決め、補完馬はちょうど z=0 に落とす。
    """
    observed = [95.0, 100.0, 105.0, 110.0, 90.0, 102.0, 98.0]
    neutral = statistics.fmean(observed)
    only_speed = {"speed": 1.0, "aptitude": 0.0, "human": 0.0}

    baseline = base_score.composite_scores(
        [{"speed_raw": v, "speed_imputed": False} for v in observed], only_speed)

    for imputed_count in (3, 6):
        horses = [{"speed_raw": v, "speed_imputed": False} for v in observed]
        horses += [{"speed_raw": neutral, "speed_imputed": True} for _ in range(imputed_count)]
        z = base_score.composite_scores(horses, only_speed)

        assert z[:len(observed)] == pytest.approx(baseline), \
            f"補完{imputed_count}頭で観測馬のzが変わってしまった"
        assert all(abs(v) < 1e-12 for v in z[len(observed):])


def test_the_guard_still_neutralises_without_the_imputed_flag():
    """`speed_imputed` が付いていない呼び出し（既存コード）でも壊れない。"""
    z = base_score.z_standardize([90.0, 100.0, 110.0], stat_mask=None)
    assert z == pytest.approx([-1.224744871, 0.0, 1.224744871])


if __name__ == "__main__":
    tests = [
        test_target_surface_blocks_cross_surface_only_speed,
        test_sparse_race_coverage_disables_speed_for_every_horse,
        test_missing_speed_is_neutral_when_race_coverage_is_sufficient,
        test_disabling_speed_also_clears_the_confidence_evidence,
        test_a_horse_that_fails_the_run_minimum_stops_counting_toward_confidence,
        test_imputed_horses_do_not_amplify_the_speed_of_observed_horses,
        test_the_guard_still_neutralises_without_the_imputed_flag,
    ]
    for test in tests:
        test()
        print(test.__name__ + ": OK")
    print(f"\nすべてのテストが通りました（{len(tests)}件）。")
