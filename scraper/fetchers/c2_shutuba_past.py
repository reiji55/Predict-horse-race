"""
C2ページ：出馬表「過去5走」（race.netkeiba.com/race/shutuba_past.html）

**Cページ（db.netkeiba.com/horse/）の代替経路。本番ではこちらを使う。**

出典：取得項目_共通内部フォーマット仕様_v1.1.md §2.5（past_runs[]の型）／OPEN_QUESTIONS C-9
取得タイミング：当日朝 / **1週あたり4〜6ページ**（レース数ぶんだけ）

--- なぜ乗り換えたか（2026-09-19 の初回本番実行） ---

`db.netkeiba.com/horse/{id}` は **GitHub Actions から叩くとbot判定され、中身の違うページが返る**
（HTTPは200。同じ馬の同じURLをブラウザで保存したものは `c_horse_history` のパーサーで
5走とも完全に取れたので、パーサーではなくアクセス側の問題）。UAを偽装して回避する道は
規約に反するうえブラックリストが解除されないため取らない。

代わりに netkeiba 自身の出馬表サブメニューにある「過去5走」ページを使う。利点が3つある：

  1. `race.netkeiba.com` は A（レース一覧）・B（出馬表）で現に通っている
  2. **1レース1ページで全出走馬の過去5走が載る**ので、16頭なら16ページ→1ページ。
     リクエストが **16分の1** になる（マナー設計§1.1 の趣旨にも合う）
  3. 仕様が求めるのがちょうど「直近5走」（取得項目仕様§4-1）

**さらに単勝オッズと人気もこのページに載っている**ので、B2オッズAPIが取れなかった場合の
保険にもなる（`parse_shutuba_past_html` は odds も返す）。

--- ページ構造（2026-09-19 阪神11R の実サンプルで確認）---

`table.Shutuba_Table.Shutuba_Past5_Table.tablesorter` の tbody の **1行＝1頭**。

  td.Waku1 / td.Waku      枠番 / 馬番
  td.Horse_Info           馬名・血統・厩舎・脚質・オッズ
      a[href*="/horse/"]  → horse_ref（B出馬表と同じ10桁）
      .Popular            → "40.5 (12人気)"  ＝ 単勝オッズと人気
  td.Jockey               性齢・騎手・斤量（今回のレースぶん。past_runsには使わない）
  td.Rest                 休養マーカー（あれば）
  td.Past × 4〜5          1セル＝1走（新しい順）
      .Data01  "2026.05.03 京都 14"        日付・場・**着順**
      .Data02  "東大路S 3勝"                レース名＋クラス（アイコンの文字が "3勝" 形式）
      .Data05  "ダ1400 1:25.2 稍"           馬場＋距離・走破タイム・馬場状態
      .Data03  "16頭 11番 11人 川須栄彦 54.0" 頭数・馬番・人気・騎手・斤量
      .Data06  "3-3 (39.1) 510(0)"         通過順・**上り3F**・馬体重
      .Data07  "ハワイアンタイム (2.5)"       勝ち馬（着差）。自身が1着なら2着馬と負の着差

⚠ **休養のある馬は4走しか載らない**：`td.Rest` が5走目の枠を1つ潰すため
  （実サンプルでも16頭中4頭が4走）。仕様の「直近5走」に対して1走少ないだけで、
  スピード指数§3 の n_usable が自然に小さくなるだけなので許容する。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from scraper.common import constants
from scraper.common.http import get as http_get

logger = logging.getLogger("scraper.c2_shutuba_past")

SHUTUBA_PAST_URL_TMPL = "https://race.netkeiba.com/race/shutuba_past.html?race_id={race_id}"

PAST5_TABLE_SELECTOR = "table.Shutuba_Past5_Table"

_HORSE_ID_RE = re.compile(r"/horse/(\d+)")
_DATE_RE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")
_DIST_RE = re.compile(r"(芝|ダ|障)\s*(\d+)")
_TIME_RE = re.compile(r"(\d+):(\d+(?:\.\d+)?)")
_LAST3F_RE = re.compile(r"\(\s*(\d+\.\d)\s*\)")
_MARGIN_RE = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*\)\s*$")
_ODDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\(\s*(\d+)\s*人気\s*\)")


def _text(node, selector: str) -> str:
    el = node.select_one(selector)
    return el.get_text(" ", strip=True).replace("\xa0", " ") if el else ""


def _to_float(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_time_sec(text: str) -> float | None:
    """"1:25.2" → 85.2。分未満の表記（"58.9"）も受ける。"""
    m = _TIME_RE.search(text)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.search(r"\b(\d{2}\.\d)\b", text)
    return float(m.group(1)) if m else None


def parse_past_cell(cell) -> dict[str, Any] | None:
    """td.Past 1つ（＝1走）を past_runs[] の1要素にする。日付が読めなければ None。"""
    data01 = _text(cell, ".Data01")
    date_m = _DATE_RE.search(data01)
    if date_m is None:
        return None
    date_str = f"{date_m.group(1)}-{int(date_m.group(2)):02d}-{int(date_m.group(3)):02d}"

    # "2026.05.03 京都 14" の 日付と着順を除いた残りが場名（.Num が着順）
    finish_text = _text(cell, ".Num")
    finish = int(finish_text) if finish_text.isdigit() else None
    note = None if (finish is not None or not finish_text) else finish_text  # 中止・除外など
    venue = _DATE_RE.sub("", data01).replace(finish_text, "", 1).strip() if finish_text else None
    venue = venue or None

    data05 = _text(cell, ".Data05")          # "ダ1400 1:25.2 稍"
    dist_m = _DIST_RE.search(data05)
    surface = dist_m.group(1) if dist_m else None
    dist = int(dist_m.group(2)) if dist_m else None
    time_sec = _parse_time_sec(data05)
    going_raw = data05.split()[-1] if data05 else ""
    going = constants.GOING_NORMALIZE.get(going_raw)

    data03 = _text(cell, ".Data03")          # "16頭 11番 11人 川須栄彦 54.0"
    heads_m = re.search(r"(\d+)頭", data03)
    heads = int(heads_m.group(1)) if heads_m else None
    impost_m = re.search(r"(\d+(?:\.\d+)?)\s*$", data03)
    impost = _to_float(impost_m.group(1)) if impost_m else None
    jockey_m = re.search(r"\d+人\s+(\S+)", data03)
    jockey_name = jockey_m.group(1) if jockey_m else None

    data06 = _text(cell, ".Data06")          # "3-3 (39.1) 510(0)"
    last3f_m = _LAST3F_RE.search(data06)
    last3f = float(last3f_m.group(1)) if last3f_m else None

    # "ハワイアンタイム (2.5)" の括弧内が着差。1着なら共通内部フォーマット仕様§2.5 に従い 0 に上書き
    data07 = _text(cell, ".Data07")
    margin_m = _MARGIN_RE.search(data07)
    margin_sec = 0.0 if finish == 1 else (_to_float(margin_m.group(1)) if margin_m else None)

    return {
        "date": date_str,
        "venue": venue,
        "surface": surface,
        "dist": dist,
        "going": going,
        "class": constants.normalize_class_label(_text(cell, ".Data02")),
        "heads": heads,
        "finish": finish,
        "time_sec": time_sec,
        "last3f": last3f,
        "margin_sec": margin_sec,
        "impost": impost,
        "jockey_name": jockey_name,
        "note": note,
    }


def parse_shutuba_past_html(html: str, n_runs: int = 5) -> dict[str, dict[str, Any]]:
    """
    HTML文字列から {horse_ref: {"past_runs": [...], "win_odds": float|None,
                               "popularity": int|None}} を組み立てる。

    テスト・オフライン解析用に公開している。
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one(PAST5_TABLE_SELECTOR)
    if table is None:
        # 200が返っているのに表が無い＝ページ構成の変更かbot判定ページ。黙って空を返さない
        title = soup.title.get_text(strip=True) if soup.title else "(titleなし)"
        logger.warning(
            "過去5走テーブル %s が見つかりません。title=%r / 本文%dバイト",
            PAST5_TABLE_SELECTOR, title, len(html),
        )
        return {}

    result: dict[str, dict[str, Any]] = {}
    for row in table.select("tbody tr"):
        link = row.select_one('td.Horse_Info a[href*="/horse/"]')
        if link is None:
            continue
        ref_m = _HORSE_ID_RE.search(link.get("href", ""))
        if ref_m is None:
            continue

        runs = [parse_past_cell(cell) for cell in row.select("td.Past")]
        runs = [r for r in runs if r is not None][:n_runs]

        odds_m = _ODDS_RE.search(_text(row, ".Popular"))
        result[ref_m.group(1)] = {
            "past_runs": runs,
            "win_odds": float(odds_m.group(1)) if odds_m else None,
            "popularity": int(odds_m.group(2)) if odds_m else None,
        }
    return result


