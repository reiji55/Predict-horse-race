"""
リーディング取得API（netkeiba 内部AJAX API）— D（騎手）・E（調教師）で共有する部品

出典：`db.sp.netkeiba.com/jockey/jockey_leading.html` と同 trainer 版のHTML、および
そこから読み込まれる `org_dbapi.action.js` の `getLeadingData()` を解析して確定。

--- 発見の経緯（引き継ぎ書v7 §2 の続き）---

リーディングページも A・B2 と同じ「静的HTMLは空、AJAXが後から描画」パターンだった。
表が入る場所は `<ul class="List_01 Leading_List"><div id="leading_detail"></div></ul>` で空。
ページ内のインラインJSに呼び出しパラメータがそのまま書かれている：

    var api_url  = 'https://db.sp.netkeiba.com';
    var category = 'jockey';   // trainer版は 'trainer'
    var limit    = 20;
    var bel      = '';         // ''=全国 / '1'=関東 / '2'=関西
    var year     = '2026';
    var sort_key = 'win';      // win / prize / avw(勝率) / avs(連対率) / avt(複勝率)
    getLeadingData(category, 'leading_detail', bel, year, sort_key, limit, fn);

`getLeadingData()` の実体（org_dbapi.action.js）は下記のリクエストを投げる：

    GET https://db.sp.netkeiba.com/
        ?pid=api_get_leading&input=UTF-8&output=jsonp
        &limit={limit}&category={category}&page={page}&show=leading_detail
        &bel={bel}&year={year}&sort_key={sort_key}

**D と E は category が違うだけで同一のAPI**。成功時の処理が `$("#...").html(data)` なので、
**レスポンスはJSONではなくHTML断片**（下記のとおりJSON文字列で包まれて返る）。
ページングは `page=1,2,3…`（「もっと見る」ボタンが page を増やして再取得する）。

--- ⚠ 場別リーディングは存在しない（OPEN_QUESTIONS B-3）---

絞り込みは **全国 / 関東 / 関西**（`bel`）＝**所属（美浦・栗東）**であって開催場ではない。
年と並び順以外の切り口は無く、PC版 `db.netkeiba.com/?pid=jockey_leading` も実体が無い。
取得項目仕様§2.6 が定めていた騎手の `scope="venue"`（当該場成績）は、この方式では取れない。
→ 騎手も `scope="overall"` で取得する（仕様§2.6 を改訂済み）。場別成績が欲しくなったら、
  騎手個別ページ（出走騎手ぶんだけ）を将来の拡張候補として検討する。

--- レスポンスの中身（実サンプルで確認：category=jockey, year=2026, sort_key=win）---

`output=json` のレスポンスは **HTML断片をJSON文字列で包んだもの**（先頭が `"` で、
中身の `"` や改行がエスケープされている）。json でデコードすると下記の `<li>` の繰り返しになる：

```html
<li>
  <a href="https://db.sp.netkeiba.com/jockey/01174/" class="LinkBox_01">
    <div class="LinkBox_Item02">
      <h2><span class="Icon_DB Ranking_Icn01"></span><span class="Barn03">栗東</span>岩田望来</h2>
      <div class="List_TextBox">
        <p>104勝</p>
        <p>勝率：18.1%&nbsp;連対率：31.2%</p>
      </div>
    </div>
  </a>
</li>
```

- **ref は5桁ゼロ埋め**（岩田望来=01174 / Ｃ.ルメール=05339）。
  引き継ぎ書v7 §2.5 の「B出馬表の jockey_ref と桁が揃うか」の確認事項は、これで**一致が確認できた**
- 所属（栗東／美浦）が `span.Barn03` に入る

⚠ **着度数（1着/2着/3着/騎乗数）は入っていない**。載るのは **勝利数・勝率・連対率** だけ。
そこで本実装は率から逆算する：

    starts  = wins / (勝率/100)
    seconds = starts × (連対率/100) − wins
    thirds  = starts × (複勝率/100) − wins − seconds     ← 複勝率が載っていれば

率は小数第1位までなので starts には丸め誤差が乗る（勝率18.1%・104勝なら 573〜576 の幅）。
分母が数百あるので相対誤差は1%未満で、③人的スコアは縮小推定を通すため実用上は問題ない。

⚠ **複勝率は sort_key=win のレスポンスには載っていない**（連対率まで）。
そのため既定では `thirds=None` になり、③人的スコアは**複勝率ではなく連対率で評価する**
（買い目生成仕様§1.5 は「複勝率」と書いているが、取得できないための代替。
全騎手が同じ指標で揃うので、レース内z標準化の相対評価としては同等に機能する）。
`sort_key=avt`（複勝率順）のレスポンスに複勝率が載るなら thirds も埋まる
→ 実サンプルが取れたら `SORT_PLACE_RATE` で取り直す運用に切り替えられる。

⚠ **limit は効かない**。`limit=100` を指定しても **20件**しか返らなかった（サーバー側で固定）。
全騎手（JRA約150人）を取るにはページングが要る。ただし本アプリの対象はメインレースなので、
騎乗するのは上位騎手が中心。**既定では5ページ＝上位100人まで**取得し、
表に載らない騎手は `jockey_stats=None`（③が欠損扱い）とする。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from scraper.common.http import get as http_get

logger = logging.getLogger("scraper.leading_api")

LEADING_API_URL = "https://db.sp.netkeiba.com/"
LEADING_PAGE_URL_TMPL = "https://db.sp.netkeiba.com/{category}/{category}_leading.html"

# 所属での絞り込み（開催場ではない点に注意）
BEL_ALL = ""
BEL_EAST = "1"    # 関東（美浦）
BEL_WEST = "2"    # 関西（栗東）

# 並び順。着度数そのものは取れる前提だが、複勝率順にしておくと上位から埋まる
SORT_WIN = "win"
SORT_PLACE_RATE = "avt"

DEFAULT_LIMIT = 100   # 指定しても20件しか返らない（実サンプルで確認）。ページングで補う
DEFAULT_MAX_PAGES = 5  # 20件×5 = 上位100人まで。メインレースに乗るのは上位騎手が中心

# refの抽出（5桁ゼロ埋めだが、英字混在の保険で \w+）
_REF_RE = re.compile(r"/(?:jockey|trainer)/+(\w+)")  # スラッシュ重複にも耐える
_WINS_RE = re.compile(r"([\d,]+)\s*勝")
_WIN_RATE_RE = re.compile(r"勝率[：:]\s*([\d.]+)\s*%")
_PLACE2_RATE_RE = re.compile(r"連対率[：:]\s*([\d.]+)\s*%")
_PLACE3_RATE_RE = re.compile(r"複勝率[：:]\s*([\d.]+)\s*%")


def build_params(category: str, year: str, *, bel: str = BEL_ALL, sort_key: str = SORT_WIN,
                 limit: int = DEFAULT_LIMIT, page: int = 1, output: str = "json") -> dict[str, Any]:
    """getLeadingData() が投げるクエリをそのまま組み立てる。"""
    return {
        "pid": "api_get_leading",
        "input": "UTF-8",
        "output": output,
        "limit": limit,
        "category": category,
        "page": page,
        "show": "leading_detail",
        "bel": bel,
        "year": year,
        "sort_key": sort_key,
    }


def unwrap_response(text: str) -> str:
    """
    APIレスポンスからHTML断片を取り出す。

    - `output=json` … 全体がJSON文字列（先頭が `"`）。json でデコードする
    - `output=jsonp` … `callback("<li>…")` の形。外側を剥がしてからデコードする
    - 生HTMLがそのまま返る場合はそのまま返す
    """
    text = (text or "").strip()
    if not text:
        return ""

    if text[0] not in '"{[<':
        # jsonp：callback(...) の外側を剥がす
        m = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", text, re.DOTALL)
        if m:
            text = m.group(1).strip()

    if text.startswith('"'):
        try:
            return json.loads(text)          # JSON文字列 → 中身のHTML
        except json.JSONDecodeError:
            return text
    return text


def fetch_leading_html(category: str, year: str, *, bel: str = BEL_ALL,
                       sort_key: str = SORT_WIN, limit: int = DEFAULT_LIMIT,
                       page: int = 1) -> str:
    """
    リーディングAPIを叩いて、表本体のHTML断片を返す。

    category: "jockey" / "trainer"
    year: "2026" のような集計年（取得項目仕様§2.6 の period）
    """
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": LEADING_PAGE_URL_TMPL.format(category=category),
        "Accept": "text/html, */*; q=0.01",
    }
    resp = http_get(LEADING_API_URL, params=build_params(
        category, year, bel=bel, sort_key=sort_key, limit=limit, page=page), headers=headers)
    if resp is None:
        raise RuntimeError(f"リーディングAPIの取得に失敗しました: category={category} year={year}")
    return unwrap_response(resp.text)


def _to_int(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def _derive_counts(wins: int, win_rate: float | None, place2_rate: float | None,
                   place3_rate: float | None) -> dict[str, Any]:
    """
    勝利数と各種率から着度数を逆算する（本モジュール冒頭の注記を参照）。

    starts  = wins / 勝率
    seconds = starts × 連対率 − wins
    thirds  = starts × 複勝率 − wins − seconds     ← 複勝率が無ければ None
    """
    if not win_rate:
        return {"starts": None, "wins": wins, "seconds": None, "thirds": None}

    starts = round(wins / (win_rate / 100.0))
    seconds = None
    thirds = None
    if place2_rate is not None:
        seconds = max(0, round(starts * place2_rate / 100.0) - wins)
    if place3_rate is not None and seconds is not None:
        thirds = max(0, round(starts * place3_rate / 100.0) - wins - seconds)

    return {"starts": starts, "wins": wins, "seconds": seconds, "thirds": thirds}


def parse_leading_html(html: str, category: str, year: str, *,
                       bel: str = BEL_ALL) -> dict[str, dict[str, Any]]:
    """
    リーディング表のHTML断片を {ref: stats} に変換する（取得項目仕様§2.6）。

    stats = {"scope": "overall", "period": year, "name": str, "barn": str|None,
             "starts": int|None, "wins": int, "seconds": int|None, "thirds": int|None}

    ref は netkeiba の騎手ID／調教師ID（5桁ゼロ埋め）。B出馬表で取得済みの
    jockey_ref / trainer_ref と同じ形式であることは実サンプルで確認済み（引き継ぎ書v7 §2.5）。
    """
    soup = BeautifulSoup(unwrap_response(html), "lxml")
    stats: dict[str, dict[str, Any]] = {}

    for item in soup.select("li"):
        link = item.select_one("a[href]")
        if link is None:
            continue
        ref_m = _REF_RE.search(link["href"])
        if ref_m is None:
            continue
        ref = ref_m.group(1)

        text = item.get_text(" ", strip=True).replace("\xa0", " ")
        wins = _to_int(_WINS_RE.search(text).group(1)) if _WINS_RE.search(text) else None
        if wins is None:
            continue  # 勝利数が読めない行は行そのものが想定外。スキップして続行

        def _rate(pattern: re.Pattern[str]) -> float | None:
            m = pattern.search(text)
            return float(m.group(1)) if m else None

        barn_el = item.select_one(".Barn03")
        barn = barn_el.get_text(strip=True) if barn_el else None
        heading = item.select_one("h2")
        name = heading.get_text(" ", strip=True) if heading else ""
        if barn and name.startswith(barn):
            name = name[len(barn):].strip()

        record = {"scope": "overall", "period": year, "name": name or None, "barn": barn}
        record.update(_derive_counts(wins, _rate(_WIN_RATE_RE), _rate(_PLACE2_RATE_RE),
                                     _rate(_PLACE3_RATE_RE)))
        stats[ref] = record

    return stats


def fetch_leading(category: str, year: str, *, bel: str = BEL_ALL,
                  sort_key: str = SORT_WIN, limit: int = DEFAULT_LIMIT,
                  max_pages: int = DEFAULT_MAX_PAGES) -> dict[str, dict[str, Any]]:
    """
    ページングを追いながら {ref: stats} にまとめる。

    max_pages: 1ページ20件固定なので、既定の5で上位100人まで。
               新しい行が増えなくなった時点で打ち切る。
    """
    merged: dict[str, dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        html = fetch_leading_html(category, year, bel=bel, sort_key=sort_key,
                                  limit=limit, page=page)
        stats = parse_leading_html(html, category, year, bel=bel)
        new_keys = set(stats) - set(merged)
        # ★ 2026-09-19 の初回本番実行で判明：同じ limit=100 を渡しているのに
        #   jockey は1ページ20件・2ページ目で打ち切り（計40人）、trainer は100件/ページで221人。
        #   カテゴリによって limit の効き方が違うらしく、jockey 側はページ間に
        #   飛びがある可能性がある（＝上位40人ではなく「1〜20位と101〜120位」かもしれない）。
        #   判定するために、各ページの先頭と末尾の名前を必ず残す。
        names = [s.get("name") for s in stats.values()]
        logger.info("%s リーディング %dページ目: %d件（うち新規 %d件・累計 %d件）先頭=%s 末尾=%s",
                    category, page, len(stats), len(new_keys), len(merged) + len(new_keys),
                    names[0] if names else None, names[-1] if names else None)
        merged.update(stats)
        if not new_keys:
            break   # 新しい行が無くなったら終わり
    return merged
