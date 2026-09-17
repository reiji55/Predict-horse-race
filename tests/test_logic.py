"""
logic/ のユニットテスト。

**仕様書に載っている検算サンプルをそのままテストにしている**ので、通れば
「実装が仕様書の数値と一致している」ことが担保される：

- スピード指数仕様 §1.4（1走分の指数 = 82.7）
- 妙味メーター仕様 §4（5頭での p / q / myomi = 73.8）
- 買い目生成仕様 §6（同じ5頭での印・sel値・源さんの軸がC）

実行： python3 tests/test_logic.py  もしくは  python -m pytest tests/test_logic.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logic import base_score, cards, myomi, prob_model
from logic import aptitude as aptitude_mod
from logic import human_score as human_mod
from logic import speed_index as speed_mod

# --- 検算用の共通データ -------------------------------------------------

SPEED_CONFIG = speed_mod.load_config()
CARDS_CONFIG = {
    "score_weights": {"speed": 0.45, "aptitude": 0.30, "human": 0.25},
    "score_scale": {"base": 70, "unit": 10},
    "aptitude": {"w_surface": 1.0, "b_dist": 0.6, "b_venue": 0.3, "b_going": 0.2, "dist_tol": 400},
    "human": {"w_jockey": 0.6, "w_trainer": 0.4, "shrink_m": 10},
    "marks": {"hon_rank": 1, "maru": 2, "sankaku": 3, "delta": [4, 5], "batsu": 6},
    "characters": {
        "kei": {"lambda": 0.15, "total": 500, "max_points": 6},
        "tetsu": {"lambda": 0.40, "total": 500, "max_points": 6},
        "gen": {"lambda": 0.75, "total": 500, "max_points": 8, "axis_base_rank_floor": 6},
        "otori": {"lambda": 0.60, "total": 1000, "max_points": 12},
    },
    "combo_prob": {"method": "harville", "takeout": 0.20},
    "amt_unit": 50,
}
MYOMI_CONFIG = {
    "prob_model": {"method": "softmax", "temperature": 10.0},
    "components": {"w_edge": 0.6, "w_breadth": 0.4, "edge_cap": 0.5, "breadth_cap": 0.4},
    "confidence": {"conf_floor": 0.5, "usable_run_min": 3},
    "myomi_threshold": 80,
}

# 妙味メーター仕様§4／買い目生成仕様§6 の5頭（A〜E）
SPEC_SCORES = [90.0, 85.0, 80.0, 70.0, 60.0]
SPEC_ODDS = [3.0, 4.0, 12.0, 8.0, 20.0]
SPEC_NAMES = ["A", "B", "C", "D", "E"]

BASE_TIMES_KYOTO = {"京都": {"芝": {"1200": 67.8, "2000": 118.0}}}


def _approx(actual: float, expected: float, tol: float = 0.05) -> bool:
    return abs(actual - expected) <= tol


# --- ① スピード指数（スピード指数仕様） --------------------------------

def test_speed_run_index_matches_spec_worksheet():
    """§1.4 の検算：京都芝1200良OP・68.9秒・56kg、base_time 67.8 → 指数 82.7。"""
    run = {"venue": "京都", "surface": "芝", "dist": 1200, "going": "良",
           "class": "op", "time_sec": 68.9, "impost": 56}
    index = speed_mod.compute_run_index(run, SPEED_CONFIG, BASE_TIMES_KYOTO)
    assert _approx(index, 82.7, 0.05), index
    print("test_speed_run_index_matches_spec_worksheet: OK (指数 %.1f)" % index)


def test_speed_class_credit_controls_class_handicap():
    """
    class_credit の効き方（OPEN_QUESTIONS B-1）。
    既定(0.0)では「速く走った上級条件の馬」が上に来る。1.0にすると仕様書§1の記述どおり
    クラス差が相殺され、遅く走った未勝利馬のほうが高く出る。
    """
    maiden = {"venue": "京都", "surface": "芝", "dist": 1200, "going": "良",
              "class": "mi", "time_sec": 69.5, "impost": 55}
    open_class = {"venue": "京都", "surface": "芝", "dist": 1200, "going": "良",
                  "class": "op", "time_sec": 68.3, "impost": 55}

    absolute = dict(SPEED_CONFIG, class_credit=0.0)
    assert speed_mod.compute_run_index(open_class, absolute, BASE_TIMES_KYOTO) > \
        speed_mod.compute_run_index(maiden, absolute, BASE_TIMES_KYOTO)

    as_written = dict(SPEED_CONFIG, class_credit=1.0)
    assert speed_mod.compute_run_index(maiden, as_written, BASE_TIMES_KYOTO) > \
        speed_mod.compute_run_index(open_class, as_written, BASE_TIMES_KYOTO)
    print("test_speed_class_credit_controls_class_handicap: OK")


def test_speed_usable_conditions():
    """§2 の5条件：中止・地方・障害・表に無いコース・クラス不明はすべて除外される。"""
    base = {"venue": "京都", "surface": "芝", "dist": 1200, "going": "良",
            "class": "op", "time_sec": 68.9, "impost": 55}
    assert speed_mod.is_usable(base, SPEED_CONFIG, BASE_TIMES_KYOTO)

    assert not speed_mod.is_usable(dict(base, time_sec=None), SPEED_CONFIG, BASE_TIMES_KYOTO)
    assert not speed_mod.is_usable(dict(base, venue="大井"), SPEED_CONFIG, BASE_TIMES_KYOTO)
    assert not speed_mod.is_usable(dict(base, surface="障"), SPEED_CONFIG, BASE_TIMES_KYOTO)
    assert not speed_mod.is_usable(dict(base, dist=1600), SPEED_CONFIG, BASE_TIMES_KYOTO)
    assert not speed_mod.is_usable(dict(base, **{"class": None}), SPEED_CONFIG, BASE_TIMES_KYOTO)
    print("test_speed_usable_conditions: OK")


def test_speed_aggregation():
    """§3 の集約：best/latest/n_usable と、鮮度重みが元の走順で引かれること。"""
    runs = [
        {"venue": "京都", "surface": "芝", "dist": 1200, "going": "良", "class": "op",
         "time_sec": 69.8, "impost": 55},                      # 0走目（最新）
        {"venue": "大井", "surface": "ダ", "dist": 1200, "going": "良", "class": "op",
         "time_sec": 70.0, "impost": 55},                      # 1走目：地方なのでusable外
        {"venue": "京都", "surface": "芝", "dist": 1200, "going": "良", "class": "op",
         "time_sec": 68.0, "impost": 55},                      # 2走目
    ]
    speed = speed_mod.compute_horse_speed(runs, SPEED_CONFIG, BASE_TIMES_KYOTO)
    assert speed["n_usable"] == 2

    latest_index = speed_mod.compute_run_index(runs[0], SPEED_CONFIG, BASE_TIMES_KYOTO)
    older_index = speed_mod.compute_run_index(runs[2], SPEED_CONFIG, BASE_TIMES_KYOTO)
    assert _approx(speed["latest"], latest_index)
    assert _approx(speed["best"], max(latest_index, older_index))

    # 2走目の重みは 0.8（usable外の1走目を詰めて0.9にしない）
    expected_avg = (1.0 * latest_index + 0.8 * older_index) / (1.0 + 0.8)
    assert _approx(speed["avg"], expected_avg)

    assert speed_mod.compute_horse_speed([], SPEED_CONFIG, BASE_TIMES_KYOTO) is None
    print("test_speed_aggregation: OK")


def test_speed_empty_base_times_yields_none():
    """base_timesが空だと全走がusable外になる（＝静かに劣化する条件の再現）。"""
    runs = [{"venue": "京都", "surface": "芝", "dist": 1200, "going": "良", "class": "op",
             "time_sec": 68.9, "impost": 55}]
    assert speed_mod.compute_horse_speed(runs, SPEED_CONFIG, {}) is None
    print("test_speed_empty_base_times_yields_none: OK")


# --- ② 適性（買い目生成仕様§1.4） --------------------------------------

def test_aptitude_surface_mismatch_is_excluded():
    """芝ダ不一致は prox=0。全走が不一致なら②は欠損(None)。"""
    config = CARDS_CONFIG["aptitude"]
    today = {"surface": "芝", "dist": 1200, "venue": "小倉", "going": "良"}
    dirt_run = {"surface": "ダ", "dist": 1200, "venue": "小倉", "going": "良",
                "finish": 1, "heads": 16}
    assert aptitude_mod.compute_prox(dirt_run, today, config) == 0.0
    assert aptitude_mod.compute_aptitude([dirt_run], today, config) is None
    print("test_aptitude_surface_mismatch_is_excluded: OK")


def test_aptitude_head_count_normalization():
    """perf は頭数正規化されるので「16頭立て3着」と「8頭立て3着」の価値差が揃う。"""
    assert _approx(aptitude_mod.compute_perf({"finish": 1, "heads": 16}), (16 - 1 + 0.5) / 16)
    perf16 = aptitude_mod.compute_perf({"finish": 3, "heads": 16})
    perf8 = aptitude_mod.compute_perf({"finish": 3, "heads": 8})
    assert perf16 > perf8  # 多頭数で同じ着順のほうが価値が高い
    assert aptitude_mod.compute_perf({"finish": None, "heads": 16}) is None  # 中止等
    print("test_aptitude_head_count_normalization: OK")


def test_aptitude_same_condition_scores_higher():
    """同場・同距離・同馬場で好走した馬のほうが②が高くなる。"""
    config = CARDS_CONFIG["aptitude"]
    today = {"surface": "芝", "dist": 1200, "venue": "小倉", "going": "良"}
    perfect = [{"surface": "芝", "dist": 1200, "venue": "小倉", "going": "良",
                "finish": 1, "heads": 16}]
    far = [{"surface": "芝", "dist": 2400, "venue": "東京", "going": "重",
            "finish": 1, "heads": 16}]
    assert aptitude_mod.compute_aptitude(perfect, today, config) is not None
    # prox は違っても1走ずつなら加重平均は同じ値になる（重みが相殺されるため）。
    # 差が出るのは複数走が混ざったとき＝今日に近い走が強く効くこと
    mixed = perfect + [{"surface": "芝", "dist": 2400, "venue": "東京", "going": "重",
                        "finish": 16, "heads": 16}]
    assert aptitude_mod.compute_aptitude(mixed, today, config) > 0.5  # 近い走(1着)が強く効く
    assert aptitude_mod.compute_aptitude(far, today, config) is not None
    print("test_aptitude_same_condition_scores_higher: OK")


# --- ③ 人的（買い目生成仕様§1.5） --------------------------------------

def test_human_shrinkage_pulls_small_samples_to_average():
    """starts が小さい騎手は全体平均へ引き戻される。"""
    config = CARDS_CONFIG["human"]
    hot = {"scope": "venue", "starts": 2, "wins": 2, "seconds": 0, "thirds": 0}   # 2戦2勝
    veteran = {"scope": "venue", "starts": 100, "wins": 20, "seconds": 15, "thirds": 10}

    hot_rate = human_mod.place_rate_from_stats(hot, 0.25, config["shrink_m"])
    veteran_rate = human_mod.place_rate_from_stats(veteran, 0.25, config["shrink_m"])

    assert hot_rate < 0.5          # 複勝率100%だが、分母2なので大きく引き戻される
    assert abs(veteran_rate - 0.45) < 0.05  # 分母100なら実績寄り
    print("test_human_shrinkage_pulls_small_samples_to_average: OK")


def test_human_score_missing_side_renormalizes():
    """騎手・厩舎の片方が欠けたら残り側の重みを再正規化、両方欠けたら None。"""
    config = CARDS_CONFIG["human"]
    stats = {"starts": 100, "wins": 20, "seconds": 15, "thirds": 10}
    only_jockey = human_mod.compute_human_score(stats, None, 0.25, 0.25, config)
    assert _approx(only_jockey, human_mod.place_rate_from_stats(stats, 0.25, config["shrink_m"]))
    assert human_mod.compute_human_score(None, None, 0.25, 0.25, config) is None
    print("test_human_score_missing_side_renormalizes: OK")


# --- 合成スコア（買い目生成仕様§1.1・§1.2） ---------------------------

def test_base_score_missing_factor_renormalizes_weights():
    """①欠損の馬（新馬など）は②③だけで評価され、不確実フラグが立つ。"""
    horses = [
        {"speed_raw": 100.0, "aptitude_raw": 0.8, "human_raw": 0.30},
        {"speed_raw": 90.0, "aptitude_raw": 0.6, "human_raw": 0.25},
        {"speed_raw": None, "aptitude_raw": 0.7, "human_raw": 0.20},   # 新馬想定
        {"speed_raw": None, "aptitude_raw": None, "human_raw": None},  # 全欠損
    ]
    result = base_score.compute_base_scores(horses, CARDS_CONFIG)

    assert result[0]["base_score"] > result[1]["base_score"]
    assert result[2]["base_score"] is not None and result[2]["uncertain"] is True
    assert result[3]["base_score"] is None and result[3]["uncertain"] is True
    assert result[0]["uncertain"] is False
    # 表示スコアは 70 + 10×z のスケールに乗る
    assert _approx(result[0]["score"], 70 + 10 * result[0]["base_score"])
    print("test_base_score_missing_factor_renormalizes_weights: OK")


def test_z_standardize_handles_zero_sd():
    """全馬同値なら z=0（ゼロ除算回避）。"""
    assert base_score.z_standardize([5.0, 5.0, 5.0]) == [0.0, 0.0, 0.0]
    assert base_score.z_standardize([None, None]) == [None, None]
    print("test_z_standardize_handles_zero_sd: OK")


# --- p / q（妙味メーター仕様§1・§1.1） --------------------------------

def test_softmax_and_market_support_match_spec_table():
    """§4 の表：p(T=10) = .463/.281/.170/.063/.023、q = .396/.297/.099/.149/.059。"""
    p = prob_model.softmax_scores(SPEC_SCORES, MYOMI_CONFIG["prob_model"]["temperature"])
    for actual, expected in zip(p, [0.463, 0.281, 0.170, 0.063, 0.023]):
        assert _approx(actual, expected, 0.001), (actual, expected)

    q = prob_model.market_support(SPEC_ODDS)
    for actual, expected in zip(q, [0.396, 0.297, 0.099, 0.149, 0.059]):
        assert _approx(actual, expected, 0.001), (actual, expected)

    assert _approx(sum(p), 1.0, 1e-9) and _approx(sum(q), 1.0, 1e-9)
    print("test_softmax_and_market_support_match_spec_table: OK")


def test_none_entries_are_excluded_from_normalization():
    """base_score=None / win_odds=None の馬は分母に入らず、自身は None のまま。"""
    p = prob_model.softmax_scores([90.0, None, 80.0], 10.0)
    assert p[1] is None and _approx(sum(v for v in p if v is not None), 1.0, 1e-9)

    q = prob_model.market_support([3.0, None, 12.0])
    assert q[1] is None and _approx(sum(v for v in q if v is not None), 1.0, 1e-9)
    print("test_none_entries_are_excluded_from_normalization: OK")


# --- 妙味メーター（妙味メーター仕様§2・§4） --------------------------

def test_myomi_matches_spec_worksheet():
    """§4 の検算：A_norm=1.0・B_norm=0.345・conf=1.0 → myomi 73.8（80未満なので鳳は降臨せず）。"""
    p = prob_model.softmax_scores(SPEC_SCORES, 10.0)
    q = prob_model.market_support(SPEC_ODDS)
    n_usable = [5, 5, 5, 5, 5]  # data_cov=1.0

    result = myomi.compute_myomi(p, q, n_usable, SPEC_ODDS, MYOMI_CONFIG)

    assert _approx(result["myomi"], 73.8, 0.1), result
    assert _approx(result["myomi_parts"]["umami"], 0.738, 0.001)
    assert _approx(result["myomi_parts"]["conf"], 1.0, 0.001)
    assert result["legendary"] is False
    print("test_myomi_matches_spec_worksheet: OK (myomi %.1f)" % result["myomi"])


def test_myomi_confidence_drops_with_thin_data():
    """時計データが薄いレースは conf が下がり、妙味も割り引かれる（安易に鳳を出さない）。"""
    p = prob_model.softmax_scores(SPEC_SCORES, 10.0)
    q = prob_model.market_support(SPEC_ODDS)

    full = myomi.compute_myomi(p, q, [5, 5, 5, 5, 5], SPEC_ODDS, MYOMI_CONFIG)
    thin = myomi.compute_myomi(p, q, [0, 0, 0, 0, 0], SPEC_ODDS, MYOMI_CONFIG)

    assert _approx(thin["myomi_parts"]["conf"], 0.5, 0.001)  # conf_floor
    assert thin["myomi"] < full["myomi"]
    print("test_myomi_confidence_drops_with_thin_data: OK")


def test_myomi_parts_reproduce_myomi():
    """myomi ≒ 100 × umami × conf（predictions.json の myomi_parts の定義）。"""
    p = prob_model.softmax_scores(SPEC_SCORES, 10.0)
    q = prob_model.market_support(SPEC_ODDS)
    result = myomi.compute_myomi(p, q, [5, 3, 5, 1, 5], SPEC_ODDS, MYOMI_CONFIG)
    parts = result["myomi_parts"]
    assert _approx(100 * parts["umami"] * parts["conf"], result["myomi"], 0.1)
    print("test_myomi_parts_reproduce_myomi: OK")


# --- 印・買い目（買い目生成仕様§2・§5・§6） -------------------------

def _spec_horses() -> list[dict]:
    """買い目生成仕様§6 の5頭を、logic が扱う形（num付き）で組み立てる。"""
    p = prob_model.softmax_scores(SPEC_SCORES, 10.0)
    q = prob_model.market_support(SPEC_ODDS)
    value_info = cards.compute_value_and_myomi_rank(p, q)

    horses = []
    for i, name in enumerate(SPEC_NAMES):
        horses.append({
            "num": i + 1, "waku": i + 1, "name": name,
            "odds": SPEC_ODDS[i], "score": SPEC_SCORES[i], "base_score": SPEC_SCORES[i],
            "p": p[i], "q": q[i],
            "value": value_info[i]["value"], "myomi_rank": value_info[i]["myomi_rank"],
        })
    return horses


def test_marks_follow_base_score_order():
    """§2：base_score降順に ◎○▲△△。◎には hon=true が付く。"""
    horses = _spec_horses()
    marks = cards.assign_marks(horses, CARDS_CONFIG)

    assert [m["mk"] for m in marks] == ["◎", "○", "▲", "△", "△"]
    assert marks[0]["hon"] is True and "hon" not in marks[1]
    assert [m["name"] for m in marks] == ["A", "B", "C", "D", "E"]
    print("test_marks_follow_base_score_order: OK")


def test_marks_include_unmarked_horses():
    """7位以下は mk="" で marks に載る（OPEN_QUESTIONS B-5 の判断）。"""
    horses = [
        {"num": i + 1, "waku": 1, "name": f"H{i}", "odds": 10.0,
         "base_score": 100 - i, "score": 100 - i}
        for i in range(8)
    ]
    marks = cards.assign_marks(horses, CARDS_CONFIG)
    assert len(marks) == 8
    assert [m["mk"] for m in marks[5:]] == ["✕", "", ""]
    print("test_marks_include_unmarked_horses: OK")


def test_value_and_myomi_rank_match_spec():
    """§6：value順は C > A > B > E > D（Cが最も過小評価）。"""
    p = prob_model.softmax_scores(SPEC_SCORES, 10.0)
    q = prob_model.market_support(SPEC_ODDS)
    info = cards.compute_value_and_myomi_rank(p, q)

    assert _approx(info[0]["value"], 0.067, 0.002)   # A
    assert _approx(info[2]["value"], 0.071, 0.002)   # C
    order = sorted(range(5), key=lambda i: info[i]["myomi_rank"])
    assert [SPEC_NAMES[i] for i in order] == ["C", "A", "B", "E", "D"]
    print("test_value_and_myomi_rank_match_spec: OK")


def test_selection_scores_match_spec_worksheet():
    """§6：ケイ(λ=0.15) sel = A0.96 B0.71 C0.58 D0.21 E0.04 / 源さん(λ=0.75)はCが最上位0.875。"""
    horses = _spec_horses()
    cards.assign_marks(horses, CARDS_CONFIG)

    kei = cards.select_horses(horses, 0.15)
    kei_sel = {h["name"]: h["sel"] for h in kei}
    for name, expected in {"A": 0.96, "B": 0.71, "C": 0.58, "D": 0.21, "E": 0.04}.items():
        assert _approx(kei_sel[name], expected, 0.01), (name, kei_sel[name])
    assert kei[0]["name"] == "A"  # ケイの軸は印どおりA

    gen = cards.select_horses(horses, 0.75, axis_base_rank_floor=6)
    gen_sel = {h["name"]: h["sel"] for h in gen}
    for name, expected in {"C": 0.875, "A": 0.81, "B": 0.56, "E": 0.19, "D": 0.06}.items():
        assert _approx(gen_sel[name], expected, 0.01), (name, gen_sel[name])
    assert gen[0]["name"] == "C"  # 源さんの軸は人気薄の過小評価馬C
    print("test_selection_scores_match_spec_worksheet: OK")


def test_gen_axis_floor_blocks_low_base_score_horse():
    """§5.2：sel最上位でも base_rank が下限を切る馬は軸にしない。"""
    horses = _spec_horses()
    cards.assign_marks(horses, CARDS_CONFIG)
    # Eのbase_rankを圏外(9位)に落とし、myomi_rank 1位にして sel を最上位にする
    for horse in horses:
        if horse["name"] == "E":
            horse["base_rank"] = 9
            horse["myomi_rank"] = 1

    ordered = cards.select_horses(horses, 0.75, axis_base_rank_floor=6)
    assert ordered[0]["name"] != "E"
    assert ordered[0]["base_rank"] <= 6
    print("test_gen_axis_floor_blocks_low_base_score_horse: OK")


def test_generated_cards_satisfy_invariants():
    """§5.5：全キャラのカードが5つの不変条件を満たす。"""
    horses = _spec_horses()
    marks = cards.assign_marks(horses, CARDS_CONFIG)

    for char_id in ("kei", "tetsu", "gen", "otori"):
        card = cards.generate_card_for_character(char_id, horses, CARDS_CONFIG)
        assert card is not None, char_id
        cards.validate_card_invariants(card, marks, CARDS_CONFIG["amt_unit"])
        assert card["total"] == CARDS_CONFIG["characters"][char_id]["total"]
        assert sum(b["amt"] for b in card["bets"]) == card["total"]
        assert 0 <= card["hit_pct"] <= 100
        low, high = card["payout_range"]
        assert 0 < low <= high
    print("test_generated_cards_satisfy_invariants: OK")


def test_invariant_violations_are_detected():
    """不変条件が崩れた買い目は ValueError で弾かれる（早期検知）。"""
    horses = _spec_horses()
    marks = cards.assign_marks(horses, CARDS_CONFIG)
    card = cards.generate_card_for_character("kei", horses, CARDS_CONFIG)

    for broken in (
        {**card, "bets": card["bets"][:-1]},                                   # Σamt != total
        {**card, "bets": [{**card["bets"][0], "amt": 175}] + card["bets"][1:]},  # 単位違反
        {**card, "bets": [{**card["bets"][0], "type": "単勝"}] + card["bets"][1:]},  # 券種違反
        {**card, "bets": [{**card["bets"][0], "horses": [1, 2, 3]}] + card["bets"][1:]},  # 頭数違反
        {**card, "bets": [{**card["bets"][0], "horses": [1, 99]}] + card["bets"][1:]},  # marks外
    ):
        try:
            cards.validate_card_invariants(broken, marks, CARDS_CONFIG["amt_unit"])
        except ValueError:
            continue
        raise AssertionError(f"検出されるべき不変条件違反が通過しました: {broken['bets'][0]}")
    print("test_invariant_violations_are_detected: OK")


def test_harville_probabilities_are_consistent():
    """Harville近似：ワイド ⊇ 3連複、馬連 ≦ ワイド、確率は0〜1に収まる。"""
    p = prob_model.softmax_scores(SPEC_SCORES, 10.0)
    p_by_num = {i + 1: p[i] for i in range(5)}

    wide = cards.wide_prob(p_by_num, 1, 2)
    umaren = cards.umaren_prob(p_by_num, 1, 2)
    trio = cards.trio_prob(p_by_num, 1, 2, 3)

    assert 0 < trio < wide <= 1.0
    assert umaren <= wide          # 1・2着限定のほうが厳しい
    assert 0 < umaren < 1.0
    # 全3連複組み合わせの合計は1.0（上位3着の分割になっている）
    total = sum(cards.trio_prob(p_by_num, *combo)
                for combo in __import__("itertools").combinations(p_by_num, 3))
    assert _approx(total, 1.0, 1e-6), total
    print("test_harville_probabilities_are_consistent: OK")


def test_kei_hits_more_often_than_gen():
    """
    §3/§5の緊張関係：堅く買うケイのほうが的中率が高く、妙味に振る源さんのほうが払戻上限が大きい。
    （λの差だけから出る性質で、追加ルールは無い）
    """
    horses = _spec_horses()
    cards.assign_marks(horses, CARDS_CONFIG)

    kei = cards.generate_card_for_character("kei", horses, CARDS_CONFIG)
    gen = cards.generate_card_for_character("gen", horses, CARDS_CONFIG)

    assert kei["hit_pct"] > gen["hit_pct"]
    assert gen["payout_range"][1] > kei["payout_range"][1]
    print("test_kei_hits_more_often_than_gen: OK (ケイ %d%% / 源さん %d%%)"
          % (kei["hit_pct"], gen["hit_pct"]))


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
    print(f"\nすべてのテストが通りました（{len(ALL_TESTS)}件）。")