def fetch_shutuba_past(race_source_ref: str, n_runs: int = 5) -> dict[str, dict[str, Any]]:
    """
    1レース分の「過去5走」ページを取得する。

    race_source_ref: netkeibaのレースID（12桁、例 "202609040511"）
    戻り値: {horse_ref: {"past_runs": [...], "win_odds": ..., "popularity": ...}}
    """
    url = SHUTUBA_PAST_URL_TMPL.format(race_id=race_source_ref)
    resp = http_get(url)
    if resp is None:
        raise RuntimeError(f"過去5走ページの取得に失敗しました: {url}")
    resp.encoding = resp.apparent_encoding
    return parse_shutuba_past_html(resp.text, n_runs=n_runs)


def merge_into_race(race: dict[str, Any], by_horse: dict[str, dict[str, Any]]) -> None:
    """
    fetch_shutuba_past の結果を、b_shutuba が作った race dict の entries に反映する。

    オッズは **B2オッズAPIが取れなかったときだけ**埋める（APIの値の方が新しいため上書きしない）。
    """
    for entry in race.get("entries", []):
        ref = (entry.get("horse_ref") or {}).get("netkeiba")
        found = by_horse.get(ref)
        if found is None:
            continue
        entry["past_runs"] = found["past_runs"]
        if entry.get("win_odds") is None and found.get("win_odds") is not None:
            entry["win_odds"] = found["win_odds"]
        if entry.get("popularity") is None and found.get("popularity") is not None:
            entry["popularity"] = found["popularity"]
