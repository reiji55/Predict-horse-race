"""
血統（父・母・母父）の取得と保存。

出典：取得項目_共通内部フォーマット仕様_v1.1.md §1（血統はMVPスコープ外＝「将来」の項目）。
ここで取得と蓄積だけを先に始める。**予想ロジック（logic/）はまだ血統を使わない**。

経路は3つ（docs/PEDIGREE.md）。

1. **毎週の取得（追加アクセスなし）**
   C2（出馬表の「過去5走」ページ、`race.netkeiba.com/race/shutuba_past.html`）の
   `td.Horse_Info` に血統が載っている。C2は既に毎週取っているので、同じHTMLから読むだけ。
2. **過去のメインレース**（`scripts/backfill_pedigree_races.py`、手動 workflow）
3. **馬ごとの5代血統表**（`scripts/backfill_pedigree.py`、手元のPCだけ）

--- 安全ゲート：C2の並びを実ページで確かめるまで、経路1・2は保存しない ---

Horse_Info の中の並び（下の C2_SELECTORS）は推定。推定のまま保存すると、別の項目を
「血統」として永久に残してしまう。そこで：

- `scripts/verify_pedigree_layout.py`（手動 workflow `manual_pedigree_verify.yml`）が実ページを取り、
  父・母・母父を知っている馬（イクイノックスなど）と突き合わせる。
- 全頭一致したときだけ `data/pedigree/c2_layout_check.json` に `"verified": true` と
  そのとき使ったセレクタを書く。
- `c2_layout_verified()` はこのファイルが verified で、かつ記録されたセレクタが今のコードと
  同じときだけ True。セレクタを変えたら確認はやり直しになる。
- False の間は、C2 から血統を読まない（raw にも入れない）・pedigree.csv に書かない・
  過去レースの取り込みは動かない。

--- 保存の約束（data/pedigree/pedigree.csv）---

- **父・母・母父の3つがそろった血統だけ**保存する。1つでも欠けたら保存せず、次の機会に取り直す
  （欠けたまま「取得済み」にしない）。
- 一度入った馬は書き換えない（血統は変わらない）。別の取得で違う値が来たら、黙って捨てずに
  `data/pedigree/conflicts.csv` に両方を残す。

--- 5代血統表（/horse/ped/）の読み方 ---

`table.blood_table` の td は rowspan で世代が分かる。5代表なら 父・母 が rowspan=16、
その次の世代（父父・父母・母父・母母）が rowspan=8。「いちばん大きい rowspan がちょうど2つ＝父・母」
「その半分がちょうど4つ＝父父・父母・母父・母母」のときだけ読み、形が違えば None。
"""
from __future__ import annotations

import csv
import datetime
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

logger = logging.getLogger("scraper.pedigree")

ROOT = Path(__file__).resolve().parent.parent.parent
PEDIGREE_DIR = ROOT / "data" / "pedigree"
STORE_PATH = PEDIGREE_DIR / "pedigree.csv"
CONFLICTS_PATH = PEDIGREE_DIR / "conflicts.csv"
LAYOUT_CHECK_PATH = PEDIGREE_DIR / "c2_layout_check.json"
PED_URL_TMPL = "https://db.netkeiba.com/horse/ped/{horse_id}/"
FIELDS = ["horse_ref", "sire", "sire_ref", "dam", "dam_ref", "damsire", "damsire_ref", "source", "fetched_at"]
NAME_FIELDS = ("sire", "dam", "damsire")
CONFLICT_FIELDS = ["horse_ref", "field", "stored", "new", "stored_source", "new_source", "found_at"]

# C2 の td.Horse_Info の中で、父・母・母父が入っている要素（推定。verify で確かめる）
C2_SELECTORS = {"sire": ".Horse01", "dam": ".Horse03", "damsire": ".Horse04"}

_HORSE_REF_RE = re.compile(r"/horse/(?:ped/)?([0-9a-zA-Z]+)")
JST = datetime.timezone(datetime.timedelta(hours=9))


def _now() -> str:
    return datetime.datetime.now(JST).isoformat(timespec="seconds")


