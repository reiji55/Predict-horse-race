"""
C2（出馬表「過去5走」ページ）の td.Horse_Info で、父・母・母父がどこに入っているかを実ページで確かめる。

父・母・母父をよく知られた馬（下の EXPECTED）が出たレースのページを取り、今のセレクタ
（pedigree.C2_SELECTORS）で読んだ値と照らし合わせる。**全頭の3項目がすべて一致したときだけ**
data/pedigree/c2_layout_check.json に "verified": true を書く。一致しなければ false と、
Horse_Info の中身の並び（クラス名と文字）を書くので、それを見てセレクタを直せる。

pedigree.c2_layout_verified() がこのファイルを読み、true のときだけ血統の保存が動く。
HTMLそのものは保存しない（netkeibaの著作物。リポジトリは公開）。

GitHub Actions の手動 workflow（.github/workflows/manual_pedigree_verify.yml）で動かす。
アクセスは3ページ（約15秒）。

使い方：
  python -m scripts.verify_pedigree_layout
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scraper.fetchers import c2_shutuba_past, pedigree

logger = logging.getLogger("scripts.verify_pedigree_layout")

# レースID → {netkeiba馬ID: (馬名, 父, 母, 母父)}。どれも広く知られた血統。
EXPECTED: dict[str, dict[str, tuple[str, str, str, str]]] = {
    "202206050811": {   # 2022年 有馬記念
        "2019105219": ("イクイノックス", "キタサンブラック", "シャトーブランシュ", "キングヘイロー"),
        "2018105027": ("エフフォーリア", "エピファネイア", "ケイティーズハート", "ハーツクライ"),
    },
    "202305020811": {   # 2023年 東京
        "2018105355": ("ソングライン", "キズナ", "ルミナスパレード", "シンボリクリスエス"),
    },
    "202409020411": {   # 2024年 阪神
        "2020103532": ("タスティエーラ", "サトノクラウン", "パルティトゥーラ", "マンハッタンカフェ"),
    },
}


def horse_infos(html: str) -> dict[str, Any] | None:
    """過去5走の表から {馬ID: td.Horse_Info}。表が無ければ None。"""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one(c2_shutuba_past.PAST5_TABLE_SELECTOR)
    if table is None:
        return None
    out = {}
    for row in table.select("tbody tr"):
        link = row.select_one('td.Horse_Info a[href*="/horse/"]')
        m = c2_shutuba_past._HORSE_ID_RE.search(link.get("href", "")) if link else None
        if m:
            out[m.group(1)] = row.select_one("td.Horse_Info")
    return out


def structure(horse_info) -> list[dict[str, str]]:
    """Horse_Info の中でクラスの付いた要素の並び（セレクタを直すときの手がかり）。"""
    items = []
    for el in horse_info.find_all(True):
        cls = el.get("class")
        if cls:
            items.append({"tag": el.name, "class": " ".join(cls), "text": el.get_text(" ", strip=True)[:40]})
        if len(items) >= 30:
            break
    return items


def check_layout(pages: dict[str, str | None],
                 expected: dict[str, dict[str, tuple[str, str, str, str]]] = EXPECTED) -> dict[str, Any]:
    """ページ（レースID→HTML、取れなければ None）と期待値から確認結果を作る。"""
    horses, ok = [], True
    for race_id, exp in expected.items():
        html = pages.get(race_id)
        infos = horse_infos(html) if html else None
        for horse_id, (name, *want) in exp.items():
            rec: dict[str, Any] = {"race_id": race_id, "horse_id": horse_id, "name": name,
                                   "expected": dict(zip(pedigree.NAME_FIELDS, want))}
            if html is None:
                rec["problem"] = "ページを取得できない"
            elif infos is None:
                rec["problem"] = "過去5走の表が無い"
            elif horse_id not in infos:
                rec["problem"] = "この馬の行が無い"
            else:
                got = pedigree.parse_horse_info_pedigree(infos[horse_id]) or {}
                rec["got"] = {k: got.get(k) for k in pedigree.NAME_FIELDS}
                rec["match"] = {k: pedigree.normalize_name(got.get(k)) == pedigree.normalize_name(v)
                                for k, v in rec["expected"].items()}
                if not all(rec["match"].values()):
                    rec["problem"] = "値が一致しない"
                    rec["structure"] = structure(infos[horse_id])
            ok = ok and "problem" not in rec
            horses.append(rec)
    return {"verified": ok, "selectors": pedigree.C2_SELECTORS,
            "checked_at": datetime.datetime.now(pedigree.JST).isoformat(timespec="seconds"),
            "n_horses": len(horses), "n_ok": sum(1 for h in horses if "problem" not in h), "horses": horses}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C2の血統の並びを実ページで確かめる")
    ap.add_argument("--out", default=str(pedigree.LAYOUT_CHECK_PATH))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)

    pages: dict[str, str | None] = {}
    for race_id in EXPECTED:
        try:
            pages[race_id] = c2_shutuba_past.fetch_shutuba_past_html(race_id)
        except RuntimeError:
            logger.exception("取得失敗: %s", race_id)
            pages[race_id] = None
    report = check_layout(pages)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for h in report["horses"]:
        logger.info("%s %s：%s", h["name"], h.get("got"), h.get("problem", "一致"))
    logger.info("結果：%d/%d 頭一致 → verified=%s（%s）", report["n_ok"], report["n_horses"], report["verified"], out)
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
