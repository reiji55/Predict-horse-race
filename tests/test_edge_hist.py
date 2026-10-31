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


def _explore(candidates: list[str], **over) -> dict:
    out = {"phase": "explore", "spec_commit": eh.SPEC_COMMIT, "beta": [1.0143, 0.8534, 0.7305],
           "rules": {k: {} for k in ("M-x_out_2", "M-top5", "P-5", "P-5|11R")},
           "candidates": [{"rule": c} for c in candidates]}
    out.update(over)
    return out


def test_confirm_with_zero_candidates_stops_before_reading_data(tmp_path, monkeypatch):
    monkeypatch.setattr(eh, "check_spec", lambda: None)
    monkeypatch.setattr(eh, "load_fixed_explore", lambda: _explore([]))

    def must_not_load(*args, **kwargs):
        raise AssertionError("データを読んではいけない")
    monkeypatch.setattr(eh, "load_races", must_not_load)
    with pytest.raises(SystemExit):
        eh.main(["--warehouse", str(tmp_path), "--phase", "confirm"])


def test_confirm_cannot_take_candidates_or_beta_from_the_command_line(tmp_path):
    with pytest.raises(SystemExit):
        eh.main(["--warehouse", str(tmp_path), "--phase", "confirm", "--candidates", "M-top5"])
    with pytest.raises(SystemExit):
        eh.main(["--warehouse", str(tmp_path), "--phase", "confirm", "--beta", "1,1,1"])


def test_confirm_plan_uses_only_the_fixed_explore_output():
    beta, found = eh.confirm_plan(_explore(["P-5|11R"]))
    assert beta == (1.0143, 0.8534, 0.7305) and found == ["P-5|11R"]
    with pytest.raises(eh.SealError):
        eh.confirm_plan(_explore([]))
    with pytest.raises(eh.SealError):  # 探す期間で計算していない作り方
        eh.confirm_plan(_explore(["M-cap3"]))
    with pytest.raises(eh.SealError):  # 本線そのものは候補にならない
        eh.confirm_plan(_explore(["M-x_out_2"]))
    with pytest.raises(eh.SealError):  # 固定した仕様で作った出力ではない
        eh.confirm_plan(_explore(["P-5|11R"], spec_commit="0" * 40))


def test_explore_output_must_match_first_commit_head_and_disk():
    a, b = b'{"candidates": []}\n', b'{"candidates": [{"rule": "M-top5"}]}\n'
    assert eh.explore_output_fixed(a, a, a)
    assert not eh.explore_output_fixed(b, a, a)  # 手元で書き換えた
    assert not eh.explore_output_fixed(b, b, a)  # 最初のコミットの後に書き換えた
    assert not eh.explore_output_fixed(None, a, a)
    assert not eh.explore_output_fixed(a, a, None)


def test_pinned_spec_allows_only_additions_after_section_8():
    pinned = "# 計画\n\n## 7. 限界\n\n- a\n"
    assert eh.pinned_spec_matches(pinned + "\n## 8. 結果\n\n- 候補は0件\n", pinned)
    assert not eh.pinned_spec_matches(pinned.replace("- a", "- b") + "\n## 8. 結果\n", pinned)


def test_repository_spec_still_matches_the_pinned_commit():
    try:
        eh.check_spec()
    except eh.SealError as exc:
        if "読めない" in str(exc):
            pytest.skip("浅い clone で、固定したコミットが無い")
        raise


def test_provenance_keeps_the_pinned_spec_commit():
    assert eh.SPEC_COMMIT == "f1f843605ae335c633eaf23febafe279a45466f7"
    out = __import__("json").loads((eh.ROOT / eh.EXPLORE_OUT).read_text(encoding="utf-8"))
    assert out["spec_commit"] == eh.SPEC_COMMIT and out["candidates"] == []


def test_summarise_with_keys_computes_only_those_rules_and_the_baseline(monkeypatch):
    calls = []
    real_choose = eh.pf.choose
    monkeypatch.setattr(eh.pf, "choose", lambda dist, x, n, cap, wx, **kw: calls.append((cap, wx)) or real_choose(dist, x, n, cap, wx))
    rng = random.Random(5)
    races = []
    for i in range(12):
        odds = {h: rng.uniform(1.5, 40.0) for h in range(1, 11)}
        order = tuple(sorted(odds, key=odds.get)[:3])
        races.append(_race(f"r{i}", datetime.date(2022, 1, 1) + datetime.timedelta(i // 3), odds, order,
                           popularity=1 + i % 6, race_num=11 if i % 2 else 5))
    s = eh.summarise(races, (1.0, 1.0, 1.0), {"P-5|11R"})
    assert set(s["rules"]) == {"P-5|11R", "M-x_out_2"} and "bands" not in s
    assert s["rules"]["P-5|11R"]["races"] == 6
    assert set(calls) == {(None, 2)}  # 本線の M-x_out_2 だけを組んだ
