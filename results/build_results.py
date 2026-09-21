"""
results.json ビルドスクリプト（データスキーマ仕様_v1.2.md §5）

流れ（引き継ぎ書v3 §4.1）：
  Fページ（scraper.fetchers.f_results）で確定着順・公式配当表を取得
  → dividends（買い目非依存の生データ）を保持
  → **発走前に凍結した予想**（data/snapshots/・logic/snapshots.py）の cards[] と
     突き合わせて的中判定・payout計算
  → 払戻の4不変条件を検証（データスキーマ仕様§5「払戻の計算ルール」）
  → results.json 書き出し

--- 払戻の計算ルール（§5の4不変条件） ---

  1. bets[].payout = 該当する dividends の pay × amt ÷ 100（外れは 0）
  2. cards[].payout = そのカードの全 bets[].payout の合計
  3. cards[].spent  = 全 bets[].amt の合計 ＝ predictions側 total と一致
  4. cards[].hit    = 1点でも bets[].hit=true があれば true

この4条件を検証し、崩れていたらエラーにする（**スクレイプミス・配当の取り違えの早期検知**）。

--- 何を採点するか（発走前の凍結） ---

採点対象は `data/predictions.json`（毎回上書きされる最新版）**ではなく**、
`data/snapshots/{race_id}.json`（発走前に観測した最後の予想）。理由は logic/snapshots.py 冒頭。

--- 市場ベンチマーク ---

モデルの成績は「買わなかった場合」ではなく「**何も考えずに人気どおり買った場合**」と
比べないと意味がない。そこで各レースで「1番人気−2番人気のワイド1点」を同額買った場合の
収支を併記する（`benchmark`）。**モデルがこれに勝てないなら、モデルは価値を生んでいない。**

--- 的中判定について ---

`dividends` には**的中した組み合わせだけ**が載る（公式配当表そのもの）。
したがって「買い目が dividends に載っていない ＝ 外れ」で判定できる。着順との突き合わせを
自前でやらないので、ワイドの3通り・同着の複数配当もそのまま正しく扱える。

--- ダッシュボード用の集計 ---

データスキーマ仕様§7 が「results.json の券種別集計フィールド詳細」をタスク7の宿題として
残しているため、**JSONの契約（§5）は変えず**に `summarize()` を別関数として用意した。
集計トップは「全予想師のカードを毎回すべて買った場合の累計収支」に一本化する方針
（引き継ぎ書v1 §2・v3 §4.1）。マイナス収支も隠さない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
from pathlib import Path
from typing import Any

from logic import snapshots

logger = logging.getLogger("results.build_results")

ROOT = Path(__file__).resolve().parent.parent
PREDICTIONS_PATH = ROOT / "data" / "predictions.json"
OUTPUT_PATH = ROOT / "data" / "results.json"

JST = datetime.timezone(datetime.timedelta(hours=9))


def find_dividend(dividends: dict[str, Any], bet: dict[str, Any]) -> int | None:
    """
    公式配当表から、その買い目に対応する配当（100円あたり）を探す。
    的中していなければ None。組み合わせは順不同で比較する。
    """
    horses = set(bet["horses"])
    for payout in dividends.get(bet["type"], []):
        if set(payout["horses"]) == horses:
            return payout["pay"]
    return None


def settle_bet(bet: dict[str, Any], dividends: dict[str, Any]) -> dict[str, Any]:
    """1点の的中判定と払戻（不変条件1）。"""
    pay = find_dividend(dividends, bet)
    return {
        "type": bet["type"],
        "horses": list(bet["horses"]),
        "amt": bet["amt"],
        "hit": pay is not None,
        "payout": int(pay * bet["amt"] / 100) if pay is not None else 0,
    }


def settle_card(card: dict[str, Any], dividends: dict[str, Any]) -> dict[str, Any]:
    """1カード分の的中判定・払戻（不変条件2〜4）。"""
    bets = [settle_bet(bet, dividends) for bet in card["bets"]]
    return {
        "char": card["char"],
        "hit": any(bet["hit"] for bet in bets),
        "spent": sum(bet["amt"] for bet in bets),
        "payout": sum(bet["payout"] for bet in bets),
        "bets": bets,
    }


def validate_result_invariants(result_card: dict[str, Any], prediction_card: dict[str, Any],
                               dividends: dict[str, Any]) -> None:
    """データスキーマ仕様§5「払戻の計算ルール」の4不変条件を検証する。崩れたら ValueError。"""
    for bet in result_card["bets"]:
        pay = find_dividend(dividends, bet)
        expected = int(pay * bet["amt"] / 100) if pay is not None else 0
        if bet["payout"] != expected:                                    # 1
            raise ValueError(f"払戻額が配当表と一致しません: {bet} / 期待値 {expected}")
        if bet["hit"] != (pay is not None):
            raise ValueError(f"的中フラグが配当表と一致しません: {bet}")

    if result_card["payout"] != sum(b["payout"] for b in result_card["bets"]):   # 2
        raise ValueError(f"cards[].payout が買い目の合計と一致しません: {result_card['char']}")

    if result_card["spent"] != sum(b["amt"] for b in result_card["bets"]):        # 3
        raise ValueError(f"cards[].spent が買い目の合計と一致しません: {result_card['char']}")
    if result_card["spent"] != prediction_card["total"]:
        raise ValueError(
            f"spent が predictions 側の total と一致しません: "
            f"{result_card['spent']} != {prediction_card['total']}（{result_card['char']}）"
        )

    if result_card["hit"] != any(b["hit"] for b in result_card["bets"]):          # 4
        raise ValueError(f"cards[].hit が買い目の的中と一致しません: {result_card['char']}")


BENCHMARK_AMT = 500          # 1カード分と同額。キャラ1人と正面から比べられるようにする
BENCHMARK_TYPE = "ワイド"


def market_benchmark(prediction_race: dict[str, Any], dividends: dict[str, Any],
                     amt: int = BENCHMARK_AMT) -> dict[str, Any] | None:
    """
    「1番人気−2番人気のワイドを1点だけ買う」という**思考ゼロの基準戦略**の収支。

    モデルが市場の歪みを突けているかは、この基準を上回れるかで判る。
    人気順は marks[].odds（単勝オッズ）の昇順で決める。オッズが2頭ぶん揃わなければ None。
    """
    ranked = sorted(
        (m for m in prediction_race.get("marks", [])
         if m.get("odds") is not None and m.get("num") is not None),
        key=lambda m: m["odds"],
    )
    if len(ranked) < 2:
        return None

    bet = {"type": BENCHMARK_TYPE, "horses": [ranked[0]["num"], ranked[1]["num"]], "amt": amt}
    settled = settle_bet(bet, dividends)
    settled["odds"] = [ranked[0]["odds"], ranked[1]["odds"]]
    return settled


def build_race_result(prediction_race: dict[str, Any], race_result: dict[str, Any]) -> dict[str, Any]:
    """
    predictions.json の races[] 1件と、Fページ由来の {finish, dividends} から
    results.json の results[] 1件を組み立てる。
    """
    dividends = race_result.get("dividends", {})
    cards = []
    for prediction_card in prediction_race.get("cards", []):
        card = settle_card(prediction_card, dividends)
        validate_result_invariants(card, prediction_card, dividends)
        cards.append(card)

    result = {
        "race_id": prediction_race["id"],
        "finish": race_result.get("finish", []),
        "dividends": dividends,
        "cards": cards,
        "meta": {
            "day": prediction_race.get("day"),
            "venue": prediction_race.get("venue"),
            "race_no": prediction_race.get("race_no"),
            "name": prediction_race.get("name"),
            "grade": prediction_race.get("grade"),
            "marks": prediction_race.get("marks", []),
        },
    }
    if prediction_race.get("evaluation_scope"):
        result["evaluation_scope"] = prediction_race["evaluation_scope"]
    if prediction_race.get("record_note"):
        result["record_note"] = prediction_race["record_note"]

    benchmark = None
    if prediction_race.get("evaluation_scope") != "manual_chat":
        benchmark = market_benchmark(prediction_race, dividends)
    if benchmark is not None:
        result["benchmark"] = benchmark
    # いつのオッズで決めた買い目を採点したのか、結果側にも残す（logic/snapshots.py 参照）
    if prediction_race.get("frozen_at"):
        result["frozen_at"] = prediction_race["frozen_at"]
    return result


def build_results(predictions: dict[str, Any],
                  race_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """
    predictions.json と {race_id: {finish, dividends}} から results.json を組み立てる。

    race_results は `scraper.fetchers.f_results.fetch_results()` の戻り値を
    race_id をキーに集めたもの。結果がまだ出ていないレースは飛ばす。
    """
    results = []
    for race in predictions.get("races", []):
        race_result = race_results.get(race.get("id"))
        if race_result is None:
            logger.info("結果が未取得のためスキップします: %s", race.get("id"))
            continue
        results.append(build_race_result(race, race_result))

    return {
        "updated_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
        "results": results,
    }


def summarize(results: dict[str, Any]) -> dict[str, Any]:
    """
    ダッシュボード用の集計（データスキーマ仕様§7 の宿題。JSONの契約は変えない）。

    集計トップは「全予想師のカードを毎回すべて買った場合の累計収支」に一本化する。
    合算の的中率のような曖昧な数値は出さず、**マイナス収支も隠さない**（引き継ぎ書v1 §2）。
    """
    overall = {"races": 0, "spent": 0, "payout": 0}
    by_char: dict[str, dict[str, Any]] = {}
    by_type: dict[str, dict[str, Any]] = {}
    market = {"races": 0, "hits": 0, "spent": 0, "payout": 0}

    for race in results.get("results", []):
        if race.get("evaluation_scope") == "manual_chat":
            continue

        overall["races"] += 1

        benchmark = race.get("benchmark")
        if benchmark:
            market["races"] += 1
            market["hits"] += 1 if benchmark["hit"] else 0
            market["spent"] += benchmark["amt"]
            market["payout"] += benchmark["payout"]

        for card in race.get("cards", []):
            overall["spent"] += card["spent"]
            overall["payout"] += card["payout"]

            char = by_char.setdefault(card["char"], {"cards": 0, "hits": 0, "spent": 0, "payout": 0})
            char["cards"] += 1
            char["hits"] += 1 if card["hit"] else 0
            char["spent"] += card["spent"]
            char["payout"] += card["payout"]

            for bet in card["bets"]:
                stats = by_type.setdefault(bet["type"], {"bets": 0, "hits": 0, "spent": 0, "payout": 0})
                stats["bets"] += 1
                stats["hits"] += 1 if bet["hit"] else 0
                stats["spent"] += bet["amt"]
                stats["payout"] += bet["payout"]

    def _finish(stats: dict[str, Any], count_key: str, hit_key: str = "hits") -> dict[str, Any]:
        stats["balance"] = stats["payout"] - stats["spent"]
        stats["roi"] = round(stats["payout"] / stats["spent"], 4) if stats["spent"] else None
        if count_key in stats and stats[count_key]:
            stats["hit_rate"] = round(stats[hit_key] / stats[count_key], 4)
        return stats

    overall["balance"] = overall["payout"] - overall["spent"]
    overall["roi"] = round(overall["payout"] / overall["spent"], 4) if overall["spent"] else None

    return {
        "overall": overall,
        "by_char": {k: _finish(v, "cards") for k, v in by_char.items()},
        "by_type": {k: _finish(v, "bets") for k, v in by_type.items()},
        # 1番人気−2番人気ワイドを買い続けた場合。**モデルの比較対象はこれ**
        "market": _finish(market, "races"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="週末3CARDS results.json ビルド")
    parser.add_argument("--results", required=True,
                        help='Fページ取得結果のJSON（{race_id: {finish, dividends}}）')
    parser.add_argument(
        "--predictions",
        help="採点する予想のJSON。既定は data/snapshots/（発走前に凍結した予想）。"
             "**data/predictions.json を直接渡すと、発走後に作り直された予想を採点してしまう**",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.predictions:
        logger.warning("スナップショットではなく %s を採点します（発走後の再生成が混ざる恐れがあります）",
                       args.predictions)
        with open(args.predictions, encoding="utf-8") as f:
            predictions = json.load(f)
    else:
        predictions = snapshots.as_predictions()
        logger.info("発走前に凍結された予想 %d レースを採点します", len(predictions["races"]))

    with open(args.results, encoding="utf-8") as f:
        race_results = json.load(f)

    results = build_results(predictions, race_results)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    summary = summarize(results)
    overall, market = summary["overall"], summary["market"]
    logger.info("書き出し完了: %s（%d レース／購入 %d円・払戻 %d円・収支 %+d円）",
                OUTPUT_PATH, overall["races"], overall["spent"], overall["payout"],
                overall["balance"])
    logger.info("市場ベンチマーク（1-2番人気ワイド）: %d レース／収支 %+d円・回収率 %s",
                market["races"], market["balance"],
                f"{market['roi']:.0%}" if market["roi"] is not None else "—")


if __name__ == "__main__":
    main()
