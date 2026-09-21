"""
印の付与・キャラ別買い目生成（買い目生成仕様_v1.md §2〜§5）

入力：base_score（logic/base_score）、p・q（logic/prob_model）、win_odds
出力：marks[]（印つき）、cards[]（キャラ別買い目、predictions.jsonスキーマ準拠）

設定：config/cards.json（marks, characters, combo_prob, amt_unit）

--- 印（§2） ---

base_score の降順に機械的に付ける。印は全キャラ共通の「新聞の見立て」で、キャラ差は買い目で出す。
1位◎(hon) / 2位○ / 3位▲ / 4〜5位△ / 6位✕ / 7位以下は無印（mk="" で marks には載る）。

  ⚠ **marks の範囲（要レビュー）**：仕様§2は「7位以下は無印（marksに載せるが mk 空）」＝
  **全出走馬を載せる**と読める一方、predictions.sample.json の marks は上位5頭だけになっている。
  本実装は仕様の記述どおり **base_score が付いた全馬を載せる**（mk は7位以下で空文字）。
  理由は、データスキーマ仕様§3が「marks の score と odds は後方検証のために残す」と定めており、
  上位数頭だけでは重みの再計算ができないため。UI は mk が空の行を描画しなければよい。
  **この判断は docs/OPEN_QUESTIONS.md B-5 に記録。**

--- 買い目（§5） ---

    sel_i = (1 − λ) × base_rank_norm_i + λ × myomi_rank_norm_i

λ はキャラ別の「妙味寄せ係数」（ケイ0.15 / 哲0.40 / 源0.75 / 鳳0.60）。
sel の高い順に軸・相手を組み、券種テンプレはキャラごとに固定（§5.1 の表）。

--- キャラごとに「見方」を変える（仕様§5の拡張・OPEN_QUESTIONS B-9） ---

λ だけを変えても、3人は**同じ実力順・同じ歪み尺度**を見ているので買い目が似通う。
2026-09-19 はそれが露骨に出た（3人＋鳳の買い目がほぼ同じ馬で埋まり、全滅）。
そこで config/cards.json の characters[] に2つの上書きを足した：

    objective      … 歪みの測り方。"pq"（p−q・絶対エッジ）か "ev"（p×odds−1・相対エッジ）
    score_weights  … そのキャラが実力を測るときの①②③の配分（省略時は共通の配分）

これで各キャラが**別々の仮説**になる。どの仮説が正しいかは成績が溜まってから決める
（いまの配分はどれも「正解」として入れていない。競わせるための散らし方）。
源さんだけは `axis_base_rank_floor`（=6）があり、**base_score が下位すぎる馬は軸にしない**
（妙味に振っても実力の裏づけは残す＝ただのギャンブルにしない・§5.2）。
鳳のカードは `legendary=true` のレースのみ生成する（§5.4）。

--- 券種確率と市場EV（§5.3 + 2026-09-20拡張） ---

券種の成立確率は、各キャラ固有の p から **Harville 式**で近似する。
払戻レンジ表示は従来どおり近似だが、鳳の降臨判定に使うEVは
馬連・ワイド・3連複の**実市場オッズ**を combo_odds から参照する。

    馬連(i,j)   = p_i·p_j/(1−p_i) + p_j·p_i/(1−p_j)
    3連複(i,j,k) = 全6順列の Harville 逐次確率の和
    ワイド(i,j)  = i,j が上位3着以内に共存する確率 = Σ_{k∉{i,j}} 3連複(i,j,k)

`hit_pct` と `payout_range` は、**上位3着の順列を全列挙**して
「1点でも当たる確率」と「当たったときの払戻合計の最小〜最大」を集計する
（券種をまたいだ重複当たりを正しく扱うため。近似の近似を避ける）。
払戻は「近似確率 → フェア配当(1/確率) → 控除率20%で割引」で概算する（§5.3 のとおり**あくまで概算**）。

--- 不変条件（§5.5） ---

  1. すべての amt は amt_unit の倍数
  2. Σ bets[].amt == total
  3. 券種は ワイド / 馬連 / 3連複 のみ
  4. horses の頭数が券種と一致（ワイド・馬連=2、3連複=3）
  5. horses の馬番が marks に存在する

  **amt_unit は100円固定。** JRAで実際に購入できる単位に合わせ、50円・150円の仮想買い目は生成しない。
"""
from __future__ import annotations

