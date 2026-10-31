"""edge-hist-v1（docs/research/EDGE_LOOP_HIST_V1.md）の計算を、合成データで確かめる。keiba_core のデータは使わない。"""
from __future__ import annotations

import datetime
import math
import random

import pytest

from research import edge_hist as eh
from research import market_baseline as mb


def _race(rid: str, day: datetime.date, odds: dict[int, float], order: tuple[int, int, int],
          payout: int = 1000, popularity: int = 1, race_num: int = 11, surface: str = "芝",
          klass: str = "重賞") -> eh.Race:
    return eh.Race(rid, day, race_num, surface, klass, odds, order, payout, popularity)


def test_class_group_maps_old_and_new_names():
    assert eh.class_group("第9回紫苑ステークス(GII)", "3歳オープン") == "重賞"
    assert eh.class_group("第40回新潟大賞典(G3)", "4歳以上オープン") == "重賞"
    assert eh.class_group("3歳以上500万下", "3歳以上500万下") == "1勝"
    assert eh.class_group("富嶽賞", "4歳以上1000万下") == "2勝"
    assert eh.class_group("海の中道特別(2勝)", "4歳以上2勝クラス") == "2勝"
    assert eh.class_group("3歳以上3勝クラス", "3歳以上3勝クラス") == "3勝"
    assert eh.class_group("2歳新馬", "2歳新馬") == "新馬・未勝利"
    assert eh.class_group("ブラジルC(L)", "3歳以上オープン") == "OP・L"


def test_band_counts_cap_at_number_of_combos():
    counts = eh.band_counts(8)  # C(8,3) = 56
    assert sum(counts) == 56
    assert counts[:6] == [1, 2, 2, 5, 10, 30]
    assert counts[6:] == [6, 0, 0]
    assert sum(eh.band_counts(18)) == math.comb(18, 3)
    assert eh.band_index(1) == 0 and eh.band_index(7) == 3 and eh.band_index(500) == 8


def test_trio_dist_with_unit_beta_is_plain_harville():
    odds = {1: 2.0, 2: 4.0, 3: 6.0, 4: 10.0, 5: 20.0}
    q = eh.trio_dist(odds, (1.0, 1.0, 1.0))
    ref = mb.harville_trio(eh.win_probs(odds))
    assert q.keys() == ref.keys()
    assert all(abs(q[k] - ref[k]) < 1e-12 for k in q)
    assert abs(sum(q.values()) - 1.0) < 1e-12


def _sample_order(rng: random.Random, p: dict[int, float], beta: tuple[float, float, float]) -> tuple[int, int, int]:
    left = dict(p)
    out = []
    for b in beta:
        w = {h: v ** b for h, v in left.items()}
        r, acc = rng.random() * sum(w.values()), 0.0
        for h, v in w.items():
            acc += v
            if acc >= r:
                out.append(h)
                break
        left.pop(out[-1])
    return out[0], out[1], out[2]


def test_fit_beta_recovers_stage_exponents():
    rng = random.Random(7)
    true = (1.0, 0.8, 0.6)
    races = []
    for i in range(3000):
        odds = {h: rng.uniform(1.5, 60.0) for h in range(1, 11)}
        order = _sample_order(rng, eh.win_probs(odds), true)
        races.append(_race(f"r{i}", datetime.date(2015, 1, 1), odds, order))
    b1, b2, b3 = eh.fit_beta(races)
    assert abs(b1 - 1.0) < 0.08 and abs(b2 - 0.8) < 0.08 and abs(b3 - 0.6) < 0.08


def test_rules_count_hits_and_stakes():
    odds = {h: float(h + 1) for h in range(1, 10)}
    race = _race("r", datetime.date(2015, 1, 1), odds, (1, 2, 3), payout=1500, popularity=4)
    rules = eh.tickets_by_rule(race, (1.0, 1.0, 1.0))
    assert rules["P-1"]["stake"] == 100 and not rules["P-1"]["hit"]
    assert rules["P-5"]["stake"] == 500 and rules["P-5"]["hit"] and rules["P-5"]["payout"] == 1500
    assert rules["M-top5"]["hit"]  # 1-2-3 は最も確率の高い組
    for name in ("M-top5", "M-x_out_2", "M-cap3"):
        assert rules[name]["status"] == "chosen" and rules[name]["stake"] == 500


def test_bootstrap_is_deterministic_and_baseline_diff_is_zero():
    day = datetime.date(2015, 1, 1)
    per_day = {"A": {day + datetime.timedelta(d): (500, 300 * (d % 3)) for d in range(30)},
               "B": {day + datetime.timedelta(d): (500, 400 * (d % 2)) for d in range(30)}}
    one = eh.bootstrap(per_day, "A", n=200, seed=1)
    two = eh.bootstrap(per_day, "A", n=200, seed=1)
    assert one == two
    assert one["A"]["diff_lo95"] == 0.0 and one["A"]["diff_hi95"] == 0.0
    assert one["B"]["roi_lo95"] <= one["B"]["roi_hi95"]


def test_summarise_counts_pass_races_as_zero_stake():
    rng = random.Random(3)
    races = []
    for i in range(40):
        odds = {h: rng.uniform(1.5, 40.0) for h in range(1, 13 if i % 2 else 9)}
        order = tuple(sorted(odds, key=odds.get)[:3])
        races.append(_race(f"r{i}", datetime.date(2015, 1, 1) + datetime.timedelta(i // 4), odds, order,
                           payout=800, popularity=1 + i % 7, surface="芝" if i % 3 else "ダート"))
    s = eh.summarise(races, (1.0, 1.0, 1.0))
    assert s["rules"]["P-5"]["races"] == 40
    assert s["rules"]["P-5|8〜10頭"]["races"] == 20 and s["rules"]["P-5|11〜13頭"]["races"] == 20
    assert s["rules"]["M-x_out_2"]["diff_lo95"] == 0.0
    assert sum(b["hits"] for b in s["bands"]) == 40


def test_confirm_requires_candidates_written_in_spec(tmp_path):
    with pytest.raises(SystemExit):
        eh.main(["--warehouse", str(tmp_path), "--phase", "confirm"])
    with pytest.raises(SystemExit):
        eh.main(["--warehouse", str(tmp_path), "--phase", "confirm", "--beta", "1,1,1", "--candidates", "Z-not-in-spec"])
