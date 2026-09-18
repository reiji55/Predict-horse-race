"""
config/base_times.json 初期構築スクリプト（スピード指数仕様_v1.md §4）

**1回きり**の実行を想定（以後は年1回程度の更新で十分）。成果物はリポジトリにコミットする。

    base_time[venue][surface][dist] = median( win_time − class_offset[class] × dist/2000 )

- 対象：JRA10場 × 芝/ダ × 施行距離の全コース（§4「約120通り」。本スクリプトの COURSES は101通り）
- 全クラスの勝ちタイムを使う（OP以上だけだとサンプル不足になるコースがあるため・§4）
- 中央値を使う（日々の馬場差・極端なペースの外れ値に強い・§4）

--- 3つの収集経路 ---

勝ちタイムの集め方は3通り用意してある。どれも同じ「勝ちタイムレコード」の形に落として
`build_table()` に渡す（レコード＝ {venue, surface, dist, class, win_time, going?, date?}）。

  1. `collect_from_raw()` … **ネットワーク不要**。既にコミット済みの raw/*.json の past_runs から
     `finish == 1` の走（＝その馬が勝った走＝そのレースの勝ちタイム）を拾う。
     週を重ねるほど溜まるが、初回は当然サンプル不足。補助的な経路。
  2. `load_records()` … 手元のJSON/CSVを読む。運用者がページを保存して渡す場合はこれ。
  3. `fetch_course_records()` … netkeibaのレース検索（`?pid=race_search_detail`）を叩く。
     ページ数を抑えるため、1コース×3年ぶんを一覧で取る（§4「一覧系ページを使えばページ数は抑えられる」）。

`--source` で選ぶ。既定は `raw`（ネットワークを使わない安全側）。

--- ⚠ 経路3（netkeiba検索）の未検証部分 ---

**検索フォームのパラメータ名・場コード・track コードは実サンプル未取得のままの想定値**である。
一方、**結果テーブルのパーサーは列インデックス直指定ではなくヘッダー名で列を解決する**ので、
netkeiba側の列構成が変わっても静かにズレることはない（OPEN_QUESTIONS C-6 と同じ轍を踏まないため）。
必要なヘッダーが見つからなければ例外を投げて止まる。

疎通を確認するときは、まず1コースだけ `--dry-run` 無しで叩いて件数を見ること。
0件が返るなら（実在するコースで0件はありえない）パラメータ側が誤っている。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import statistics
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup

from scraper.common import constants
from scraper.common.http import post as http_post

logger = logging.getLogger("scripts.build_base_times")

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "config" / "base_times.json"
SPEED_CONFIG_PATH = ROOT / "config" / "speed_index.json"
RAW_DIR = ROOT / "raw"

SEARCH_URL = "https://db.netkeiba.com/"

# 1コースあたりこの本数に満たなければ中央値を採らない（外れ値1本で基準がぶれるのを防ぐ）
DEFAULT_MIN_SAMPLES = 5

# JRA10場の施行距離（芝／ダ）。§4の「施行距離の全コース」の実体。
# 年に数回しか施行されない距離（東京芝3400=ダイヤモンドS、中山芝3600=ステイヤーズS 等）も
# 列挙してある。サンプルが min_samples に満たなければ表には載らず、レポートに未充足として出る。
COURSES: dict[str, dict[str, list[int]]] = {
    "札幌": {"芝": [1200, 1500, 1800, 2000, 2600], "ダ": [1000, 1700, 2400]},
    "函館": {"芝": [1000, 1200, 1800, 2000, 2600], "ダ": [1000, 1700, 2400]},
    "福島": {"芝": [1200, 1800, 2000, 2600], "ダ": [1150, 1700, 2400]},
    "新潟": {"芝": [1000, 1200, 1400, 1600, 1800, 2000, 2200, 2400], "ダ": [1200, 1800, 2500]},
    "東京": {"芝": [1400, 1600, 1800, 2000, 2300, 2400, 2500, 3400], "ダ": [1300, 1400, 1600, 2100, 2400]},
    "中山": {"芝": [1200, 1600, 1800, 2000, 2200, 2500, 3600], "ダ": [1200, 1800, 2400, 2500]},
    "中京": {"芝": [1200, 1400, 1600, 2000, 2200], "ダ": [1200, 1400, 1800, 1900]},
    "京都": {"芝": [1200, 1400, 1600, 1800, 2000, 2200, 2400, 3000, 3200], "ダ": [1100, 1200, 1400, 1800, 1900]},
    "阪神": {"芝": [1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 3000], "ダ": [1200, 1400, 1800, 2000]},
    "小倉": {"芝": [1200, 1800, 2000, 2600], "ダ": [1000, 1700, 2400]},
}

# netkeibaの場コード（race_id の場部分と同じ並び）。⚠検索フォームでの使用は未検証
VENUE_CODE = {
    "札幌": "01", "函館": "02", "福島": "03", "新潟": "04", "東京": "05",
    "中山": "06", "中京": "07", "京都": "08", "阪神": "09", "小倉": "10",
}

# 検索フォームの馬場コード。⚠未検証
TRACK_CODE = {"芝": "1", "ダ": "2"}

# 結果テーブルのヘッダー名→内部キー。表記ゆれを吸収するため候補を並べる
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("日付",),
    "venue": ("開催",),
    "racename": ("レース名", "レース"),
    "finish": ("着順",),
    "dist": ("距離",),
    "going": ("馬場",),
    "time": ("タイム",),
}
_REQUIRED_COLUMNS = ("finish", "dist", "time")

_DIST_RE = re.compile(r"(芝|ダ|障)\s*(\d+)")
_VENUE_IN_KAISAI_RE = re.compile(r"^\d*(\D+?)\d*$")


# --------------------------------------------------------------------------- 計算


def load_class_offset() -> dict[str, float]:
    with SPEED_CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)["class_offset"]


def normalize_win_time(record: dict[str, Any], class_offset: dict[str, float]) -> float | None:
    """
    勝ちタイムをOP水準に正規化する（§4）：`win_time − class_offset[class] × dist/2000`。

    class が正規化表記でない（＝class_offset に無い）レコードは使えないので None を返す。
    §1 の going/斤量補正はここでは掛けない：§4の式がクラス補正のみを定めており、
    かつ良馬場が全体の大半を占めるので中央値が自然に良馬場水準に落ちるため。
    馬場を絞りたい場合は `build_table(goings=["良"])` を使う。
    """
    dist = record.get("dist")
    win_time = record.get("win_time")
    klass = record.get("class")
    if dist is None or win_time is None or klass not in class_offset:
        return None
    return win_time - class_offset[klass] * (dist / 2000)


def build_table(
    records: Iterable[dict[str, Any]],
    class_offset: dict[str, float] | None = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    goings: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    勝ちタイムレコードの集合から base_times 表を組み立てる（§4）。

    goings: 馬場状態で絞る場合に指定（例 ["良"]）。None なら全馬場（仕様§4のとおり）。
    戻り値: (表, レポート)。レポートは
            {"counts": {"東京/芝/1600": 42, ...}, "skipped": [...], "courses_missing": [...]}
    """
    class_offset = class_offset if class_offset is not None else load_class_offset()

    buckets: dict[tuple[str, str, int], list[float]] = {}
    skipped = 0
    for record in records:
        venue = record.get("venue")
        surface = record.get("surface")
        dist = record.get("dist")
        if venue not in constants.JRA_VENUES or surface not in ("芝", "ダ") or dist is None:
            skipped += 1
            continue
        if goings is not None and record.get("going") not in goings:
            skipped += 1
            continue
        normalized = normalize_win_time(record, class_offset)
        if normalized is None:
            skipped += 1
            continue
        buckets.setdefault((venue, surface, int(dist)), []).append(normalized)

    table: dict[str, dict[str, dict[str, float]]] = {}
    counts: dict[str, int] = {}
    for (venue, surface, dist), values in sorted(buckets.items()):
        key = f"{venue}/{surface}/{dist}"
        counts[key] = len(values)
        if len(values) < min_samples:
            continue
        table.setdefault(venue, {}).setdefault(surface, {})[str(dist)] = round(statistics.median(values), 1)

    missing = [
        f"{venue}/{surface}/{dist}"
        for venue, by_surface in COURSES.items()
        for surface, dists in by_surface.items()
        for dist in dists
        if str(dist) not in table.get(venue, {}).get(surface, {})
    ]
    report = {"counts": counts, "skipped": skipped, "courses_missing": missing}
    return table, report