import itertools
import logging
from typing import Any

from logic import base_score, prob_model

logger = logging.getLogger("logic.cards")

WIDE = "ワイド"
UMAREN = "馬連"
SANRENPUKU = "3連複"

BET_TYPES = (WIDE, UMAREN, SANRENPUKU)
BET_SIZE = {WIDE: 2, UMAREN: 2, SANRENPUKU: 3}

# 券種テンプレ（買い目生成仕様§5.1の表を具体化したもの）。
# タプルは「選定順（sel降順）の何番目の馬か」のインデックス。金額の合計は必ず total と一致する。
# min_horses を満たす最初のテンプレを採用する（少頭数レースへのフォールバック）。
CARD_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    # ケイ：ワイド軸厚め＋馬連。印どおりの堅い買い目
    "kei": [
        {"min_horses": 3, "bets": [(WIDE, (0, 1), 200), (WIDE, (0, 2), 100),
                                   (WIDE, (1, 2), 100), (UMAREN, (0, 1), 100)]},
        {"min_horses": 2, "bets": [(WIDE, (0, 1), 300), (UMAREN, (0, 1), 200)]},
    ],
    # 哲さん：馬連＋ワイド＋3連複少点数。印軸＋少し妙味
    "tetsu": [
        {"min_horses": 4, "bets": [(UMAREN, (0, 1), 200), (UMAREN, (0, 2), 100),
                                   (SANRENPUKU, (0, 1, 2), 100), (WIDE, (0, 3), 100)]},
        {"min_horses": 3, "bets": [(UMAREN, (0, 1), 200), (UMAREN, (0, 2), 100),
                                   (SANRENPUKU, (0, 1, 2), 200)]},
    ],
    # 源さん：3連複フォーメーション中心。妙味馬を軸に散らす
    "gen": [
        {"min_horses": 4, "bets": [(SANRENPUKU, (0, 1, 2), 100), (SANRENPUKU, (0, 1, 3), 100),
                                   (SANRENPUKU, (0, 2, 3), 100), (WIDE, (0, 1), 200)]},
        {"min_horses": 3, "bets": [(SANRENPUKU, (0, 1, 2), 200), (WIDE, (0, 1), 200),
                                   (WIDE, (0, 2), 100)]},
    ],
    # 鳳：全券種ミックス・点数増（降臨時のみ）
    # **金額は他のキャラと同じ500円**。2026-09-19 の降臨（妙味87.5）が他3人とほぼ同じ買い目のまま
    # 倍額を張って外れたため、鳳が「他と違う予想を出せている」と確認できるまで同額に落とした
    # （docs/OPEN_QUESTIONS.md B-8）。
    "otori": [
        {"min_horses": 4, "bets": [(WIDE, (0, 1), 100), (WIDE, (0, 2), 100),
                                   (UMAREN, (0, 1), 100),
                                   (SANRENPUKU, (0, 1, 2), 100), (SANRENPUKU, (0, 1, 3), 100)]},
        {"min_horses": 3, "bets": [(WIDE, (0, 1), 200), (UMAREN, (0, 1), 100),
                                   (SANRENPUKU, (0, 1, 2), 200)]},
    ],
}


# ---------------------------------------------------------------- 印（§2）

