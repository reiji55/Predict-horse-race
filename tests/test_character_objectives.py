"""
キャラごとの「見方」（目的関数・指数配分）のテスト。

2026-09-19 の反省点：λ を変えただけでは3人＋鳳が**同じ実力順・同じ歪み尺度**を見るので
買い目がほぼ重なり、鳳を降臨させる意味も無かった。そこでキャラごとに
  - objective     … 歪みの測り方（p−q か EV か）
  - score_weights … 実力を測るときの①②③の配分
を持たせた（logic/cards.py の冒頭を参照）。ここではその2つが**実際に選定を変える**ことを見る。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from logic import cards

ROOT = Path(__file__).resolve().parent.parent
with (ROOT / "config" / "cards.json").open(encoding="utf-8") as _f:
    CONFIG = json.load(_f)


def _horses():
    """
    人気馬(1番)と伏兵(4番)がいる4頭立て。
    4番は p が小さいが倍率が高いので、p−q では下位・EV では最上位になる。
    """
    return [
        {"num": 1, "speed_raw": 95.0, "aptitude_raw": 0.30, "human_raw": 0.30,
         "odds": 2.0, "p": 0.45, "q": 0.42},
        {"num": 2, "speed_raw": 90.0, "aptitude_raw": 0.55, "human_raw": 0.20,
         "odds": 5.0, "p": 0.25, "q": 0.17},
        {"num": 3, "speed_raw": 88.0, "aptitude_raw": 0.20, "human_raw": 0.60,
         "odds": 8.0, "p": 0.20, "q": 0.10},
        {"num": 4, "speed_raw": 70.0, "aptitude_raw": 0.95, "human_raw": 0.05,
         "odds": 30.0, "p": 0.10, "q": 0.03},
    ]


# --------------------------------------------------------- 目的関数

def test_pq_and_ev_rank_the_same_horses_differently():
    p = [h["p"] for h in _horses()]
    q = [h["q"] for h in _horses()]
    odds = [h["odds"] for h in _horses()]

    pq = cards.objective_values(cards.OBJECTIVE_PQ, p, q, odds)
    ev = cards.objective_values(cards.OBJECTIVE_EV, p, q, odds)

    assert pq == pytest.approx([0.03, 0.08, 0.10, 0.07])
    assert ev == pytest.approx([-0.10, 0.25, 0.60, 2.00])

    # p−q は3番が1位・4番は3位。EV は4番が断トツ1位。**同じデータで結論が変わる**
    assert cards._ranks_desc(pq) == [4, 2, 1, 3]
    assert cards._ranks_desc(ev) == [4, 3, 2, 1]


def test_unknown_objective_is_rejected_loudly():
    with pytest.raises(ValueError, match="未対応の目的関数"):
        cards.objective_values("kanpeki", [0.5], [0.4], [2.0])


def test_ev_falls_back_when_no_odds_were_fetched(caplog):
    """単勝オッズが全滅したときにEV勢のカードだけ消えないように、既定の尺度に落とす。"""
    horses = [{**h, "odds": None} for h in _horses()]
    cards.assign_character_ranks(horses, CONFIG, CONFIG["characters"]["gen"])

    # フォールバックしても源さん固有の適性重視pは維持する（共通pへ戻してはいけない）
    assert [h["sel_value_rank"] for h in horses] == [4, 2, 3, 1]
    assert "切り替えます" in caplog.text


# --------------------------------------------------- キャラ別の指数配分

def test_each_character_measures_ability_with_its_own_weights():
    """
    ケイは時計(①)重視、源さん・鳳は適性(②)重視。
    1番は時計最速・4番は適性最高なので、実力順の1位が入れ替わる。
    """
    def top_by(char_id):
        horses = _horses()
        cards.assign_character_ranks(horses, CONFIG, CONFIG["characters"][char_id])
        return next(h["num"] for h in horses if h["sel_base_rank"] == 1)

    assert top_by("kei") == 1      # 時計を信じる
    assert top_by("gen") == 4      # コース適性を信じる
    assert top_by("otori") == 4


def test_a_character_without_an_override_uses_the_shared_ranking():
    """哲さんは共通の見立てのまま（上書きを書かなければ base_rank をそのまま使う）。"""
    horses = _horses()
    for i, horse in enumerate(horses, start=1):
        horse["base_rank"] = i
    cards.assign_character_ranks(horses, CONFIG, CONFIG["characters"]["tetsu"])

    assert [h["sel_base_rank"] for h in horses] == [1, 2, 3, 4]


# ------------------------------------------------------- 買い目への波及

def test_the_characters_no_longer_buy_the_same_horses():
    """★ 本丸：同じレースで4人の買い目が全員一致しないこと。"""
    from logic import base_score

    horses = _horses()
    base_score.compute_base_scores(horses, CONFIG)
    marks = cards.assign_marks(horses, CONFIG)

    axes = {}
    for char_id in ("kei", "tetsu", "gen", "otori"):
        card = cards.generate_card_for_character(char_id, horses, CONFIG)
        assert card is not None, char_id
        cards.validate_card_invariants(card, marks, CONFIG["amt_unit"])
        axes[char_id] = card["bets"][0]["horses"][0]

    assert len(set(axes.values())) >= 2, f"全員が同じ軸を買っています: {axes}"
    assert axes["kei"] != axes["gen"], "堅いケイと穴の源さんが同じ軸なら分ける意味がない"


def test_cards_record_which_lens_they_used():
    """あとで「どの見方が効いたか」を集計できるよう、カードに目的関数を残す。"""
    horses = _horses()
    from logic import base_score
    base_score.compute_base_scores(horses, CONFIG)
    cards.assign_marks(horses, CONFIG)

    assert cards.generate_card_for_character("kei", horses, CONFIG)["objective"] == "pq"
    assert cards.generate_card_for_character("gen", horses, CONFIG)["objective"] == "ev"
    assert cards.generate_card_for_character("otori", horses, CONFIG)["objective"] == "ev"


def test_gen_keeps_the_ability_floor_on_the_shared_ranking():
    """
    源さんの軸下限（axis_base_rank_floor）は**共通の base_rank** で見る。
    キャラ固有の配分で持ち上げた順位で線引きしたら、安全弁にならないため。
    """
    horses = _horses()
    from logic import base_score
    base_score.compute_base_scores(horses, CONFIG)
    cards.assign_marks(horses, CONFIG)
    cards.assign_character_ranks(horses, CONFIG, CONFIG["characters"]["gen"])

    # 共通順位で下位の馬しかいない状況を作る（floor=1 にすると共通1位だけが軸になれる）
    ordered = cards.select_horses(horses, 0.75, axis_base_rank_floor=1,
                                  base_rank_key="sel_base_rank",
                                  value_rank_key="sel_value_rank")
    assert ordered[0]["base_rank"] == 1
