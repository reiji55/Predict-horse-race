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
源さんだけは `axis_base_rank_floor`（=6）があり、**base_score が下位すぎる馬は軸にしない**
（妙味に振っても実力の裏づけは残す＝ただのギャンブルにしない・§5.2）。
鳳のカードは `legendary=true` のレースのみ生成する（§5.4）。

--- 券種確率と払戻の近似（§5.3） ---

式別オッズを取得していないので、単勝由来の p から **Harville 式**で近似する。

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

  ⚠ **amt_unit（要レビュー）**：仕様§5.5-1 は「100円単位」だが、サンプル（predictions/results とも）
  の哲さんカードには `amt: 150` がある。500円を厚み付きで配分する自由度を優先し、
  **既定を50円単位**（`config/cards.json` の `amt_unit`）とした。100に戻せば仕様の記述どおりになる。
  **この判断は docs/OPEN_QUESTIONS.md B-4 に記録。**
"""
from __future__ import annotations

import itertools
import logging
from typing import Any

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
        {"min_horses": 4, "bets": [(UMAREN, (0, 1), 150), (UMAREN, (0, 2), 100),
                                   (SANRENPUKU, (0, 1, 2), 150), (WIDE, (0, 3), 100)]},
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
    # 鳳：全券種ミックス・点数増（降臨時のみ・total=1000）
    "otori": [
        {"min_horses": 4, "bets": [(WIDE, (0, 1), 200), (WIDE, (0, 2), 100),
                                   (UMAREN, (0, 1), 100), (UMAREN, (0, 2), 100),
                                   (SANRENPUKU, (0, 1, 2), 200), (SANRENPUKU, (0, 1, 3), 100),
                                   (SANRENPUKU, (0, 2, 3), 200)]},
        {"min_horses": 3, "bets": [(WIDE, (0, 1), 300), (UMAREN, (0, 1), 200),
                                   (SANRENPUKU, (0, 1, 2), 500)]},
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
    """
    values = [
        p_i - q_i if (p_i is not None and q_i is not None) else None
        for p_i, q_i in zip(p, q)
    ]

    order = sorted(
        (i for i, v in enumerate(values) if v is not None),
        key=lambda i: values[i],
        reverse=True,
    )
    ranks: dict[int, int] = {index: rank for rank, index in enumerate(order, start=1)}

    return [
        {"value": values[i], "myomi_rank": ranks.get(i)}
        for i in range(len(values))
    ]


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


def _round100(value: float) -> int:
    """払戻の概算値は100円単位に丸める（あくまで目安の表示なので精度を主張しない）。"""
    return int(round(value / 100.0) * 100)


# ------------------------------------------- キャラ別カード生成（§5）

def _rank_norm(rank: int | None, total: int) -> float | None:
    """順位を 1位=1.0 … 最下位=0.0 に線形正規化する（§5.1）。"""
    if rank is None or total <= 0:
        return None
    if total == 1:
        return 1.0
    return (total - rank) / (total - 1)


def select_horses(horses: list[dict[str, Any]], lam: float,
                  axis_base_rank_floor: int | None = None) -> list[dict[str, Any]]:
    """
    sel = (1−λ)×base_rank_norm + λ×myomi_rank_norm の降順に馬を並べる（§5.1）。

    axis_base_rank_floor が指定されている場合、**軸（先頭）だけ**は base_rank が
    その値以下の馬に限る（§5.2 源さんの線引き）。条件を満たす馬が無ければ制約を諦める。
    """
    candidates = [
        h for h in horses
        if h.get("base_rank") is not None and h.get("myomi_rank") is not None
    ]
    if not candidates:
        return []

    base_total = max(h["base_rank"] for h in candidates)
    myomi_total = max(h["myomi_rank"] for h in candidates)

    for horse in candidates:
        base_norm = _rank_norm(horse["base_rank"], base_total) or 0.0
        myomi_norm = _rank_norm(horse["myomi_rank"], myomi_total) or 0.0
        horse["sel"] = (1.0 - lam) * base_norm + lam * myomi_norm

    ordered = sorted(candidates, key=lambda h: h["sel"], reverse=True)

    if axis_base_rank_floor is not None and ordered:
        eligible = next(
            (h for h in ordered if h["base_rank"] <= axis_base_rank_floor), None
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
                                config: dict[str, Any]) -> dict[str, Any] | None:
    """
    1キャラ分のcards[]要素を生成する（買い目生成仕様§5）。

    horses: base_rank / myomi_rank / num / p が付いた出走馬リスト
    戻り値: {"char", "hit_pct", "payout_range", "total", "bets"}。
            馬が足りずテンプレを適用できない場合は None（カードを作らない）
    生成後に §5.5 の不変条件を検証し、崩れていれば ValueError を投げる。
    """
    char_config = config["characters"][char_id]
    ordered = select_horses(horses, char_config["lambda"], char_config.get("axis_base_rank_floor"))

    template = next(
        (t for t in CARD_TEMPLATES[char_id] if len(ordered) >= t["min_horses"]), None
    )
    if template is None:
        logger.warning("出走可能な馬が %d 頭しかなく、%s のカードを生成できません", len(ordered), char_id)
        return None

    bets: list[dict[str, Any]] = [
        {"type": bet_type, "horses": [ordered[i]["num"] for i in indices], "amt": amt}
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
        h["num"]: h["p"] for h in horses
        if h.get("p") is not None and h.get("num") is not None
    }
    evaluation = evaluate_card(bets, p_by_num, config["combo_prob"]["takeout"])

    return {
        "char": char_id,
        "hit_pct": evaluation["hit_pct"],
        "payout_range": evaluation["payout_range"],
        "total": total,
        "bets": bets,
    }


def validate_card_invariants(card: dict[str, Any], marks: list[dict[str, Any]],
                             amt_unit: int = 50) -> None:
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
