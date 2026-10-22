"""
血統（父・母・母父）の取得と保存。

出典：取得項目_共通内部フォーマット仕様_v1.1.md §1（血統はMVPスコープ外＝「将来」の項目）。
ここで取得と蓄積だけを先に始める。**予想ロジック（logic/）はまだ血統を使わない**。

経路は2つ。

1. **毎週の取得（追加アクセスなし）**
   C2（出馬表の「過去5走」ページ、`race.netkeiba.com/race/shutuba_past.html`）の
   `td.Horse_Info` に血統が載っている。C2は既に毎週取っているので、同じHTMLから読むだけ。
   `c2_shutuba_past.parse_shutuba_past_html` が各馬の `pedigree` を返し、
   `merge_into_race` が raw の entry に `pedigree` として入れる。
   build_raw の最後に `update_store_from_races` で `data/pedigree/pedigree.csv` に追記する
   （血統は変わらないので、一度入った馬は二度と書き換えない）。

2. **過去の馬の一括取得（手元のPCで手動実行）**
   `scripts/backfill_pedigree.py` が `db.netkeiba.com/horse/ped/{horse_id}/`（5代血統表）を取る。
   `db.netkeiba.com/horse/` は GitHub Actions から叩くとbot判定される（c2_shutuba_past の冒頭参照）
   ので、**GitHub Actions では動かさない**。間隔は scraper.common.http の3〜5秒をそのまま守る。

--- 【要確認】C2の Horse_Info 内の並び（実サンプル未確認）---

netkeibaの馬柱は Horse_Info の中に1行ずつ div を並べている前提で読む：
  .Horse01  父
  .Horse02  馬名（a[href*="/horse/"]）
  .Horse03  母
  .Horse04  （母父）← 括弧つき
  .Horse05  厩舎
実サンプル（tests/samples/shutuba_past_hanshin11.html）で確かめるまでは推定。
クラスが見つからないときは None を返し、その馬の pedigree は入れない（全体は止めない）。
確かめ方は docs/PEDIGREE.md。

--- 5代血統表（/horse/ped/）の読み方（実ページの構造で確認済み）---

`table.blood_table` の td は rowspan で世代が分かる。5代表なら 父・母 が rowspan=16、
その次の世代（父父・父母・母父・母母）が rowspan=8。表の世代数が違っても崩れないよう、
「いちばん大きい rowspan の2つ＝父・母」「その半分の4つ＝父父・父母・母父・母母」で読む。
"""
from __future__ import annotations

import csv
import datetime
import logging
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

logger = logging.getLogger("scraper.pedigree")

ROOT = Path(__file__).resolve().parent.parent.parent
STORE_PATH = ROOT / "data" / "pedigree" / "pedigree.csv"
PED_URL_TMPL = "https://db.netkeiba.com/horse/ped/{horse_id}/"
FIELDS = ["horse_ref", "sire", "sire_ref", "dam", "dam_ref", "damsire", "damsire_ref", "source", "fetched_at"]

_HORSE_REF_RE = re.compile(r"/horse/(?:ped/)?([0-9a-zA-Z]+)")
JST = datetime.timezone(datetime.timedelta(hours=9))


def _name_and_ref(node) -> tuple[str | None, str | None]:
    """td / div から（馬名, netkeiba馬ID）を取る。リンクが無ければ文字だけ。"""
    if node is None:
        return None, None
    link = node.find("a", href=True)
    text = (link.get_text(" ", strip=True) if link else node.get_text(" ", strip=True)).replace("\xa0", " ").strip()
    if len(text) >= 2 and text[0] in "(（" and text[-1] in ")）":      # "(母父名)" の外側の括弧だけ外す
        text = text[1:-1].strip()                                          # "名 (米)" の国名表記は残す
    ref = None
    if link is not None:
        m = _HORSE_REF_RE.search(link["href"])
        ref = m.group(1) if m else None
    return (text or None), ref


# --- 経路1：C2（過去5走ページ）の Horse_Info ---------------------------------------

