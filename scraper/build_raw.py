"""
raw/{week_id}.json ビルドスクリプト（取得項目・共通内部フォーマット仕様_v1.1.md §2 が出力契約）

流れ：
  A（レース一覧）→ メインレース特定（§ 下記「メインレースの絞り込み」）
  → B（出馬表）で基本情報＋出走馬一覧
  → B2（オッズAPI）で単勝・複勝・人気を埋める
     ※ 出馬表HTMLのオッズ欄はJS描画のため空。内部AJAX API（b2_odds）を叩いて補完する
  → C（馬戦績）で各出走馬の past_runs を埋める（同一週内キャッシュ）
  → D（騎手LB）／E（厩舎LB）で jockey_stats／trainer_stats を埋める
  → 正規化（取得項目仕様§2.1）を通して raw/{week_id}.json に書き出す

GitHub Actions（workflow_dispatch）から
`python -m scraper.build_raw --week 2026-W27 --dates 2026-07-04 2026-07-05` のように呼ばれる想定。

--- メインレースの絞り込み（config/scraper.json の main_race） ---

取得項目仕様§1.1 の A行は「→ メインレースの特定」、§2.2 は races を「土日のメイン4〜6」と定め、
スピード指数仕様§0 も「現在の取得設計（メインレースのみ）」を前提にしている。
一方 `a_race_list.fetch_race_list()` は**その日の全レースを返す**（絞り込みはしない）。
つまり **絞り込みは A ではなくこのオーケストレーターの責務**。

  mode="race_no"（既定）… 各開催場の指定R（既定11R＝JRAのメインレース）だけを対象にする。
                          3場開催の土日なら 3×2=6レースとなり、§2.2の「4〜6」と一致する
  mode="grade"          … min_grade 以上のグレードのレースを対象にする
  mode="all"            … 絞り込まない（**要注意**：1日36レース×頭数ぶんの馬戦績ページを取りに行くので、
                          §1.1のページ数見積もり 週90〜130 を大きく超える。検証目的以外では使わない）

**「メイン」の定義そのものは運用者の確認待ち**（docs/OPEN_QUESTIONS.md B-2）。
既定を 11R にしてあるが、`config/scraper.json` を書き換えれば挙動を変えられる。

--- 週内キャッシュ（土日別実行への対応） ---

引き継ぎ書v6 §1 でGitHub Actionsは**土曜・日曜で別々に実行**すると決まったため、
`c_horse_history.fetch_horse_history_cached()` のプロセス内キャッシュだけでは
日曜に同じ馬を取り直してしまう（v5 §3-2 からの既知の課題）。

ここでは **既にコミットされている raw/{week_id}.json をキャッシュとして読み直す**ことで解決している。
土曜の実行結果には各馬の past_runs が入っているので、日曜はそれを再利用すればページを叩かずに済む。
新しいファイルを増やさず、ワークフローが既に raw/ をコミットしている前提にも乗れる。
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
from pathlib import Path
from typing import Any

from scraper.common import constants
from scraper.fetchers import (
    a_race_list,
    b_shutuba,
    b2_odds,
    c_horse_history,
    d_jockey_leading,
    e_trainer_leading,
)

logger = logging.getLogger("scraper.build_raw")

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "raw"
CONFIG_PATH = ROOT / "config" / "scraper.json"

JST = datetime.timezone(datetime.timedelta(hours=9))

# グレードの強さ順（取得項目仕様§2.1 のクラス表記）。mode="grade" の比較に使う
GRADE_ORDER = ["mi", "1win", "2win", "3win", "op", "g3", "g2", "g1"]


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------ メインレースの絞り込み

def select_main_races(entries: list[a_race_list.RaceListEntry],
                      config: dict[str, Any]) -> list[a_race_list.RaceListEntry]:
    """その日のレース一覧から、予想対象のレースだけを選ぶ（本モジュール冒頭の説明を参照）。"""
    main_config = config["main_race"]
    mode = main_config.get("mode", "race_no")

    if mode == "all":
        selected = list(entries)
    elif mode == "grade":
        threshold = GRADE_ORDER.index(main_config.get("min_grade", "g3"))
        selected = [
            e for e in entries
            if e.grade in GRADE_ORDER and GRADE_ORDER.index(e.grade) >= threshold
        ]
    elif mode == "race_no":
        target = main_config.get("race_no", 11)
        selected = [e for e in entries if e.race_no == target]
    else:
        raise ValueError(f"未対応の main_race.mode です: {mode}")

    # JRA中央10場のみ（race_id 生成が場名のromaji対応表に依存しているため）
    selected = [e for e in selected if e.venue in constants.JRA_VENUES]
    selected.sort(key=lambda e: (e.venue, e.race_no))

    max_per_day = main_config.get("max_per_day")
    if max_per_day is not None and len(selected) > max_per_day:
        logger.warning("対象レースが %d 件あるため、先頭 %d 件に絞ります", len(selected), max_per_day)
        selected = selected[:max_per_day]

    return selected


# ------------------------------------------------------------ 週内キャッシュ

def load_week_cache(week_id: str) -> dict[str, list[dict[str, Any]]]:
    """
    既存の raw/{week_id}.json から {horse_ref: past_runs} を復元する。
    土曜の実行結果を日曜の実行で再利用するための仕組み（本モジュール冒頭の説明を参照）。
    """
    path = OUTPUT_DIR / f"{week_id}.json"
    if not path.exists():
        return {}

    try:
        with path.open(encoding="utf-8") as f:
            existing = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("既存の %s を読めなかったため、キャッシュ無しで続行します", path)
        return {}

    cache: dict[str, list[dict[str, Any]]] = {}
    for race in existing.get("races", []):
        for entry in race.get("entries", []):
            horse_ref = (entry.get("horse_ref") or {}).get("netkeiba")
            past_runs = entry.get("past_runs")
            if horse_ref and past_runs:
                cache[horse_ref] = past_runs

    if cache:
        logger.info("週内キャッシュを %d 頭ぶん読み込みました（%s）", len(cache), path.name)
    return cache


def merge_races(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    既存の races[] に新しい races[] を統合する（同じ id は新しい方で置き換え）。
    土日別実行で日曜ぶんを追記していくために必要（上書きしてしまうと土曜ぶんが消える）。
    """
    by_id = {race.get("id"): race for race in existing}
    for race in new:
        by_id[race.get("id")] = race
    return sorted(by_id.values(), key=lambda r: (r.get("id") or ""))


