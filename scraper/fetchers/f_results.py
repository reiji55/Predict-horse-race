"""
Fページ：レース結果・払戻ページ

出典：取得項目_共通内部フォーマット仕様_v1.1.md §1.1（表の# F行）
取得タイミング：レース後 / 1週あたり4〜6ページ
取れるもの：確定着順、公式配当表 → results.json用（データスキーマ仕様v1.2 §5）

URL形式：
  https://race.netkeiba.com/race/result.html?race_id={race_source_ref}

--- 実サンプル解析で分かったこと（2026-09-13 中山1R で確認）---

**このページは素のHTMLに中身が入っている**（A・B2・D/Eのような後付けAJAXではない）。

### 着順テーブル

`table#All_Result_Table`。列は
  0=着順 1=枠 2=馬番 3=馬名 4=性齢 5=斤量 6=騎手 7=タイム 8=着差
  9=人気 10=単勝オッズ 11=後3F 12=コーナー通過順 13=厩舎 14=馬体重(増減)

`finish` に必要なのは 0=着順 と 2=馬番 だけ。着順が数字でない行（中止・除外・取消）は
順位付けから外す（C馬戦績と同じ扱い）。

### 払戻テーブル

`table.Payout_Detail_Table` が2つあり、行ごとに券種のclassが付く：
  Tansho（単勝）/ Fukusho（複勝）/ Wakuren（枠連）/ Umaren（馬連）/
  Wide（ワイド）/ Umatan（馬単）/ Fuku3（3連複）/ Tan3（3連単）

1行の構造（ワイドのように複数当たる券種は、組み合わせが `<ul>` の数だけ並ぶ）：

```html
<tr class="Wide">
  <th>ワイド</th>
  <td class="Result">
    <ul><li><span>4</span></li><li><span>5</span></li><li></li></ul>   <!-- 1組目 -->
    <ul><li><span>4</span></li><li><span>8</span></li><li></li></ul>   <!-- 2組目 -->
    <ul><li><span>5</span></li><li><span>8</span></li><li></li></ul>   <!-- 3組目 -->
  </td>
  <td class="Payout"><span>610円<br>580円<br>710円</span></td>          <!-- 組ごとの配当 -->
  <td class="Ninki"><span>6人気</span>...</td>
</tr>
```

- 組み合わせは **`<ul>` 1つ = 1組**（空の `<li>` は桁合わせの埋め草なので無視する）
- 配当は `td.Payout` のテキストを **`<br>` で分割**して組の順に対応させる
- 金額は `"3,630円"` のようにカンマと単位付き → 数値に正規化
- 同着で配当が複数出る場合も、この「ulの数だけ配当が並ぶ」構造で表現されるため
  そのまま複数エントリになる（集計側は順不同で照合するので問題ない）

本アプリが使うのは **ワイド / 馬連 / 3連複** の3券種だけ（単勝・複勝は使わない＝
データスキーマ仕様§5）。他の券種も同じ構造なので、必要になれば `PAYOUT_ROW_CLASSES` に足すだけ。

### 【発見】1日分の払戻をまとめて取れるページもある

`https://race.netkeiba.com/top/payback_list.html` は**その開催日の全レースの払戻表**を
1ページに持っている（実サンプルでは12レース分＝`Payout_Detail_Table` が24個、
各レースの `race_id` へのリンク付き）。ただし**着順（finish）は載っていない**ため、
results.json を作るには結果ページ側が要る。対象がメイン4〜6レースなら結果ページ方式で
ページ数見積もり（§1.1）に収まるので、v1は結果ページを使う。
過去分をまとめて埋めたくなったときの選択肢として記録しておく。
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from scraper.common.http import get as http_get

RESULT_URL_TMPL = "https://race.netkeiba.com/race/result.html?race_id={race_id}"

# 払戻行のclass → データスキーマ仕様§5 の券種名。本アプリが使う3券種のみ
PAYOUT_ROW_CLASSES = {
    "Wide": "ワイド",
    "Umaren": "馬連",
    "Fuku3": "3連複",
}

_YEN_RE = re.compile(r"([\d,]+)\s*円")


def _to_yen(text: str) -> int | None:
    """"3,630円" → 3630。数値にできなければ None。"""
    m = _YEN_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_finish(soup: BeautifulSoup) -> list[int]:
    """着順テーブルから、1着から順の馬番リストを作る。着順が数字でない行は除外する。"""
    table = soup.select_one("table#All_Result_Table")
    if table is None:
        return []

    ordered: list[tuple[int, int]] = []
    for row in table.select("tr"):
        cells = row.select("td")
        if len(cells) < 3:
            continue  # ヘッダー行など
        rank_text = cells[0].get_text(strip=True)
        num_text = cells[2].get_text(strip=True)
        if not rank_text.isdigit() or not num_text.isdigit():
            continue  # 中止・除外・取消・失格
        ordered.append((int(rank_text), int(num_text)))

    ordered.sort(key=lambda x: x[0])
    return [num for _, num in ordered]


def _parse_payout_row(row) -> list[dict[str, Any]]:
    """払戻テーブルの1行から [{"horses": [...], "pay": int}, ...] を作る。"""
    result_cell = row.select_one("td.Result")
    payout_cell = row.select_one("td.Payout")
    if result_cell is None or payout_cell is None:
        return []

    combos: list[list[int]] = []
    for combo in result_cell.select("ul"):
        nums = [
            int(text) for text in
            (li.get_text(strip=True) for li in combo.select("li"))
            if text.isdigit()   # 空の<li>は桁合わせの埋め草
        ]
        if nums:
            combos.append(nums)

    # 配当は <br> 区切りで組の順に並ぶ
    pays = [
        _to_yen(part) for part in
        payout_cell.get_text("\n", strip=True).split("\n")
        if part.strip()
    ]

    dividends = []
    for combo, pay in zip(combos, pays):
        if pay is not None:
            dividends.append({"horses": combo, "pay": pay})
    return dividends


def parse_dividends(soup: BeautifulSoup) -> dict[str, list[dict[str, Any]]]:
    """払戻テーブルから公式配当表（データスキーマ仕様§5 の dividends）を作る。"""
    dividends: dict[str, list[dict[str, Any]]] = {}
    for row_class, bet_type in PAYOUT_ROW_CLASSES.items():
        for row in soup.select(f"table.Payout_Detail_Table tr.{row_class}"):
            entries = _parse_payout_row(row)
            if entries:
                dividends.setdefault(bet_type, []).extend(entries)
    return dividends


def parse_result_html(html: str) -> dict[str, Any]:
    """
    結果ページのHTMLから {"finish": [...], "dividends": {...}} を作る
    （テスト・オフライン解析用に公開）。
    """
    soup = BeautifulSoup(html, "lxml")
    return {"finish": parse_finish(soup), "dividends": parse_dividends(soup)}


def fetch_results(race_source_ref: str) -> dict[str, Any]:
    """
    レース結果ページから確定着順・公式配当表を取得する。

    race_source_ref: netkeibaのレースID（12桁）
    戻り値: {"finish": [馬番...], "dividends": {...}}
            この形をそのまま `results.build_results.build_results()` に渡せる
            （dividends の型は データスキーマ仕様v1.2 §5）
    """
    url = RESULT_URL_TMPL.format(race_id=race_source_ref)
    resp = http_get(url)
    if resp is None:
        raise RuntimeError(f"レース結果ページの取得に失敗しました: {url}")
    return parse_result_html(resp.text)
