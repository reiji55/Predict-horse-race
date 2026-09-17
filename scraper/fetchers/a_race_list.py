"""
Aページ：開催日別レース一覧

出典：取得項目_共通内部フォーマット仕様_v1.md §1.1（表の# A行）
取得タイミング：前日〜当日朝 / 1週あたり土日各1"日" だが、下記の発見によりHTTPリクエストは
土日各2回（date_list + race_list_sub）＝週合計4回になる。ページ数見積もり（§1.1「約90〜130ページ」）
への影響は軽微。

--- 実サンプル解析で分かったこと（2026-06-20土曜、東京・阪神・函館の3場）---

出馬表（Bページ）・オッズ（B2）と同じパターン：**レース一覧の中身も静的HTMLには無く、
JSが後からAJAXで流し込む**。`https://race.netkeiba.com/top/race_list.html?kaisai_date=YYYYMMDD`
を素で取得しても `<div id="date_list"></div>` は空。2段階のAJAXを踏む必要がある。

### ステップ1：日付タブ一覧の取得

```
GET https://race.netkeiba.com/top/race_list_get_date_list.html
    ?kaisai_date={YYYYMMDD}&encoding=UTF-8
```

レスポンスはHTML断片（前週/次週ボタン＋当該週7日分のタブ `<li date="..." group="...">`）。
**探している日付の `<li date="{YYYYMMDD}">` が持つ `group` 属性の値が、ステップ2で必要な
`current_group` パラメータそのもの**（例 `group="1020260620"`）。tab要素のhref内クエリからも
同じ値が取れるが、liのgroup属性から直接読む方がシンプルで確実。

`group` の値の意味（"10"+その週の土曜日付、のように見える）は未確認のため、
決め打ちで組み立てず、**必ずステップ1のレスポンスから実際の値を読む**方針にした
（弾かれるリスクを避ける。B2オッズAPIのCookie問題と同種の「不確実な部分は都度確認」の姿勢）。

### ステップ2：該当日のレース一覧本体

```
GET https://race.netkeiba.com/top/race_list_sub.html
    ?kaisai_date={YYYYMMDD}&current_group={group}
```

レスポンスはHTML断片。開催場ごとに `dl.RaceList_DataList` が並び、各場の中に
`li.RaceList_DataItem` でレースが並ぶ（1〜12R程度）。

### パース対象の構造

```html
<dl class="RaceList_DataList">
  <dt class="RaceList_DataHeader">
    <p class="RaceList_DataTitle"><small>3回</small> 東京 <small>5日目</small></p>
    ...
  </dt>
  <dd class="RaceList_Data"><ul>
    <li class="RaceList_DataItem">
      <a href="../race/result.html?race_id=202605030509&rf=race_list">
        <div class="Race_Num Race_Fixed"><span>...9R</span></div>
        <div class="RaceList_ItemContent">
          <div class="RaceList_ItemTitle">
            <span class="ItemTitle">町田特別</span>
            <span class="Icon_GradeType Icon_GradeType17 Icon_GradePos01"></span>  <!-- 特別戦のみ -->
          </div>
          <div class="RaceData">
            <span class="Icon_GradeType Icon_GradeType13"></span>  <!-- ハンデ戦のみ（下記参照） -->
            <span class="RaceList_Itemtime">14:35 </span>
            <span class="RaceList_ItemLong Turf">芝2400m</span>
            <span class="RaceList_Itemnumber">10頭 </span>
          </div>
        </div>
      </a>
    </li>
    ...
  </ul></dd>
</dl>
```

- **場名**：`p.RaceList_DataTitle` のテキストは "3回 東京 5日目"（<small>で回数・日数を挟む）。
  空白区切りで3トークンになるので中央（[1]）が場名。JRA中央10場のみ登場する前提
  （地方・海外はこのページに出ない）
- **race_id**：レースへのリンク（`a[href*=race_id]`）のクエリから直接取れる（12桁）。
  `Race_Num`（○R）を正規表現でパースするより確実
- **レース名／グレード**：`ItemTitle` span のテキストが条件文字列（"3歳未勝利"等）の場合は
  b_shutuba/c_horse_historyと共有の `constants.CONDITION_GRADE_MAP` で判定できる。
  特別戦・重賞（"町田特別"のような固有名）は同マップでは判定できず、代わりに隣の
  `span.Icon_GradeType.Icon_GradeTypeN`（N=5,16,17,18など）が付くが、**この数値の意味は
  Bページで確認済みの `Icon_GradeType{1,2,3}`→g1/g2/g3 とは別スケール**（このサンプルにG1〜G3が
  無いため対応関係を確認できていない）。誤った決め打ちで grade を汚すより、**未確認のまま
  grade=None + 生アイコンクラスを `grade_icon_raw` に温存**する方針にした（後日Bページの
  結果と突き合わせて確定させる）。**【要確認】**
- **ハンデ戦フラグ（note）**：`div.RaceData` の先頭に付く `Icon_GradeType13` は、観測した6件の
  特別戦のうち「相模湖特別・スレイプニル・ストークS・天保山S・STV杯」の5件に付き、
  「町田特別・舞子特別・下北半島特別・駒ケ岳特別」には付かない。実際のレース条件と照合すると
  前者はハンデ戦、後者は別定/定量戦であり一致する。**推定の域を出ないが妥当性は高いと判断し、
  `note="ハンデ"` として採用**（データスキーマ仕様の `course.note` 例と型が一致）。**【要確認】**
- **距離・馬場**：`div.RaceData` 内の span を総なめし、`(芝|ダ|障)\\d+m` に一致するテキストを探す
  （通常は `class="RaceList_ItemLong Turf/Dart"` だが、障害戦は `class=""` の無地spanで来るため
  クラス名に頼らず正規表現マッチで拾う設計にした）
- **天候・当日馬場状態**：`dt div.RaceList_DataDesc` に場単位で載っている
  （例 `芝(D)：稍` `ダ：不`）が、天候はアイコン専用（`Icon_Weather03`等）でテキストが無く、
  アイコン番号→天候名の対応が未確認のため**今回は取得しない**（B出馬表側が
  "天候:曇 / 馬場:稍" の形でテキスト取得済み・v4§2.2なので、そちらを正とする）
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from scraper.common import constants
from scraper.common.http import get as http_get

DATE_LIST_API_URL = "https://race.netkeiba.com/top/race_list_get_date_list.html"
RACE_LIST_SUB_URL = "https://race.netkeiba.com/top/race_list_sub.html"
TOP_PAGE_URL_TMPL = "https://race.netkeiba.com/top/race_list.html?kaisai_date={date}"

_DIST_RE = re.compile(r"^(芝|ダ|障)(\d+)m$")
_RACE_NUM_RE = re.compile(r"(\d+)\s*R")

# ItemTitleが条件文字列でない場合の補助マッチ（英字「OP」表記。取得項目仕様の正規化表には
# 「オープン」しか無いが、Aページ独自の略記のためここでローカルに補完する）
_OP_ABBR_RE = re.compile(r"(?:^|[^A-Za-z])OP(?:[^A-Za-z]|$)")

_WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]  # datetime.weekday(): 月=0…日=6


@dataclass
class RaceListEntry:
    """1レース分の一覧情報（このあと B〜F の入力になる最小限の情報）"""
    date: str          # "2026-07-05"
    day: str           # "土" / "日"（まれに祝日開催で平日になりうるため月〜日全対応）
    venue: str         # 日本語場名。例 "小倉"
    race_no: int        # 例 11
    name: str           # レース名（条件戦は"3歳未勝利"等の条件文字列がそのまま入る）
    grade: str | None   # 正規化クラス表記（取得項目仕様§2.1）。特別戦・重賞は現状None（要確認、本体docstring参照）
    post_time: str       # "HH:MM"
    source_ref: str       # netkeibaのレースID（12桁）
    surface: str | None = None   # "芝"/"ダ"/"障"
    dist: int | None = None      # m
    heads: int | None = None     # 出走頭数
    note: str | None = None      # "ハンデ" 等（推定判定。§本体docstring参照）
    grade_icon_raw: str | None = None  # 未確定のgrade判定用に、見つかったIcon_GradeTypeクラスを温存


def _parse_venue(title_text: str) -> str | None:
    """"3回 東京 5日目" のようなテキストから場名だけを抜く。"""
    parts = title_text.split()
    if len(parts) >= 3:
        return parts[1]
    if len(parts) == 1:
        return parts[0]
    return None


def _parse_grade(title_text: str) -> str | None:
    """ItemTitleのテキストから条件クラスを判定する（b_shutuba/c_horse_historyと共有のマップ）。"""
    for keyword, cls in constants.CONDITION_GRADE_MAP:
        if keyword in title_text:
            return cls
    if _OP_ABBR_RE.search(title_text):
        return "op"
    return None  # 特別戦・重賞名など条件文字列でないもの（要確認、本体docstring参照）


def _parse_race_item(li, venue: str) -> dict[str, Any] | None:
    a = li.select_one("a[href*=race_id]")
    if a is None:
        return None
    qs = parse_qs(urlparse(a["href"]).query)
    race_ids = qs.get("race_id")
    if not race_ids:
        return None
    source_ref = race_ids[0]

    race_num_el = li.select_one("div.Race_Num")
    race_no_text = race_num_el.get_text() if race_num_el else ""
    m = _RACE_NUM_RE.search(race_no_text)
    if m is None:
        return None
    race_no = int(m.group(1))

    title_el = li.select_one("div.RaceList_ItemTitle span.ItemTitle")
    title_text = title_el.get_text(strip=True) if title_el else ""
    grade = _parse_grade(title_text)

    grade_icon_el = li.select_one("div.RaceList_ItemTitle span.Icon_GradeType")
    grade_icon_raw = None
    if grade_icon_el is not None:
        # class属性は ["Icon_GradeType", "Icon_GradeTypeN", "Icon_GradePos01"] のような形。
        # 意味の分かっている共通クラスを除いた残りを生値として保持する
        classes = [c for c in grade_icon_el.get("class", []) if c not in ("Icon_GradeType", "Icon_GradePos01")]
        grade_icon_raw = classes[0] if classes else None

    time_el = li.select_one("span.RaceList_Itemtime")
    post_time = time_el.get_text(strip=True) if time_el else None

    racedata_el = li.select_one("div.RaceData")
    surface = dist = None
    if racedata_el is not None:
        for sp in racedata_el.select("span"):
            text = sp.get_text(strip=True)
            dm = _DIST_RE.match(text)
            if dm:
                surface, dist = dm.group(1), int(dm.group(2))
                break

    heads_el = li.select_one("span.RaceList_Itemnumber")
    heads = None
    if heads_el is not None:
        heads_text = heads_el.get_text(strip=True).replace("頭", "")
        heads = int(heads_text) if heads_text.isdigit() else None

    # ハンデ戦フラグ（推定。本体docstring参照）：RaceData直下のIcon_GradeType13
    handicap_el = li.select_one("div.RaceData > span.Icon_GradeType.Icon_GradeType13")
    note = "ハンデ" if handicap_el is not None else None

    return {
        "venue": venue,
        "race_no": race_no,
        "name": title_text,
        "grade": grade,
        "grade_icon_raw": grade_icon_raw,
        "post_time": post_time,
        "source_ref": source_ref,
        "surface": surface,
        "dist": dist,
        "heads": heads,
        "note": note,
    }


def parse_date_list_html(html: str, date_compact: str) -> str | None:
    """
    ステップ1（race_list_get_date_list.html）のレスポンスから、
    指定日 `<li date="{date_compact}">` の group 属性値（=current_group）を取り出す。
    見つからなければ None（呼び出し側でエラー扱い）。
    """
    soup = BeautifulSoup(html, "lxml")
    li = soup.select_one(f'li[date="{date_compact}"]')
    if li is None:
        return None
    return li.get("group")


def parse_race_list_sub_html(html: str, date_str: str) -> list[RaceListEntry]:
    """
    ステップ2（race_list_sub.html）のレスポンスから RaceListEntry のリストを組み立てる。
    テスト・オフライン解析用に公開。

    date_str: "2026-06-20" のようなISO8601日付文字列（レスポンス本文には日付そのものが
              含まれないため、呼び出し側から渡す）
    """
    soup = BeautifulSoup(html, "lxml")
    d = datetime.date.fromisoformat(date_str)
    day = _WEEKDAY_JP[d.weekday()]

    entries: list[RaceListEntry] = []
    for dl in soup.select("dl.RaceList_DataList"):
        title_el = dl.select_one("p.RaceList_DataTitle")
        if title_el is None:
            continue
        venue = _parse_venue(title_el.get_text())
        if venue is None:
            continue
        for li in dl.select("li.RaceList_DataItem"):
            parsed = _parse_race_item(li, venue)
            if parsed is None:
                continue  # 想定外の行構造はスキップして続行（マナー設計§1.1の原則）
            entries.append(
                RaceListEntry(
                    date=date_str,
                    day=day,
                    venue=parsed["venue"],
                    race_no=parsed["race_no"],
                    name=parsed["name"],
                    grade=parsed["grade"],
                    post_time=parsed["post_time"],
                    source_ref=parsed["source_ref"],
                    surface=parsed["surface"],
                    dist=parsed["dist"],
                    heads=parsed["heads"],
                    note=parsed["note"],
                    grade_icon_raw=parsed["grade_icon_raw"],
                )
            )
    return entries


def _fetch_current_group(date_compact: str) -> str:
    """ステップ1を叩いて current_group を取得する。取れなければ例外。"""
    headers = {
        "X-Requested-With": "XMLHttpRequest",  # B2 APIと同様の推定ヘッダー。本番未検証【要確認】
        "Referer": TOP_PAGE_URL_TMPL.format(date=date_compact),
    }
    params = {"kaisai_date": date_compact, "encoding": "UTF-8"}
    resp = http_get(DATE_LIST_API_URL, params=params, headers=headers)
    if resp is None:
        raise RuntimeError(f"日付タブ一覧の取得に失敗しました: kaisai_date={date_compact}")
    group = parse_date_list_html(resp.text, date_compact)
    if group is None:
        raise RuntimeError(
            f"指定日のタブが日付タブ一覧に見つかりませんでした: kaisai_date={date_compact}"
        )
    return group


def fetch_race_list(date_str: str) -> list[RaceListEntry]:
    """
    指定日の開催レース一覧を取得する（絞り込みなし・全レース対象。引き継ぎ書v5§4より確定）。

    date_str: "2026-07-05" のようなISO8601日付文字列
    戻り値: RaceListEntry のリスト（その日の全レース、絞り込みなし）

    内部で2段階のAJAXを叩く（本体docstring参照）：
      1. race_list_get_date_list.html でその日の current_group を確認
      2. race_list_sub.html で実際のレース一覧を取得
    """
    date_compact = date_str.replace("-", "")
    group = _fetch_current_group(date_compact)

    headers = {
        "X-Requested-With": "XMLHttpRequest",  # 【要確認】本番未検証
        "Referer": TOP_PAGE_URL_TMPL.format(date=date_compact),
    }
    params = {"kaisai_date": date_compact, "current_group": group}
    resp = http_get(RACE_LIST_SUB_URL, params=params, headers=headers)
    if resp is None:
        raise RuntimeError(f"レース一覧本体の取得に失敗しました: kaisai_date={date_compact}")

    return parse_race_list_sub_html(resp.text, date_str)