def parse_horse_info_pedigree(horse_info) -> dict[str, Any] | None:
    """
    C2 の td.Horse_Info から {sire, sire_ref, dam, dam_ref, damsire, damsire_ref} を読む。
    3つとも取れなければ None（推定した並びと違う＝ページ構造が想定と違う）。
    """
    if horse_info is None:
        return None
    sire, sire_ref = _name_and_ref(horse_info.select_one(".Horse01"))
    dam, dam_ref = _name_and_ref(horse_info.select_one(".Horse03"))
    damsire, damsire_ref = _name_and_ref(horse_info.select_one(".Horse04"))
    if not (sire or dam or damsire):
        return None
    return {"sire": sire, "sire_ref": sire_ref, "dam": dam, "dam_ref": dam_ref,
            "damsire": damsire, "damsire_ref": damsire_ref}


# --- 経路2：5代血統表（/horse/ped/） ----------------------------------------------

def parse_ped_page_html(html: str) -> dict[str, Any] | None:
    """`db.netkeiba.com/horse/ped/{id}/` のHTMLから 父・母・母父 を読む。表が無ければ None。"""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.blood_table")
    if table is None:
        title = soup.title.get_text(strip=True) if soup.title else "(titleなし)"
        logger.warning("血統表 table.blood_table が見つかりません。title=%r / 本文%dバイト", title, len(html))
        return None
    cells = []
    for td in table.find_all("td"):
        try:
            span = int(td.get("rowspan", 1))
        except ValueError:
            span = 1
        cells.append((span, td))
    top = max((s for s, _ in cells), default=1)
    parents = [td for s, td in cells if s == top]
    grand = [td for s, td in cells if s == top // 2] if top >= 2 else []
    if len(parents) < 2:
        logger.warning("血統表の父・母を特定できません（最大rowspan=%d が %d 個）", top, len(parents))
        return None
    sire, sire_ref = _name_and_ref(parents[0])
    dam, dam_ref = _name_and_ref(parents[1])
    damsire, damsire_ref = _name_and_ref(grand[2]) if len(grand) >= 4 else (None, None)
    return {"sire": sire, "sire_ref": sire_ref, "dam": dam, "dam_ref": dam_ref,
            "damsire": damsire, "damsire_ref": damsire_ref}


def fetch_ped_page(horse_id: str) -> dict[str, Any] | None:
    """5代血統表を1頭分取る（手元のPC用。GitHub Actions では使わない）。"""
    from scraper.common.http import get as http_get     # テストでネットワークを読み込まないよう遅延import

    resp = http_get(PED_URL_TMPL.format(horse_id=horse_id))
    if resp is None:
        return None
    resp.encoding = resp.apparent_encoding
    return parse_ped_page_html(resp.text)


# --- 保存（data/pedigree/pedigree.csv） --------------------------------------------

def load_store(path: Path = STORE_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as f:
        return {row["horse_ref"]: row for row in csv.DictReader(f)}


def save_store(store: dict[str, dict[str, str]], path: Path = STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for ref in sorted(store):
            w.writerow({k: store[ref].get(k) or "" for k in FIELDS})


def add_record(store: dict[str, dict[str, str]], horse_ref: str, ped: dict[str, Any], source: str) -> bool:
    """未登録の馬だけ追加する（血統は変わらないので上書きしない）。追加したら True。"""
    if not horse_ref or not ped or horse_ref in store:
        return False
    store[horse_ref] = {**{k: ped.get(k) for k in FIELDS if k in ped}, "horse_ref": horse_ref, "source": source,
                        "fetched_at": datetime.datetime.now(JST).isoformat(timespec="seconds")}
    return True


def update_store_from_races(races: list[dict[str, Any]], path: Path = STORE_PATH) -> int:
    """raw の races[].entries[].pedigree を保存先に追記する。追加した頭数を返す。"""
    store = load_store(path)
    added = 0
    for race in races:
        for entry in race.get("entries", []):
            ref = (entry.get("horse_ref") or {}).get("netkeiba")
            if add_record(store, ref, entry.get("pedigree"), "shutuba_past"):
                added += 1
    if added:
        save_store(store, path)
    logger.info("血統：%d 頭を追加（保存済み %d 頭）", added, len(store))
    return added
