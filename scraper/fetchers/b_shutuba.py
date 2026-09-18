"""
Bページ：出馬表（shutuba）

出典：取得項目_共通内部フォーマット仕様_v1.1.md §1.1（表の# B行）／§2.3・§2.4（出力の型）
取得タイミング：当日朝 / 1週あたり4〜6ページ
取れるもの：レース基本情報、馬場状態、出走馬一覧
  （枠・馬番・馬名・性齢・斤量・騎手・厩舎・馬体重・単勝オッズ・人気）

URL形式（実サンプルのcanonicalから確認）：
  https://race.netkeiba.com/race/shutuba.html?race_id={race_source_ref}

出力は 共通内部フォーマット の races[] 1件（entries[] は §2.4準拠、
past_runs / jockey_stats / trainer_stats はこの時点では空 or null。C/D/Eページの結果で埋める）。

--- 実サンプル解析で分かったこと（2レース×2状態で確認済み） ---

**枠順未確定・レース日より前のページ**（例：七夕賞2026、レース5日前・登録22頭）と
**枠順確定後のページ**（例：府中牝馬S2026、確定後）で、HTML構造が変わる：

| フィールド | 未確定時 | 確定後 |
|---|---|---|
| td.Waku のclass/テキスト | class="Waku"（数字無し）、テキスト空 | class="Waku1"のように数字付き、テキストも"1" |
| td.Umaban のclass/テキスト | class="Umaban"、テキスト空 | class="Umaban1"、テキストも数字 |
| 天候・馬場状態（RaceData01） | 記載なし | "… / 天候:曇 / 馬場:稍" の形で記載 |
| 馬体重（td.Weight） | 空 | "470(-4)" の形で記載（パース済み・確認OK） |
| 単勝オッズ・人気 | "---.-" / "**"（プレースホルダー） | **確定後ページでもプレースホルダーのまま**（2レースとも）。オッズ・人気はJS/AJAXで描画される値でHTMLに焼き込まれていない可能性が高い。必要なら別ページ（オッズページ：`odds/index.html?race_id=...`）を別途取得する方式に切り替える要検討 |

→ 馬番はテキスト優先、空なら `tr id="tr_{馬番}"` にフォールバック（odds/ninki spanのidとも一致確認済み）。
→ 枠番はテキストが取れる時のみ。取れない場合は None（フォールバック手段なし）。
→ グレードは見出し内の `Icon_GradeType{1,2,3}` から g1/g2/g3 を判定。
  G1〜G3以外は RaceData02 の条件文字列から判定
  （"オープン"→op, "３勝クラス"→3win, "２勝クラス"→2win, "１勝クラス"→1win, "未勝利"→mi）。

【残課題】単勝オッズ・人気が2サンプルとも取得できていない。当日朝の「本番に近い」ページで
再確認するか、オッズ専用ページの構造を別途調査する必要がある。
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from scraper.common import constants
from scraper.common.http import get as http_get

SHUTUBA_URL_TMPL = "https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"

_GRADE_ICON_RE = re.compile(r"Icon_GradeType([1-3])\b")


def _parse_grade(soup: BeautifulSoup, race_data02_text: str) -> str | None:
    heading = soup.select_one(".RaceList_Item02 .RaceName, h1.RaceName")
    if heading:
        for span in heading.select("span"):
            classes = span.get("class") or []
            for cls in classes:
                m = _GRADE_ICON_RE.fullmatch(cls)
                if m:
                    return f"g{m.group(1)}"
    for keyword, grade in constants.CONDITION_GRADE_MAP:
        if keyword in race_data02_text:
            return grade
    return None


def _parse_odds_or_none(text: str) -> float | None:
    text = text.strip()
    if not text or "-" in text or text in ("**", "---.-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_popularity_or_none(text: str) -> int | None:
    text = text.strip()
    if not text or text in ("**", "--"):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_body_weight_or_none(text: str) -> dict[str, Any] | None:
    """
    想定書式（要検証）: "484(-4)" -> {"value": 484, "diff": -4}
    未計測時は空文字 or "計不" 等 -> None
    """
    text = text.strip()
    m = re.match(r"(\d+)\(([+\-]?\d+)\)", text)
    if not m:
        return None
    return {"value": int(m.group(1)), "diff": int(m.group(2))}


def _extract_ref_id(href: str | None, pattern: str) -> str | None:
    if not href:
        return None
    m = re.search(pattern, href)
    return m.group(1) if m else None


def _parse_race_meta(soup: BeautifulSoup, race_source_ref: str) -> dict[str, Any]:
    name_el = soup.select_one(".RaceName")
    race_no_el = soup.select_one(".RaceNum")
    data01_el = soup.select_one(".RaceData01")
    data02_el = soup.select_one(".RaceData02")

    name = name_el.get_text(strip=True) if name_el else None
    race_no_text = race_no_el.get_text(strip=True) if race_no_el else ""
    race_no_m = re.search(r"(\d+)", race_no_text)
    race_no = int(race_no_m.group(1)) if race_no_m else None

    data01_text = data01_el.get_text(" ", strip=True) if data01_el else ""
    data02_text = data02_el.get_text(" ", strip=True) if data02_el else ""

    post_time_m = re.search(r"(\d{1,2}:\d{2})発走", data01_text)
    post_time = post_time_m.group(1) if post_time_m else None

    surface = None
    if "芝" in data01_text:
        surface = "芝"
    elif "ダ" in data01_text:
        surface = "ダ"

    dist_m = re.search(r"(\d{3,4})m", data01_text)
    dist = int(dist_m.group(1)) if dist_m else None

    # 天候・馬場状態（当日朝以降のページのみ存在。例："... / 天候:曇 / 馬場:稍"）
    weather_m = re.search(r"天候:(\S+)", data01_text)
    weather = weather_m.group(1) if weather_m else None

    going_m = re.search(r"馬場:(\S+)", data01_text)
    going_raw = going_m.group(1) if going_m else None
    going = constants.GOING_NORMALIZE.get(going_raw, going_raw) if going_raw else None

    heads_m = re.search(r"(\d+)頭", data02_text)
    heads = int(heads_m.group(1)) if heads_m else None

    note = "ハンデ" if "ハンデ" in data02_text else None

    venue = None
    for jp_name in constants.JRA_VENUES:
        if jp_name in data02_text:
            venue = jp_name
            break

    grade = _parse_grade(soup, data02_text)

    # 開催日の年月日はタイトルタグから取得（例："... | 2026年7月12日 福島11R ..."）
    date_str = None
    day = None
    if soup.title:
        title_text = soup.title.get_text()
        date_m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", title_text)
        if date_m:
            y, mo, d = (int(g) for g in date_m.groups())
            date_str = f"{y:04d}-{mo:02d}-{d:02d}"
            import datetime
            weekday = datetime.date(y, mo, d).weekday()  # 0=月
            day = "土" if weekday == 5 else ("日" if weekday == 6 else None)

    race_id_internal = None
    if date_str and venue and race_no:
        race_id_internal = constants.race_id(date_str, venue, race_no)

    return {
        "id": race_id_internal,  # date/venue/race_no が全部取れたときのみ埋まる
        "source_refs": {"netkeiba": race_source_ref, "jravan": None},
        "date": date_str,
        "day": day,
        "venue": venue,
        "race_no": race_no,
        "name": name,
        "grade": grade,
        "post_time": post_time,
        "course": {"surface": surface, "dist": dist, "heads": heads, "note": note},
        # 当日朝以降のページはRaceData01に含まれる（例："天候:曇 / 馬場:稍"）。
        # レース日より前のページ（枠順未確定段階）では両方 None になる
        "going": going,
        "weather": weather,
        "odds_updated_at": None,  # 【未実装】ページ内に更新時刻の表記が見当たらない。要検証
    }


def _parse_entry(row) -> dict[str, Any]:
    tds = row.find_all("td", recursive=False)
    # 列位置は実サンプルで固定（15列）: Waku,Umaban,CheckMark,HorseInfo,Barei,斤量,Jockey,Trainer,
    #                                  Weight,Popular(odds),Popular_Ninki,FavRegist,FavMemo,Note0,Note1

    # 枠番・馬番：class名が "Waku"/"Umaban"（未確定）→ "Waku1"/"Umaban5"のように
    # 数字付きclassになり、td内テキストにも数字が入る（当日朝以降・確定後）。
    # レース日より前（枠順未確定）は class・テキストとも数字なしで空になる。
    # その場合、馬番だけは <tr id="tr_{馬番}"> から代替取得できる（odds/ninki spanのidと一致確認済み）。
    # 枠番はテキストが空の場合の代替手段が無いため None のまま返す。
    waku_td = row.select_one('td[class*="Waku"]')
    umaban_td = row.select_one('td[class*="Umaban"]')

    waku_text = waku_td.get_text(strip=True) if waku_td else ""
    waku = int(waku_text) if waku_text.isdigit() else None

    umaban_text = umaban_td.get_text(strip=True) if umaban_td else ""
    if umaban_text.isdigit():
        num = int(umaban_text)
    else:
        tr_id = row.get("id", "")
        num_m = re.match(r"tr_(\d+)", tr_id)
        num = int(num_m.group(1)) if num_m else None

    horse_a = row.select_one(".HorseName a")
    name = horse_a.get_text(strip=True) if horse_a else None
    horse_ref = _extract_ref_id(horse_a["href"] if horse_a else None, r"/horse/(\d+)")

    barei_text = tds[4].get_text(strip=True) if len(tds) > 4 else ""
    barei_m = re.match(r"(牡|牝|セ)(\d+)", barei_text)
    sex = barei_m.group(1) if barei_m else None
    age = int(barei_m.group(2)) if barei_m else None

    impost_text = tds[5].get_text(strip=True) if len(tds) > 5 else ""
    impost = float(impost_text) if re.match(r"^\d+(\.\d+)?$", impost_text) else None

    jockey_a = row.select_one(".Jockey a")
    jockey_name = jockey_a.get_text(strip=True) if jockey_a else None
    jockey_ref = _extract_ref_id(jockey_a["href"] if jockey_a else None, r"/jockey/result/recent/(\w+)")  # \w+: 英字混在IDの保険

    trainer_a = row.select_one(".Trainer a")
    trainer_name = trainer_a.get_text(strip=True) if trainer_a else None
    trainer_ref = _extract_ref_id(trainer_a["href"] if trainer_a else None, r"/trainer/result/recent/(\w+)")

    weight_td = row.select_one(".Weight")
    body_weight = _parse_body_weight_or_none(weight_td.get_text(strip=True) if weight_td else "")

    odds_span = row.select_one("[id^=odds-]")
    win_odds = _parse_odds_or_none(odds_span.get_text() if odds_span else "")

    ninki_span = row.select_one("[id^=ninki-]")
    popularity = _parse_popularity_or_none(ninki_span.get_text() if ninki_span else "")

    return {
        "num": num,
        "waku": waku,
        "name": name,
        "horse_ref": {"netkeiba": horse_ref},
        "sex": sex,
        "age": age,
        "impost": impost,
        "body_weight": body_weight,
        "jockey": {"name": jockey_name, "ref": {"netkeiba": jockey_ref}},
        "trainer": {"name": trainer_name, "ref": {"netkeiba": trainer_ref}},
        "win_odds": win_odds,
        "place_odds": None,  # 【未実装】複勝オッズのセレクタは今回のサンプルで未特定
        "popularity": popularity,
        "combo_odds": None,  # 常にnull（取得項目仕様§2.4の予約枠）
        "past_runs": [],          # Cページのフェッチャーが埋める
        "jockey_stats": None,     # Dページのフェッチャーが埋める
        "trainer_stats": None,    # Eページのフェッチャーが埋める
    }


def parse_shutuba_html(html: str, race_source_ref: str) -> dict[str, Any]:
    """HTML文字列から races[] 1件分を組み立てる（テスト・オフライン解析用に公開）。"""
    soup = BeautifulSoup(html, "lxml")
    race = _parse_race_meta(soup, race_source_ref)

    table = soup.select_one("table.Shutuba_Table")
    entries = []
    if table:
        for row in table.find_all("tr", class_="HorseList"):
            entries.append(_parse_entry(row))
    # HTML内の行順は馬名の五十音順になっていることがある（枠順未確定段階）ため、
    # 馬番が取れているものは馬番順に並べ直す
    entries.sort(key=lambda e: (e["num"] is None, e["num"]))
    race["entries"] = entries
    return race


def fetch_shutuba(race_source_ref: str) -> dict[str, Any]:
    """
    出馬表ページから1レース分の基本情報＋出走馬一覧を取得する。

    race_source_ref: netkeibaのレースID（Aページ由来。12桁、例 "202603020611"）
    戻り値: 共通内部フォーマット仕様§2.3 の races[] 1件相当の dict
    """
    url = SHUTUBA_URL_TMPL.format(race_id=race_source_ref)
    resp = http_get(url)
    if resp is None:
        raise RuntimeError(f"出馬表の取得に失敗しました: {url}")
    return parse_shutuba_html(resp.text, race_source_ref)