def assign_marks(horses_with_base_score: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    base_score順に印(mk)を付与する（買い目生成仕様§2）。

    各馬に "base_rank"（1始まり・base_scoreが無い馬はNone）と "mk" を書き込み、
    predictions.json の marks[] 相当のリストを返す（base_score が付いた馬のみ・base_rank順）。
    """
    marks_config = config["marks"]
    ranked = sorted(
        (h for h in horses_with_base_score if h.get("base_score") is not None),
        key=lambda h: h["base_score"],
        reverse=True,
    )

    for horse in horses_with_base_score:
        horse["base_rank"] = None
        horse["mk"] = ""

    marks: list[dict[str, Any]] = []
    for rank, horse in enumerate(ranked, start=1):
        horse["base_rank"] = rank
        horse["mk"] = _mark_for_rank(rank, marks_config)

        mark: dict[str, Any] = {"mk": horse["mk"]}
        if rank == marks_config["hon_rank"]:
            mark["hon"] = True
        mark.update({
            "num": horse.get("num"),
            "waku": horse.get("waku"),
            "name": horse.get("name"),
            "odds": horse.get("odds"),
            "score": round(horse["score"], 1) if horse.get("score") is not None else None,
            # Top3は勝率とは別の「3着以内に残る適性」。確率ではなく相手候補ランキング用。
            "top3_score": round(horse["top3_raw"], 4) if horse.get("top3_raw") is not None else None,
            "top3_rank": horse.get("top3_rank"),
            "top3_same_dist_runs": horse.get("top3_same_dist_runs", 0),
            "top3_same_dist_hits": horse.get("top3_same_dist_hits", 0),
        })
        marks.append(mark)

    return marks


def _mark_for_rank(rank: int, marks_config: dict[str, Any]) -> str:
    if rank == marks_config["hon_rank"]:
        return "◎"
    if rank == marks_config["maru"]:
        return "○"
    if rank == marks_config["sankaku"]:
        return "▲"
    if rank in marks_config["delta"]:
        return "△"
    if rank == marks_config["batsu"]:
        return "✕"
    return ""  # 7位以下は無印（marksには載せる）


# ------------------------------------------------- 妙味との接続（§4）

def compute_value_and_myomi_rank(p: list[float | None], q: list[float | None]) -> list[dict[str, Any]]:
    """
    value = p − q とそのレース内順位(myomi_rank)を計算する（買い目生成仕様§4）。

    value が正＝モデルが市場より高く見ている＝過小評価。myomi_rank は1位＝最も過小評価。
    p か q が欠損している馬は value=None・myomi_rank=None（順位付けの対象外）。

    これは**表示・既定用**の歪み尺度。買い目の選定にどちらの尺度を使うかはキャラごとに
    変わる（`assign_character_ranks` と OBJECTIVES を参照）。
    """
    values = objective_values(OBJECTIVE_PQ, p, q, [None] * len(p))
    return [
        {"value": value, "myomi_rank": rank}
        for value, rank in zip(values, _ranks_desc(values))
    ]


# ------------------------------------------- 歪みの測り方（目的関数）

OBJECTIVE_PQ = "pq"   # 絶対エッジ：p − q。「確率で何ポイント得か」
OBJECTIVE_EV = "ev"   # 相対エッジ：p × odds − 1。「1円が何円になるか」
OBJECTIVES = (OBJECTIVE_PQ, OBJECTIVE_EV)

DEFAULT_OBJECTIVE = OBJECTIVE_PQ


def objective_values(objective: str, p: list[float | None], q: list[float | None],
                     win_odds: list[float | None]) -> list[float | None]:
    """
    歪みの大きさを、指定した目的関数で測る。

    **なぜ2種類あるか。** p−q は確率の差なので、人気馬のわずかな過小評価が上位に来やすい。
    p×odds−1 は倍率込みなので、人気薄の大きな期待値を上位に出す。2026-09-19 の検証では
    実際の上位3頭の順位和が p−q で最下位（63）、EV は荒れたレースで最良（24）だった。
    どちらが正しいかはまだ判らないので、**キャラごとに別の尺度を持たせて競わせる**
    （ケイ＝p−q／源さん・鳳＝EV。docs/OPEN_QUESTIONS.md B-9）。
    """
    if objective == OBJECTIVE_PQ:
        return [
            p_i - q_i if (p_i is not None and q_i is not None) else None
            for p_i, q_i in zip(p, q)
        ]
    if objective == OBJECTIVE_EV:
        return [
            p_i * odds_i - 1.0
            if (p_i is not None and odds_i is not None and odds_i > 0) else None
            for p_i, odds_i in zip(p, win_odds)
        ]
    raise ValueError(f"未対応の目的関数です: {objective}（使えるのは {OBJECTIVES}）")


def _ranks_desc(values: list[float | None]) -> list[int | None]:
    """値の降順の順位（1始まり）。None は順位なし。"""
    order = sorted(
        (i for i, v in enumerate(values) if v is not None),
        key=lambda i: values[i],
        reverse=True,
    )
    ranks: dict[int, int] = {index: rank for rank, index in enumerate(order, start=1)}
    return [ranks.get(i) for i in range(len(values))]


def _ranks_asc(values: list[float | None]) -> list[int | None]:
    """値の昇順の順位（1始まり）。単勝オッズの人気順などに使う。"""
    order = sorted(
        (i for i, v in enumerate(values) if v is not None),
        key=lambda i: values[i],
    )
    ranks: dict[int, int] = {index: rank for rank, index in enumerate(order, start=1)}
    return [ranks.get(i) for i in range(len(values))]


def assign_place_partner_ranks(horses: list[dict[str, Any]]) -> None:
    """
    Top3生スコアと市場人気から、券種別の相手候補選定に必要な順位を付ける。

    top3_rank       : Top3 Scoreの高い順
    market_short_rank: 単勝オッズが低い順（保険型）
    market_long_rank : 単勝オッズが高い順（妙味型）

    Top3 Scoreが無い馬はtop3_rank=None。Win側のscore/pには一切影響しない。
    """
    top3 = [h.get("top3_raw") for h in horses]
    odds = [h.get("odds") for h in horses]
    top3_ranks = _ranks_desc(top3)
    market_short = _ranks_asc(odds)
    market_long = _ranks_desc(odds)
    for horse, t_rank, short_rank, long_rank in zip(
            horses, top3_ranks, market_short, market_long):
        horse["top3_rank"] = t_rank
        horse["market_short_rank"] = short_rank
        horse["market_long_rank"] = long_rank


def order_place_partners(horses: list[dict[str, Any]], mode: str,
                         config: dict[str, Any],
                         axis_num: int | None = None) -> list[dict[str, Any]]:
    """
    ワイド/3連複の相手候補をTop3 Score中心で並べる。

    insurance: Top3再現性 + 市場人気（低オッズ）を少し重視
    balanced : Top3再現性のみ
    edge     : Top3再現性 + 市場不人気（高オッズ）を少し重視

    ここでのmarket成分は**EVではない**。未校正Top3 Scoreを確率扱いしないため、
    「相手候補の順位を散らす」用途だけに限定する。
    """
    mode_cfg = config.get("top3", {}).get("partner_modes", {}).get(mode)
    if not mode_cfg:
        return []

    candidates = [
        h for h in horses
        if h.get("top3_rank") is not None and h.get("num") is not None
        and h.get("num") != axis_num
    ]
    if not candidates:
        return []

    top3_values = [float(h["top3_raw"]) for h in candidates]
    top3_min, top3_max = min(top3_values), max(top3_values)
    market_key = "market_long_rank" if mode == "edge" else "market_short_rank"
    market_candidates = [h[market_key] for h in candidates if h.get(market_key) is not None]
    market_total = max(market_candidates) if market_candidates else 0

    for horse in candidates:
        # 順位ではなく生スコアの差を使う。Top3適性が僅差ならmarket側の差が効く。
        if top3_max > top3_min:
            t = (float(horse["top3_raw"]) - top3_min) / (top3_max - top3_min)
        else:
            t = 1.0
        market = 0.0
        if mode_cfg.get("market", 0) > 0 and horse.get(market_key) is not None and market_total:
            market = _rank_norm(horse[market_key], market_total) or 0.0
        horse["place_partner_sel"] = mode_cfg.get("top3", 1.0) * t + mode_cfg.get("market", 0.0) * market

    return sorted(
        candidates,
        key=lambda h: (h["place_partner_sel"], h.get("top3_raw") or -1.0),
        reverse=True,
    )


def _bet_horses_with_place_policy(
        bet_type: str, indices: tuple[int, ...],
        ordered: list[dict[str, Any]], place_pool: list[dict[str, Any]]) -> list[int]:
    """
    テンプレの0番をWin側の軸として残し、1番以降をTop3相手候補へ差し替える。

    ワイド/3連複だけに適用する。候補不足や重複が起きる場合は従来のorderedへフォールバック。
    """
    original = [ordered[i]["num"] for i in indices]
    if bet_type not in (WIDE, SANRENPUKU) or not place_pool:
        return original

    mapped: list[int] = []
    for index in indices:
        if index == 0:
            mapped.append(ordered[0]["num"])
            continue
        pool_index = index - 1
        if pool_index >= len(place_pool):
            return original
        mapped.append(place_pool[pool_index]["num"])

    if len(set(mapped)) != len(mapped):
        return original
    return mapped


def assign_character_ranks(horses: list[dict[str, Any]], config: dict[str, Any],
                           char_config: dict[str, Any], temperature: float = 10.0) -> None:
    """
    そのキャラ固有の「実力順（sel_base_rank）」と「歪み順（sel_value_rank）」を horses に書き込む。

    - `score_weights` の上書きがあれば、そのキャラは**違う指数配分で実力を測る**。
      無ければ全キャラ共通の base_rank をそのまま使う。
    - `objective` で歪みの測り方を切り替える（p−q か EV か）。

    印（marks）は共通のまま。ここで変えるのは**買い目の選定順だけ**なので、
    「同じ新聞を見て、別の見方で買う3人」という建て付けは保たれる。
    """
    weights = char_config.get("score_weights")
    has_raw_factors = any(base_score.count_usable_factors(h) for h in horses)

    # キャラ固有の能力配分を使うなら、順位だけでなく score → p まで同じ配分で再計算する。
    # 以前は「適性重視の源さん」が、EVだけは共通45/30/25モデルのpを使っており内部矛盾があった。
    if weights and has_raw_factors:
        char_base_scores = base_score.composite_scores(horses, weights)
        char_scores = [base_score.to_display_score(v, config) for v in char_base_scores]
        char_p = prob_model.softmax_scores(char_scores, temperature)
        base_ranks = _ranks_desc(char_base_scores)
    else:
        char_scores = [h.get("score") for h in horses]
        char_p = [h.get("p") for h in horses]
        base_ranks = [h.get("base_rank") for h in horses]

    objective = char_config.get("objective", DEFAULT_OBJECTIVE)
    q = [h.get("q") for h in horses]
    odds = [h.get("odds") for h in horses]
    values = objective_values(objective, char_p, q, odds)
    if not any(v is not None for v in values) and objective != DEFAULT_OBJECTIVE:
        # 単勝オッズが1頭も取れていないとEVが全滅する。無言で全馬を選定対象外にせず、
        # 同じキャラ固有pのまま既定のp-q尺度へ落とす。
        logger.warning("目的関数 %s の値が1頭も計算できないため %s に切り替えます",
                       objective, DEFAULT_OBJECTIVE)
        values = objective_values(DEFAULT_OBJECTIVE, char_p, q, odds)

    value_ranks = _ranks_desc(values)
    for horse, base_rank, score, p_i, value, value_rank in zip(
            horses, base_ranks, char_scores, char_p, values, value_ranks):
        horse["sel_base_rank"] = base_rank
        horse["sel_score"] = score
        horse["sel_p"] = p_i
        horse["sel_value"] = value
        horse["sel_value_rank"] = value_rank


# ------------------------------------------- Harville近似（§5.3）

def _sequential(p_by_num: dict[int, float], order: tuple[int, ...]) -> float:
    """Harville の逐次確率。order の並びちょうどで決まる確率を返す。"""
    remaining = 1.0
    prob = 1.0
    for num in order:
        if remaining <= 0:
            return 0.0
        prob *= p_by_num[num] / remaining
        remaining -= p_by_num[num]
    return prob


def umaren_prob(p_by_num: dict[int, float], i: int, j: int) -> float:
    """馬連（1・2着を順不同で的中）の確率。"""
    return sum(_sequential(p_by_num, order) for order in ((i, j), (j, i)))


def trio_prob(p_by_num: dict[int, float], i: int, j: int, k: int) -> float:
    """3連複（1〜3着を順不同で的中）の確率。全6順列の和。"""
    return sum(_sequential(p_by_num, order) for order in itertools.permutations((i, j, k)))


def wide_prob(p_by_num: dict[int, float], i: int, j: int) -> float:
    """ワイド（2頭がともに3着以内）の確率。3着に入る第三の馬で場合分けして合計する。"""
    others = [n for n in p_by_num if n not in (i, j)]
    return sum(trio_prob(p_by_num, i, j, k) for k in others)


def bet_probability(bet: dict[str, Any], p_by_num: dict[int, float]) -> float:
    """1点の的中確率（Harville近似）。"""
    horses = bet["horses"]
    if not all(num in p_by_num for num in horses):
        return 0.0
    if bet["type"] == UMAREN:
        return umaren_prob(p_by_num, horses[0], horses[1])
    if bet["type"] == WIDE:
        return wide_prob(p_by_num, horses[0], horses[1])
    if bet["type"] == SANRENPUKU:
        return trio_prob(p_by_num, horses[0], horses[1], horses[2])
    raise ValueError(f"未対応の券種です: {bet['type']}")


def _bet_hits(bet: dict[str, Any], top3: tuple[int, ...]) -> bool:
    """着順（1〜3着の馬番タプル）に対して、その1点が的中しているか。"""
    horses = set(bet["horses"])
    if bet["type"] == UMAREN:
        return horses == set(top3[:2])
    if bet["type"] == WIDE:
        return horses <= set(top3)
    if bet["type"] == SANRENPUKU:
        return horses == set(top3)
    return False


def evaluate_card(bets: list[dict[str, Any]], p_by_num: dict[int, float],
                  takeout: float) -> dict[str, Any]:
    """
    カード全体の的中確率と払戻レンジを、上位3着の順列を全列挙して集計する（§5.3）。

    戻り値: {"hit_pct": int, "payout_range": [下限, 上限]}
    払戻は 1点あたり amt × (1 − takeout) / 的中確率（＝控除率で割り引いたフェア配当）。
    的中する組み合わせが1つも無ければ payout_range は [0, 0]。
    """
    fair_payout: list[float] = []
    for bet in bets:
        prob = bet_probability(bet, p_by_num)
        fair_payout.append(bet["amt"] * (1.0 - takeout) / prob if prob > 0 else 0.0)

    hit_prob = 0.0
    payouts: list[float] = []
    for top3 in itertools.permutations(p_by_num.keys(), 3):
        outcome_prob = _sequential(p_by_num, top3)
        if outcome_prob <= 0:
            continue
        payout = sum(
            fair_payout[i] for i, bet in enumerate(bets) if _bet_hits(bet, top3)
        )
        if payout > 0:
            hit_prob += outcome_prob
            payouts.append(payout)

    if not payouts:
        return {"hit_pct": 0, "payout_range": [0, 0]}

    return {
        "hit_pct": round(hit_prob * 100),
        "payout_range": [_round100(min(payouts)), _round100(max(payouts))],
    }


PAYOUT_ROUND_UNIT = 100


def _combo_lookup_key(horses: list[int]) -> str:
    """race.combo_odds と同じ、馬番昇順の "2-9[-10]" キー。"""
    return "-".join(str(n) for n in sorted(horses))


def market_odds_for_bet(bet: dict[str, Any], combo_odds: dict[str, Any] | None) -> float | None:
    """
    1点の実市場オッズ倍率を返す。

    ワイドは発売中に下限〜上限の幅で提示されるため、鳳のEV判定では**下限**を使う。
    これは高EVを誇張しないための保守的な選択。
    """
    if not combo_odds:
        return None
    table = combo_odds.get(bet["type"])
    if not isinstance(table, dict):
        return None
    raw = table.get(_combo_lookup_key(bet["horses"]))
    if isinstance(raw, (int, float)):
        return float(raw) if raw > 0 else None
    if isinstance(raw, (list, tuple)) and raw:
        vals = [float(v) for v in raw if isinstance(v, (int, float)) and v > 0]
        return min(vals) if vals else None
    return None


def evaluate_market_card(bets: list[dict[str, Any]], p_by_num: dict[int, float],
                         combo_odds: dict[str, Any] | None) -> dict[str, Any]:
    """
    カードを**実際の馬券オッズ**で評価する。

    expected_payout = Σ(的中確率 × 市場オッズ倍率 × 購入額)
    expected_roi    = expected_payout / 総購入額
    edge            = expected_roi - 1

    1点でも市場オッズが欠けたカードを「高EV」と判定すると、都合の良い点だけを
    足し上げることになるため、complete=false のカードは鳳降臨判定には使わない。
    """
    spent = sum(b["amt"] for b in bets)
    if spent <= 0:
        return {"complete": False, "coverage": 0.0, "expected_payout": None,
                "expected_roi": None, "edge": None, "bets": []}

    rows = []
    expected_payout = 0.0
    known = 0
    for bet in bets:
        prob = bet_probability(bet, p_by_num)
        odds = market_odds_for_bet(bet, combo_odds)
        expected = None
        if odds is not None:
            known += 1
            expected = prob * odds * bet["amt"]
            expected_payout += expected
        rows.append({
            "type": bet["type"],
            "horses": list(bet["horses"]),
            "amt": bet["amt"],
            "prob": round(prob, 8),
            "market_odds": odds,
            "expected_payout": round(expected, 2) if expected is not None else None,
        })

    coverage = known / len(bets) if bets else 0.0
    complete = known == len(bets) and bool(bets)
    if not complete:
        return {
            "complete": False,
            "coverage": round(coverage, 4),
            "expected_payout": None,
            "expected_roi": None,
            "edge": None,
            "bets": rows,
        }

    roi = expected_payout / spent
    return {
        "complete": True,
        "coverage": 1.0,
        "expected_payout": round(expected_payout, 2),
        "expected_roi": round(roi, 6),
        "edge": round(roi - 1.0, 6),
        "bets": rows,
    }


def _round100(value: float) -> int:
    """
    払戻の概算値を丸める（あくまで目安の表示なので精度を主張しない）。

    丸めの単位は100円。JRAの購入単位と表示単位を一致させる。
    正の概算払戻は最低でも1単位（100円）を残す。
    """
    if value <= 0:
        return 0
    return max(PAYOUT_ROUND_UNIT,
               int(round(value / PAYOUT_ROUND_UNIT) * PAYOUT_ROUND_UNIT))


# ------------------------------------------- キャラ別カード生成（§5）

def _rank_norm(rank: int | None, total: int) -> float | None:
    """順位を 1位=1.0 … 最下位=0.0 に線形正規化する（§5.1）。"""
    if rank is None or total <= 0:
        return None
    if total == 1:
        return 1.0
    return (total - rank) / (total - 1)


def select_horses(horses: list[dict[str, Any]], lam: float,
                  axis_base_rank_floor: int | None = None,
                  base_rank_key: str = "base_rank",
                  value_rank_key: str = "myomi_rank") -> list[dict[str, Any]]:
    """
    sel = (1−λ)×base_rank_norm + λ×妙味rank_norm の降順に馬を並べる（§5.1）。

    どの順位を使うかは *_rank_key で差し替えられる（キャラ固有の実力順・歪み順を渡すため。
    `assign_character_ranks` 参照）。既定は全キャラ共通の base_rank / myomi_rank。

    axis_base_rank_floor が指定されている場合、**軸（先頭）だけ**は base_rank が
    その値以下の馬に限る（§5.2 源さんの線引き）。ここは**共通の base_rank で見る**：
    キャラ固有の重みで下駄を履かせた順位で線引きしたら、安全弁の意味が無くなるため。
    条件を満たす馬が無ければ制約を諦める。
    """
    candidates = [
        h for h in horses
        if h.get(base_rank_key) is not None and h.get(value_rank_key) is not None
    ]
    if not candidates:
        return []

    base_total = max(h[base_rank_key] for h in candidates)
    myomi_total = max(h[value_rank_key] for h in candidates)

    for horse in candidates:
        base_norm = _rank_norm(horse[base_rank_key], base_total) or 0.0
        myomi_norm = _rank_norm(horse[value_rank_key], myomi_total) or 0.0
        horse["sel"] = (1.0 - lam) * base_norm + lam * myomi_norm

    ordered = sorted(candidates, key=lambda h: h["sel"], reverse=True)

    if axis_base_rank_floor is not None and ordered:
        eligible = next(
            (h for h in ordered
             if h.get("base_rank") is not None and h["base_rank"] <= axis_base_rank_floor),
            None,
        )
        if eligible is not None and eligible is not ordered[0]:
            ordered.remove(eligible)
            ordered.insert(0, eligible)
        elif eligible is None:
            logger.warning(
                "axis_base_rank_floor=%s を満たす馬がいないため、軸の制約を適用できません",
                axis_base_rank_floor,
            )

    return ordered


def generate_card_for_character(char_id: str, horses: list[dict[str, Any]],
                                config: dict[str, Any], temperature: float = 10.0,
                                combo_odds: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """
    1キャラ分のcards[]要素を生成する（買い目生成仕様§5）。

    horses: base_rank / myomi_rank / num / p が付いた出走馬リスト
    戻り値: {"char", "hit_pct", "payout_range", "total", "bets"}。
            馬が足りずテンプレを適用できない場合は None（カードを作らない）
    生成後に §5.5 の不変条件を検証し、崩れていれば ValueError を投げる。
    """
    char_config = config["characters"][char_id]
    assign_character_ranks(horses, config, char_config, temperature)
    assign_place_partner_ranks(horses)
    ordered = select_horses(
        horses, char_config["lambda"], char_config.get("axis_base_rank_floor"),
        base_rank_key="sel_base_rank", value_rank_key="sel_value_rank",
    )

    template = next(
        (t for t in CARD_TEMPLATES[char_id] if len(ordered) >= t["min_horses"]), None
    )
    if template is None:
        logger.warning("出走可能な馬が %d 頭しかなく、%s のカードを生成できません", len(ordered), char_id)
        return None

    place_mode = char_config.get("place_partner_mode")
    place_pool = order_place_partners(
        horses, place_mode, config, axis_num=ordered[0]["num"]
    ) if place_mode else []

    bets: list[dict[str, Any]] = [
        {
            "type": bet_type,
            "horses": _bet_horses_with_place_policy(
                bet_type, indices, ordered, place_pool
            ),
            "amt": amt,
        }
        for bet_type, indices, amt in template["bets"]
    ]

    total = char_config["total"]
    if sum(b["amt"] for b in bets) != total:
        raise ValueError(
            f"{char_id} の券種テンプレの合計が total と一致しません: "
            f"{sum(b['amt'] for b in bets)} != {total}"
        )
    if len(bets) > char_config["max_points"]:
        raise ValueError(f"{char_id} の点数がmax_pointsを超えています: {len(bets)} > {char_config['max_points']}")

    p_by_num = {
        h["num"]: h["sel_p"] for h in horses
        if h.get("sel_p") is not None and h.get("num") is not None
    }
    evaluation = evaluate_card(bets, p_by_num, config["combo_prob"]["takeout"])
    market_ev = evaluate_market_card(bets, p_by_num, combo_odds)

    return {
        "char": char_id,
        # どの尺度で歪みを測って買ったか。あとで「どの見方が効いたか」を集計するために残す
        "objective": char_config.get("objective", DEFAULT_OBJECTIVE),
        "place_partner_mode": place_mode,
        "model_version": "top3-partner-v1",
        "hit_pct": evaluation["hit_pct"],
        "payout_range": evaluation["payout_range"],
        "market_ev": market_ev,
        "probability_model": {
            "temperature": temperature,
            "score_weights": char_config.get("score_weights", config["score_weights"]),
        },
        "total": total,
        "bets": bets,
    }


def validate_card_invariants(card: dict[str, Any], marks: list[dict[str, Any]],
                             amt_unit: int = 100) -> None:
    """買い目生成仕様§5.5の5条件を検証。崩れていたら ValueError を投げる。"""
    mark_nums = {m["num"] for m in marks}

    for bet in card["bets"]:
        # 1. 金額の単位
        if bet["amt"] % amt_unit != 0:
            raise ValueError(f"amtが{amt_unit}円単位ではありません: {bet}")
        # 3. 券種
        if bet["type"] not in BET_TYPES:
            raise ValueError(f"未対応の券種です: {bet['type']}")
        # 4. 頭数と券種の一致
        if len(bet["horses"]) != BET_SIZE[bet["type"]]:
            raise ValueError(f"券種と頭数が一致しません: {bet}")
        if len(set(bet["horses"])) != len(bet["horses"]):
            raise ValueError(f"同じ馬番が重複しています: {bet}")
        # 5. marksに存在する馬か
        for num in bet["horses"]:
            if num not in mark_nums:
                raise ValueError(f"marksに存在しない馬番が買い目に含まれています: {num}")

    # 2. 合計の一致
    spent = sum(bet["amt"] for bet in card["bets"])
    if spent != card["total"]:
        raise ValueError(f"Σamt が total と一致しません: {spent} != {card['total']}")