# --------------------------------------------------------------------------- 経路1：raw から


def collect_from_raw(raw_dir: Path = RAW_DIR) -> list[dict[str, Any]]:
    """
    コミット済みの raw/*.json の past_runs から勝ちタイムを拾う（ネットワーク不要）。

    `finish == 1` の走＝その馬が勝った走なので、その `time_sec` はそのレースの勝ちタイムそのもの。
    同じレースが複数の raw / 複数の馬から重複して入りうるので (date, venue, dist, time) で重複排除する。
    """
    records: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    if not raw_dir.is_dir():
        return records

    for path in sorted(raw_dir.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("raw の読み込みに失敗したのでスキップします path=%s error=%s", path, exc)
            continue
        for race in raw.get("races", []):
            for horse in race.get("horses", []):
                for run in horse.get("past_runs", []) or []:
                    if run.get("finish") != 1 or run.get("time_sec") is None:
                        continue
                    key = (run.get("date"), run.get("venue"), run.get("surface"),
                           run.get("dist"), run.get("time_sec"))
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append({
                        "date": run.get("date"),
                        "venue": run.get("venue"),
                        "surface": run.get("surface"),
                        "dist": run.get("dist"),
                        "going": run.get("going"),
                        "class": run.get("class"),
                        "win_time": run.get("time_sec"),
                    })
    return records


# --------------------------------------------------------------------------- 経路2：手元ファイルから


def load_records(path: Path) -> list[dict[str, Any]]:
    """
    手元のJSON（レコードの配列）またはCSV（ヘッダー付き）を読む。

    必要な列：venue, surface, dist, class, win_time（going は任意）。
    win_time は秒（float）でも "1:33.4" 形式でも受け付ける。
    """
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    else:
        with path.open(encoding="utf-8") as f:
            rows = json.load(f)
        if isinstance(rows, dict):
            rows = rows.get("records", [])

    records = []
    for row in rows:
        records.append({
            "date": row.get("date"),
            "venue": row.get("venue"),
            "surface": row.get("surface"),
            "dist": int(row["dist"]) if row.get("dist") not in (None, "") else None,
            "going": row.get("going") or None,
            "class": row.get("class") or None,
            "win_time": time_to_sec(str(row.get("win_time", ""))),
        })
    return records


def time_to_sec(text: str) -> float | None:
    """"1:33.4" / "58.9" を秒に変換する（c_horse_history と同じ2パターン）。"""
    text = text.strip()
    if not text:
        return None
    m = re.match(r"^(\d+):(\d+(?:\.\d+)?)$", text)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    try:
        return float(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- 経路3：netkeiba検索


def _resolve_columns(table_el) -> dict[str, int]:
    """
    ヘッダー行の文字列から列位置を解決する。

    列インデックス直指定にしないのは、netkeiba側の列構成が変わったときに
    **静かにズレる**のを避けるため（OPEN_QUESTIONS C-6 で指摘されている c_horse_history の弱点）。
    """
    header_cells = table_el.select("thead th") or table_el.select("tr th")
    headers = [cell.get_text(strip=True) for cell in header_cells]
    columns: dict[str, int] = {}
    for key, aliases in _HEADER_ALIASES.items():
        for index, text in enumerate(headers):
            if text in aliases:
                columns[key] = index
                break
    missing = [key for key in _REQUIRED_COLUMNS if key not in columns]
    if missing:
        raise ValueError(
            f"レース検索結果のヘッダーから必要な列を特定できませんでした: {missing} / 実際のヘッダー={headers}"
        )
    return columns


def _parse_class(racename: str) -> str | None:
    """レース名からクラス表記を正規化する（c_horse_history._parse_class と同じ規則）。"""
    paren = re.search(r"\(([^)]+)\)", racename)
    if paren:
        tag = paren.group(1)
        if tag in constants.GRADE_TAG_MAP:
            return constants.GRADE_TAG_MAP[tag]
        for keyword, cls in constants.CONDITION_GRADE_MAP:
            if keyword in tag:
                return cls
    for keyword, cls in constants.CONDITION_GRADE_MAP:
        if keyword in racename:
            return cls
    return None


def parse_race_search_html(html: str) -> list[dict[str, Any]]:
    """
    レース検索結果のHTMLから**1着馬の行だけ**を拾って勝ちタイムレコードにする。

    検索結果は「1行＝1頭の出走」なので、着順1の行がそのレースの勝ち馬＝勝ちタイムになる。
    """
    soup = BeautifulSoup(html, "lxml")
    table_el = soup.select_one("table.race_table_01") or soup.select_one("table.nk_tb_common")
    if table_el is None:
        return []
    columns = _resolve_columns(table_el)

    records: list[dict[str, Any]] = []
    for row in table_el.select("tbody tr") or table_el.select("tr"):
        tds = row.find_all("td")
        if len(tds) <= max(columns.values()):
            continue  # ヘッダー行・広告行

        def cell(key: str) -> str:
            index = columns.get(key)
            return tds[index].get_text(strip=True) if index is not None else ""

        if cell("finish") != "1":
            continue

        dist_m = _DIST_RE.search(cell("dist"))
        if dist_m is None:
            continue
        surface, dist = dist_m.group(1), int(dist_m.group(2))

        kaisai = cell("venue")
        venue_m = _VENUE_IN_KAISAI_RE.match(kaisai)
        venue = venue_m.group(1) if venue_m else kaisai

        going_raw = cell("going")
        date_text = cell("date")

        records.append({
            "date": date_text.replace("/", "-") or None,
            "venue": venue or None,
            "surface": surface,
            "dist": dist,
            "going": constants.GOING_NORMALIZE.get(going_raw, going_raw) or None,
            "class": _parse_class(cell("racename")),
            "win_time": time_to_sec(cell("time")),
        })
    return records


def build_search_params(venue_jp: str, surface: str, dist: int, start_year: int, end_year: int,
                        page: int = 1) -> dict[str, Any]:
    """
    レース検索（`?pid=race_search_detail`）のフォーム値を組み立てる。

    ⚠ **パラメータ名・場コード・trackコードは実サンプル未取得の想定値**（本書冒頭の注記）。
    検証は「実在するコースで0件が返らないこと」で行う。
    """
    return {
        "pid": "race_search_detail",
        "word": "",
        "start_year": str(start_year),
        "start_mon": "1",
        "end_year": str(end_year),
        "end_mon": "12",
        "jyo[]": VENUE_CODE[venue_jp],
        "kyori_min": str(dist),
        "kyori_max": str(dist),
        "track[]": TRACK_CODE[surface],
        "sort": "date",
        "list": "100",
        "page": str(page),
    }


def fetch_course_records(venue_jp: str, surface: str, dist: int, start_year: int, end_year: int,
                         max_pages: int = 10) -> list[dict[str, Any]]:
    """1コース（venue×surface×dist）ぶんの勝ちタイムを、期間まとめて取得する。"""
    records: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        params = build_search_params(venue_jp, surface, dist, start_year, end_year, page=page)
        resp = http_post(SEARCH_URL, data=params)
        if resp is None:
            logger.warning("検索の取得に失敗 %s/%s/%s page=%d", venue_jp, surface, dist, page)
            break
        resp.encoding = resp.apparent_encoding
        page_records = parse_race_search_html(resp.text)
        if not page_records:
            break
        records.extend(page_records)
    if not records:
        logger.warning(
            "%s/%s/%s で0件でした。実在するコースで0件はありえないので、"
            "検索パラメータ（フォーム名・場コード・trackコード）を疑ってください",
            venue_jp, surface, dist,
        )
    return records


def collect_from_netkeiba(start_year: int, end_year: int,
                          courses: dict[str, dict[str, list[int]]] | None = None) -> list[dict[str, Any]]:
    """全コースを順に取得する。**約101コース×数ページ＝数百リクエスト**になるので1回きりの実行に限る。"""
    courses = courses if courses is not None else COURSES
    records: list[dict[str, Any]] = []
    for venue_jp, by_surface in courses.items():
        for surface, dists in by_surface.items():
            for dist in dists:
                logger.info("取得中 %s %s %dm", venue_jp, surface, dist)
                records.extend(fetch_course_records(venue_jp, surface, dist, start_year, end_year))
    return records


# --------------------------------------------------------------------------- CLI


def main() -> None:
    parser = argparse.ArgumentParser(description="config/base_times.json を構築する（スピード指数仕様§4）")
    parser.add_argument("--source", choices=["raw", "file", "netkeiba"], default="raw",
                        help="勝ちタイムの収集経路（既定 raw＝ネットワーク不要）")
    parser.add_argument("--input", type=Path, help="--source file のときの入力JSON/CSV")
    parser.add_argument("--start-year", type=int, default=2023)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--going", action="append", help="馬場を絞る（例 --going 良）。既定は全馬場")
    parser.add_argument("--dry-run", action="store_true", help="書き出さずレポートだけ表示する")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.source == "raw":
        records = collect_from_raw()
    elif args.source == "file":
        if args.input is None:
            parser.error("--source file には --input が必要です")
        records = load_records(args.input)
    else:
        records = collect_from_netkeiba(args.start_year, args.end_year)

    table, report = build_table(records, min_samples=args.min_samples, goings=args.going)

    total_courses = sum(len(d) for by_surface in COURSES.values() for d in by_surface.values())
    filled = total_courses - len(report["courses_missing"])
    print(f"勝ちタイム {len(records)} 件（除外 {report['skipped']} 件）")
    print(f"基準タイムが埋まったコース: {filled}/{total_courses}")
    if report["courses_missing"]:
        print(f"未充足（サンプル {args.min_samples} 本未満）: {len(report['courses_missing'])} コース")
        print("  " + ", ".join(report["courses_missing"][:20])
              + (" ..." if len(report["courses_missing"]) > 20 else ""))

    if args.dry_run:
        print("--dry-run のため書き出しません")
        return

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(table, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(f"書き出し完了: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