def normalize_name(name: str | None) -> str:
    """比較用：全角半角をそろえ、空白を除く。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name or ""))


def is_complete(ped: dict[str, Any] | None) -> bool:
    """父・母・母父の3つとも名前があるか。"""
    return bool(ped) and all((ped.get(k) or "").strip() for k in NAME_FIELDS)


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


# --- 安全ゲート ------------------------------------------------------------------

def c2_layout_verified(path: Path | None = None) -> bool:
    """C2 の並びを実ページで確かめ済みで、そのときのセレクタが今と同じなら True。"""
    path = path or LAYOUT_CHECK_PATH
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return rec.get("verified") is True and rec.get("selectors") == C2_SELECTORS


# --- 経路1・2：C2（過去5走ページ）の Horse_Info ---------------------------------------

def parse_horse_info_pedigree(horse_info) -> dict[str, Any] | None:
    """
    C2 の td.Horse_Info から {sire, sire_ref, dam, dam_ref, damsire, damsire_ref} を読む。
    1つも取れなければ None。一部だけのときもそのまま返す（保存するかは is_complete で決める）。
    """
    if horse_info is None:
        return None
    out: dict[str, Any] = {}
    for field, selector in C2_SELECTORS.items():
        out[field], out[f"{field}_ref"] = _name_and_ref(horse_info.select_one(selector))
    if not any(out[k] for k in NAME_FIELDS):
        return None
    return out


# --- 経路3：5代血統表（/horse/ped/） ----------------------------------------------

def parse_ped_page_html(html: str) -> dict[str, Any] | None:
    """`db.netkeiba.com/horse/ped/{id}/` のHTMLから 父・母・母父 を読む。表の形が想定と違えば None。"""
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
    if len(parents) != 2 or len(grand) != 4:
        logger.warning("血統表の形が想定と違います（最大rowspan=%d が %d 個、その半分が %d 個）",
                       top, len(parents), len(grand))
        return None
    sire, sire_ref = _name_and_ref(parents[0])
    dam, dam_ref = _name_and_ref(parents[1])
    damsire, damsire_ref = _name_and_ref(grand[2])
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for ref in sorted(store):
            w.writerow({k: store[ref].get(k) or "" for k in FIELDS})
    tmp.replace(path)       # 途中で落ちても壊れたCSVを残さない


def _log_conflicts(rows: list[dict[str, str]], path: Path) -> None:
    new_file = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CONFLICT_FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)


def add_record(store: dict[str, dict[str, str]], horse_ref: str, ped: dict[str, Any] | None, source: str,
               conflicts_path: Path | None = None) -> str:
    """
    1頭分を保存先に入れる。戻り値：
      "added"      … 新しく保存した（3つそろっているときだけ）
      "incomplete" … 父・母・母父のどれかが欠けている。保存しない（次の機会に取り直す）
      "same"       … もう保存済みで、値も同じ
      "conflict"   … もう保存済みで、値が違う。上書きせず conflicts.csv に残す
      "skipped"    … horse_ref が無い
    """
    if not horse_ref:
        return "skipped"
    if not is_complete(ped):
        return "incomplete"
    old = store.get(horse_ref)
    if old is None:
        store[horse_ref] = {**{k: ped.get(k) for k in FIELDS if k in ped}, "horse_ref": horse_ref,
                            "source": source, "fetched_at": _now()}
        return "added"
    diffs = [k for k in NAME_FIELDS if normalize_name(old.get(k)) != normalize_name(ped.get(k))]
    if not diffs:
        return "same"
    found_at = _now()
    _log_conflicts([{"horse_ref": horse_ref, "field": k, "stored": old.get(k) or "", "new": ped.get(k) or "",
                     "stored_source": old.get("source") or "", "new_source": source, "found_at": found_at}
                    for k in diffs], conflicts_path or CONFLICTS_PATH)
    logger.warning("血統の食い違い：%s の %s（保存済み %s / 今回 %s）。上書きせず conflicts.csv に記録",
                   horse_ref, ",".join(diffs), [old.get(k) for k in diffs], [ped.get(k) for k in diffs])
    return "conflict"


def update_store_from_races(races: list[dict[str, Any]], path: Path = STORE_PATH,
                            verified: bool | None = None, conflicts_path: Path | None = None) -> dict[str, int]:
    """raw の races[].entries[].pedigree を保存先に入れる。結果の内訳（added / incomplete / ...）を返す。"""
    verified = c2_layout_verified() if verified is None else verified
    counts = {"added": 0, "incomplete": 0, "same": 0, "conflict": 0, "skipped": 0}
    if not verified:
        logger.info("血統：C2の並びが未確認のため保存しません（docs/PEDIGREE.md の確認手順）")
        return counts
    store = load_store(path)
    for race in races:
        for entry in race.get("entries", []):
            if not entry.get("pedigree"):
                continue
            ref = (entry.get("horse_ref") or {}).get("netkeiba")
            counts[add_record(store, ref, entry.get("pedigree"), "shutuba_past", conflicts_path)] += 1
    if counts["added"]:
        save_store(store, path)
    logger.info("血統：%s（保存済み %d 頭）", counts, len(store))
    return counts