# ------------------------------------------------------------ D・E（任意）

def fetch_leading_stats(venues: list[str], period: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    D（騎手リーディング）とE（調教師リーディング）を取得する。

    **場別リーディングは存在しない**ため、騎手も全国成績を1回取るだけでよい
    （OPEN_QUESTIONS B-3）。`venues` は呼び出し側の互換のために受け取るが絞り込みには使わない。

    取得に失敗しても例外を投げずに空マップを返す。人的スコア③は jockey_stats /
    trainer_stats が None でも `logic.human_score` 側で欠損として扱われ、合成スコアの重みが
    再正規化されるのでパイプライン全体は止まらない（取得項目仕様§2.0 原則2「取れなかったらnull」）。

    戻り値: ({venue: {jockey_ref: stats}}, {trainer_ref: stats})
            騎手側は場別に見えるが、全場に同じ全国成績のマップを入れている
            （attach_stats の呼び出し側を変えずに済ませるため）
    """
    jockey_stats: dict[str, Any] = {}
    trainer_stats: dict[str, Any] = {}

    try:
        overall_jockeys = d_jockey_leading.fetch_jockey_leading(period=period)
        jockey_stats = {venue: overall_jockeys for venue in venues}
    except (NotImplementedError, RuntimeError):
        logger.warning("騎手リーディングを取得できませんでした。jockey_stats は null のまま続行します",
                       exc_info=True)

    try:
        trainer_stats = e_trainer_leading.fetch_trainer_leading(period)
    except (NotImplementedError, RuntimeError):
        logger.warning("調教師リーディングを取得できませんでした。trainer_stats は null のまま続行します",
                       exc_info=True)

    return jockey_stats, trainer_stats


def attach_stats(race: dict[str, Any], jockey_stats_by_venue: dict[str, Any],
                 trainer_stats: dict[str, Any]) -> None:
    """出走馬の jockey_ref / trainer_ref を使って、リーディング表の着度数を振り分ける（§2.6）。"""
    venue_stats = jockey_stats_by_venue.get(race.get("venue"), {})
    for entry in race.get("entries", []):
        jockey_ref = ((entry.get("jockey") or {}).get("ref") or {}).get("netkeiba")
        trainer_ref = ((entry.get("trainer") or {}).get("ref") or {}).get("netkeiba")
        if jockey_ref and jockey_ref in venue_stats:
            entry["jockey_stats"] = venue_stats[jockey_ref]
        if trainer_ref and trainer_ref in trainer_stats:
            entry["trainer_stats"] = trainer_stats[trainer_ref]


# ------------------------------------------------------------ 1レース分の組み立て

def build_race(list_entry: a_race_list.RaceListEntry, cache: dict[str, list[dict[str, Any]]],
               n_runs: int) -> dict[str, Any]:
    """A の1件から、B → B2 → C を通して races[] 1件を組み立てる。"""
    race = b_shutuba.fetch_shutuba(list_entry.source_ref)

    # A で取れている情報で、B が取りこぼした欄を補う（B優先・Aは保険）
    race.setdefault("date", list_entry.date)
    for key, value in (("date", list_entry.date), ("day", list_entry.day),
                       ("venue", list_entry.venue), ("race_no", list_entry.race_no),
                       ("name", list_entry.name), ("post_time", list_entry.post_time)):
        if race.get(key) is None and value is not None:
            race[key] = value
    if race.get("grade") is None and list_entry.grade is not None:
        race["grade"] = list_entry.grade
    course = race.setdefault("course", {})
    for key, value in (("surface", list_entry.surface), ("dist", list_entry.dist),
                       ("heads", list_entry.heads), ("note", list_entry.note)):
        if course.get(key) is None and value is not None:
            course[key] = value

    # id は date / venue / race_no が揃って初めて作れる（取得項目仕様§2.1）
    if race.get("id") is None and race.get("date") and race.get("venue") and race.get("race_no"):
        race["id"] = constants.race_id(race["date"], race["venue"], race["race_no"])

    # B2：出馬表のオッズ欄はJS描画で空なので、AJAX APIで補完する
    try:
        b2_odds.merge_odds_into_race(race, b2_odds.fetch_odds(list_entry.source_ref))
    except RuntimeError:
        logger.warning("オッズを取得できませんでした（%s）。win_odds は null のまま続行します", race.get("id"))

    # C：各出走馬の past_runs（週内キャッシュ経由）
    for entry in race.get("entries", []):
        horse_ref = (entry.get("horse_ref") or {}).get("netkeiba")
        if not horse_ref:
            continue
        try:
            entry["past_runs"] = c_horse_history.fetch_horse_history_cached(
                horse_ref, cache, n_runs=n_runs
            )
        except RuntimeError:
            logger.warning("馬戦績を取得できませんでした（horse_ref=%s）。past_runs は空のまま続行します",
                           horse_ref)
            entry["past_runs"] = []

    return race


# ------------------------------------------------------------ 週単位の組み立て

def build_week(week_id: str, dates: list[str], config: dict[str, Any] | None = None) -> dict:
    """
    週単位で raw データを組み立てる。

    week_id: "2026-W27" のようなISO週識別子
    dates: その週の対象日（例 ["2026-07-04", "2026-07-05"] 土日）
           土日を別々のActions実行で回す場合は、1回につき1日だけ渡す
    戻り値: 取得項目仕様§2.2 のトップレベル構造（既存ファイルがあればマージ済み）
    """
    config = config if config is not None else load_config()
    cache = load_week_cache(week_id)          # 土曜ぶんの past_runs を再利用する
    n_runs = config.get("past_runs", 5)

    races: list[dict[str, Any]] = []
    for date_str in dates:
        try:
            list_entries = a_race_list.fetch_race_list(date_str)
        except RuntimeError:
            logger.exception("レース一覧を取得できませんでした: %s", date_str)
            continue

        targets = select_main_races(list_entries, config)
        logger.info("%s: %d レース中 %d レースを対象にします（%s）", date_str, len(list_entries),
                    len(targets), ", ".join(f"{e.venue}{e.race_no}R" for e in targets))

        venues = sorted({e.venue for e in targets})
        jockey_stats, trainer_stats = fetch_leading_stats(venues, config.get("leading_period", "2026"))

        for list_entry in targets:
            try:
                race = build_race(list_entry, cache, n_runs)
            except RuntimeError:
                # 1レースの失敗で週全体を落とさない（取得項目仕様§1.1 マナー設計）
                logger.exception("レースの取得に失敗しました: %s%dR",
                                 list_entry.venue, list_entry.race_no)
                continue
            attach_stats(race, jockey_stats, trainer_stats)
            races.append(race)

    existing_races: list[dict[str, Any]] = []
    existing_path = OUTPUT_DIR / f"{week_id}.json"
    if existing_path.exists():
        try:
            with existing_path.open(encoding="utf-8") as f:
                existing_races = json.load(f).get("races", [])
        except (OSError, json.JSONDecodeError):
            logger.warning("既存の %s を読めなかったため、新規作成として扱います", existing_path.name)

    return {
        "fetched_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
        "week_id": week_id,
        "races": merge_races(existing_races, races),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="週末3CARDS raw/{week_id}.json ビルド")
    parser.add_argument("--week", required=True, help='例: "2026-W27"')
    parser.add_argument("--dates", required=True, nargs="+", help='例: 2026-07-04 2026-07-05')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    data = build_week(args.week, args.dates)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{args.week}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("書き出し完了: %s（%d レース）", out_path, len(data["races"]))


if __name__ == "__main__":
    main()
