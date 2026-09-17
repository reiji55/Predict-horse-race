"""
results.json ビルドスクリプト（データスキーマ仕様_v1.2.md §5）

流れ（引き継ぎ書v3 §4.1）：
  Fページ（scraper.fetchers.f_results）で確定着順・公式配当表を取得
  → dividends（買い目非依存の生データ）を保持
  → predictions.json の cards[] と突き合わせて的中判定・payout計算
  → 払戻の4不変条件を検証（データスキーマ仕様§5「払戻の計算ルール」）
  → results.json 書き出し

--- 払戻の計算ルール（§5の4不変条件） ---

  1. bets[].payout = 該当する dividends の pay × amt ÷ 100（外れは 0）
  2. cards[].payout = そのカードの全 bets[].payout の合計
  3. cards[].spent  = 全 bets[].amt の合計 ＝ predictions側 total と一致
  4. cards[].hit    = 1点でも bets[].hit=true があれば true

この4条件を検証し、崩れていたらエラーにする（**スクレイプミス・配当の取り違えの早期検知**）。

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

    return {
        "race_id": prediction_race["id"],
        "finish": race_result.get("finish", []),
        "dividends": dividends,
        "cards": cards,
    }


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

    for race in results.get("results", []):
        overall["races"] += 1
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
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="週末3CARDS results.json ビルド")
    parser.add_argument("--results", required=True,
                        help='Fページ取得結果のJSON（{race_id: {finish, dividends}}）')
    parser.add_argument("--predictions", default=str(PREDICTIONS_PATH))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    with open(args.predictions, encoding="utf-8") as f:
        predictions = json.load(f)
    with open(args.results, encoding="utf-8") as f:
        race_results = json.load(f)

    results = build_results(predictions, race_results)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    summary = summarize(results)["overall"]
    logger.info("書き出し完了: %s（%d レース／購入 %d円・払戻 %d円・収支 %+d円）",
                OUTPUT_PATH, summary["races"], summary["spent"], summary["payout"],
                summary["balance"])


if __name__ == "__main__":
    main()
