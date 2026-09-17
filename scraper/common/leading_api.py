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
**レスポンスはJSONではなくHTML断片**（jsonpでHTML文字列を包んで返す形）。
`output=json` にすれば生で返る可能性が高い（B2オッズAPIと同じ挙動。v4 §2.3）ため、
実装では json を先に試し、jsonp で包まれていた場合は剥がしてから返す。

ページングは `page=1,2,3…`（「もっと見る」ボタンが page を増やして再取得する）。
`limit` を大きくすれば1リクエストで全件取れる可能性があるので、既定を大きめにして
リクエスト数を抑える方針にしている（マナー設計§1.1）。

--- ⚠ 場別リーディングは存在しない（OPEN_QUESTIONS B-3）---

絞り込みは **全国 / 関東 / 関西**（`bel`）＝**所属（美浦・栗東）**であって開催場ではない。
年と並び順以外の切り口は無く、PC版 `db.netkeiba.com/?pid=jockey_leading` も実体が無い。
取得項目仕様§2.6 が定めていた騎手の `scope="venue"`（当該場成績）は、この方式では取れない。
→ 騎手も `scope="overall"` で取得する（仕様§2.6 を改訂済み）。場別成績が欲しくなったら、
  騎手個別ページ（出走騎手ぶんだけ）を将来の拡張候補として検討する。

--- 未実装 ---

`parse_leading_html()` だけが未実装。**APIレスポンスの実サンプルが1件あれば書ける**
（行の構造・着度数がどのタグに入るかが分からないと、推測でパーサーを書くことになるため）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

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

DEFAULT_LIMIT = 100   # 既定の20だとページングが増えるので大きめにする（要確認：上限は未検証）

# パーサー（parse_leading_html）が実装されたら True にする。
# False の間は fetch_leading() が**通信する前に**止まる（無駄なリクエストを投げないため）
PARSER_IMPLEMENTED = False


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


def unwrap_jsonp(text: str) -> str:
    """jsonp（callback({...}) や callback("<html>")）なら外側を剥がす。生ならそのまま返す。"""
    text = text.strip()
    if not text or text[0] in "{[<":
        return text
    m = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", text, re.DOTALL)
    return m.group(1) if m else text


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
    return unwrap_jsonp(resp.text)


def parse_leading_html(html: str, category: str, year: str, *,
                       bel: str = BEL_ALL) -> dict[str, dict[str, Any]]:
    """
    リーディング表のHTML断片を {ref: stats} に変換する（取得項目仕様§2.6）。

    stats = {"scope": "overall", "period": year,
             "starts": int, "wins": int, "seconds": int, "thirds": int}

    ref は netkeiba の騎手ID／調教師IDで、**B出馬表で取得済みの jockey_ref / trainer_ref と
    同じ桁・同じ形式であることを実装時に必ず確認する**（引き継ぎ書v7 §2.5）。
    ここが食い違うと出走馬への振り分けが丸ごと壊れる。
    """
    raise NotImplementedError(
        "APIレスポンスの実サンプル待ち。下記URLをブラウザで開いて保存したものを渡せば実装できる:\n"
        "  https://db.sp.netkeiba.com/?pid=api_get_leading&input=UTF-8&output=json"
        "&limit=100&category=jockey&page=1&show=leading_detail&bel=&year=2026&sort_key=win"
    )


def fetch_leading(category: str, year: str, *, bel: str = BEL_ALL,
                  sort_key: str = SORT_WIN, limit: int = DEFAULT_LIMIT,
                  max_pages: int = 5) -> dict[str, dict[str, Any]]:
    """
    全件（ページングを追い切るまで）取得して {ref: stats} にまとめる。

    max_pages: 保険の上限。1ページ目で全件返ってくれば2ページ目は叩かない
               （同じ内容が返る／空が返る時点で打ち切る）。
    """
    if not PARSER_IMPLEMENTED:
        # パーサーが無いのにリクエストを投げても無駄打ちになるだけなので、通信の前に止める
        # （マナー設計§1.1：不要なアクセスをしない）
        raise NotImplementedError(
            "リーディング表のパーサーが未実装です。APIレスポンスの実サンプルが1件あれば実装できます。"
            "詳細は scraper/common/leading_api.parse_leading_html を参照。"
        )

    merged: dict[str, dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        html = fetch_leading_html(category, year, bel=bel, sort_key=sort_key,
                                  limit=limit, page=page)
        stats = parse_leading_html(html, category, year, bel=bel)
        new_keys = set(stats) - set(merged)
        merged.update(stats)
        if not new_keys:
            break   # 新しい行が無くなったら終わり
        logger.info("%s リーディング %dページ目: %d件（累計 %d件）",
                    category, page, len(stats), len(merged))
    return merged
