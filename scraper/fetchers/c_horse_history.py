"""
Cページ：馬の戦績ページ（db.netkeiba.com/horse/）

出典：取得項目_共通内部フォーマット仕様_v1.1.md §1.1（表の# C行）／§2.5（past_runs[]の型）
取得タイミング：当日朝（キャッシュ可） / 1週あたり約70〜110ページ（頭数×レース数）
取れるもの：過去走の全履歴（本アプリは直近5走を採用）

**同一週内キャッシュ必須**（マナー設計§1.1）：土曜に取った馬は日曜再取得しない。
→ キャッシュキーは horse_ref（netkeiba馬ID）、キャッシュ有効期間は週単位（week_id）。
このモジュール自体はキャッシュを持たない（b_shutuba.py等と同じく「1頭を取得する」責務のみ）。
週内キャッシュは build_raw.py 側（オーケストレーター）が horse_ref をキーに持つ（実装済み）。

--- 実サンプル解析で分かったこと（ヴァルキリーバース、1頭分・全6走で確認済み）---

戦績テーブルは `table.db_h_race_results`（thead/tbodyあり）。tbody内の各trが1走。
列インデックス（0始まり）：
  0=日付 1=開催 4=レース名 6=頭数 11=着順 12=騎手 13=斤量 14=距離(surface+dist) 16=馬場 18=タイム 19=着差 27=上り3F

- 開催列（例"3東京6"）：先頭の回数・末尾の開催日数はどちらも数字で、中央の場名部分だけを
  正規表現で抜き出す（地方・海外の場名もこの方式でそのまま文字列として取れる。§2.5の設計どおり
  「地方・海外はそのまま文字列」を実現）
- 馬場状態は単一文字の略記（"稍"/"不"）で来る。既存 constants.GOING_NORMALIZE がこれをカバー済み
- レース名列にクラス情報が同居している：
  括弧内グレード表記 "(GIII)"/"(GII)"/"(GI)"/"(L)" と、括弧内条件表記 "(1勝クラス)" 等、
  括弧なし表記 "3歳未勝利"/"2歳新馬" の3パターンが混在する。
  → constants.normalize_class_label が GRADE_TAG_MAP（括弧・空白で区切った全トークン）と
    CONDITION_GRADE_MAP（条件文字列）をこの順で試す（他の取得経路と共有・二重定義しない）。
    "天皇賞(春)(GI)" のように括弧が2つ以上あっても、後ろの "(GI)" まで見る
  → 【要確認】"新馬"（新馬戦）はclass_offsetに専用キーが無いため、"未勝利"と同じ mi に丸めている。
    新馬戦は本来「同条件での実績が皆無」という点で未勝利ともニュアンスが違うが、v1では割り切り。
  → 【既知の限界】グレード表記も条件表記も括弧内に見つからない場合（無印の特別戦など）は
    class=None（欠損）。取得項目仕様の「取れなかったらnull」の原則どおり
- 走破タイムは "M:SS.S"（例 "1:48.0"）または "SS.S"（短距離走で分未満）の2パターン
- 着差列（着差）は「勝ち馬とのタイム差」の生値だが、勝ち馬自身の行でも 0.0 や 負値 など
  表記が一定しない（netkeiba側の仕様）。共通内部フォーマット仕様§2.5の定義
  「自身が1着なら0」に合わせ、finish==1 のときは常に margin_sec=0 に上書きする
- 「大差」等タイム差換算不能な表記は margin_sec=null（仕様§2.1どおり）
- 着順列が数字でない（中止・除外・取消等）場合は finish=None とし、その生テキストを note に格納
- surfaceには実データ上「障」（障害）も出現しうる。取得項目仕様§2.1の正規化表は芝/ダのみ列挙だが、
  これはフェッチャー層が「生の事実を判断せず記録する」責務（§2.0原則1）に従い障もそのまま記録する。
  スピード指数仕様§2の usable判定（surfaceが芝/ダのみ）で自然に除外されるため、フェッチャー側で
  弾く必要はない
"""
from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from scraper.common import constants
from scraper.common.http import get as http_get

logger = logging.getLogger("scraper.c_horse_history")

HORSE_URL_TMPL = "https://db.netkeiba.com/horse/{horse_id}"

# 開催列（例 "3東京6"）から場名部分だけを抜き出す：先頭の回数・末尾の開催日数（ともに数字）を除いた中央部分
_VENUE_IN_KAISAI_RE = re.compile(r"^\d*(\D+?)\d*$")

_DIST_RE = re.compile(r"(芝|ダ|障)(\d+)")
_BODY_WEIGHT_RE = re.compile(r"^(\d{3,4})\(\s*([+\-]?\d+)\s*\)$")


def _time_to_sec(text: str) -> float | None:
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


def _to_float_or_none(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None  # "大差" 等の換算不能表記（取得項目仕様§2.1）


def _parse_venue(kaisai_text: str) -> str:
    """開催列（例 "3東京6"）から場名だけを抜く。地方・海外もそのまま文字列として返す（§2.5）。"""
    m = _VENUE_IN_KAISAI_RE.match(kaisai_text.strip())
    return m.group(1) if m else kaisai_text.strip()


def _parse_class(racename: str) -> str | None:
    """
    レース名列からクラス表記を正規化する（constants.normalize_class_label に委ねる）。
    優先順：①括弧・空白で区切ったグレード表記（GI/GII/GIII/L/OP） ②条件文字列（未勝利/1勝クラス等）
    どちらにも当てはまらない場合は None（取得項目仕様§2.0「取れなかったらnull」）。

    以前は最初の括弧だけを見ていたため、"天皇賞(春)(GI)" / "天皇賞(秋)(GI)" では "春"/"秋" を拾って
    後ろの "(GI)" を見ず、class=None になっていた。出馬表の過去5走（c2_shutuba_past）・基準タイム表の
    取得（scripts/build_base_times）と同じ正規化に揃える。
    """
    return constants.normalize_class_label(racename)


def _parse_finish(text: str) -> tuple[int | None, str | None]:
    """着順テキストを (finish, note) に分解。中止・除外・取消等は finish=None・生テキストをnoteへ。"""
    text = text.strip()
    if text.isdigit():
        return int(text), None
    if not text:
        return None, None
    return None, text  # "中止"/"除外"/"取消"/"失格" 等


def _parse_run_row(tds: list) -> dict[str, Any]:
    date_text = tds[0].get_text(strip=True)  # "2026/06/21"
    date_str = date_text.replace("/", "-") if date_text else None

    venue = _parse_venue(tds[1].get_text(strip=True)) if len(tds) > 1 else None

    racename = tds[4].get_text(strip=True) if len(tds) > 4 else ""
    klass = _parse_class(racename)

    heads_text = tds[6].get_text(strip=True) if len(tds) > 6 else ""
    heads = int(heads_text) if heads_text.isdigit() else None

    finish_text = tds[11].get_text(strip=True) if len(tds) > 11 else ""
    finish, finish_note = _parse_finish(finish_text)

    jockey_name = tds[12].get_text(strip=True) if len(tds) > 12 else None
    jockey_name = jockey_name or None

    impost = _to_float_or_none(tds[13].get_text(strip=True)) if len(tds) > 13 else None

    dist_text = tds[14].get_text(strip=True) if len(tds) > 14 else ""
    dist_m = _DIST_RE.match(dist_text)
    surface = dist_m.group(1) if dist_m else None
    dist = int(dist_m.group(2)) if dist_m else None

    going_raw = tds[16].get_text(strip=True) if len(tds) > 16 else ""
    going = constants.GOING_NORMALIZE.get(going_raw, going_raw) if going_raw else None

    time_sec = _time_to_sec(tds[18].get_text(strip=True)) if len(tds) > 18 else None

    margin_text = tds[19].get_text(strip=True) if len(tds) > 19 else ""
    if finish == 1:
        margin_sec = 0.0  # 共通内部フォーマット仕様§2.5：自身が1着なら0（netkeiba生値の表記ゆれを上書き）
    else:
        margin_sec = _to_float_or_none(margin_text)

    last3f = _to_float_or_none(tds[27].get_text(strip=True)) if len(tds) > 27 else None

    # netkeiba の列構成変更に強くするため固定indexではなく、行内の
    # "510(0)" / "484(-4)" 形式を完全一致で探す。斤量やオッズとは形が違うため誤認しにくい。
    body_weight = None
    for td in tds:
        weight_m = _BODY_WEIGHT_RE.match(td.get_text(strip=True))
        if weight_m:
            body_weight = {
                "value": int(weight_m.group(1)),
                "diff": int(weight_m.group(2)),
            }
            break

    return {
        "date": date_str,
        "venue": venue,
        "surface": surface,
        "dist": dist,
        "going": going,
        "class": klass,
        "heads": heads,
        "finish": finish,
        "time_sec": time_sec,
        "last3f": last3f,
        "margin_sec": margin_sec,
        "impost": impost,
        "jockey_name": jockey_name,
        "body_weight": body_weight,
        "note": finish_note,
    }


def parse_horse_history_html(html: str, n_runs: int = 5) -> list[dict[str, Any]]:
    """HTML文字列から past_runs[] を組み立てる（テスト・オフライン解析用に公開）。新しい順。"""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.db_h_race_results")
    if table is None:
        # 200が返っているのに表が無い＝ページ構成の変更か、ボット判定ページ。
        # 黙って空を返すと「全馬 past_runs=[]」のまま静かにパイプラインが通ってしまうので、
        # 何が返ってきたのかを必ず残す（2026-09-19 の初回本番実行で実際にこれが起きた）。
        title = soup.title.get_text(strip=True) if soup.title else "(titleなし)"
        tables = [t.get("class") for t in soup.find_all("table")][:5]
        logger.warning(
            "戦績テーブル table.db_h_race_results が見つかりません。"
            "title=%r / 本文%dバイト / 先頭のtable class=%s",
            title, len(html), tables,
        )
        return []
    rows = table.select("tbody tr")
    runs = []
    for row in rows[:n_runs]:
        tds = row.find_all("td")
        if len(tds) < 20:
            continue  # 想定外の行構造（広告行等）はスキップして続行
        runs.append(_parse_run_row(tds))
    return runs


def fetch_horse_history(horse_ref: str, n_runs: int = 5) -> list[dict[str, Any]]:
    """
    馬の直近n走を取得する。

    horse_ref: netkeibaの馬ID
    n_runs: 取得する過去走数（取得項目仕様§4-1で確定：5）
    戻り値: past_runs[] のリスト（取得項目仕様§2.5準拠、新しい順）
    """
    url = HORSE_URL_TMPL.format(horse_id=horse_ref)
    resp = http_get(url)
    if resp is None:
        raise RuntimeError(f"馬戦績ページの取得に失敗しました: {url}")
    return parse_horse_history_html(resp.text, n_runs=n_runs)


def fetch_horse_history_cached(
    horse_ref: str, cache: dict[str, list[dict[str, Any]]], n_runs: int = 5
) -> list[dict[str, Any]]:
    """
    同一週内キャッシュ対応版（マナー設計§1.1：土曜に取った馬は日曜再取得しない）。

    cache: 呼び出し側（build_raw.py）が週単位で保持する {horse_ref: past_runs} の辞書。
           build_week() の1回の実行内で複数レースに同じ馬が登場するケースをこれで吸収する。

    実行をまたいだ永続化（土曜・日曜を別々のActions実行で回す運用・引き継ぎ書v6 §1）は、
    `build_raw.load_week_cache()` が **既にコミットされている raw/{week_id}.json から
    past_runs を読み直して**この辞書に詰めることで実現している（新しいキャッシュファイルは作らない）。
    """
    if horse_ref in cache:
        return cache[horse_ref]
    runs = fetch_horse_history(horse_ref, n_runs=n_runs)
    cache[horse_ref] = runs
    return runs


# --- 研究用（PR-B history inventory）。本番の past_runs の形・取得経路は変えない -------------

def parse_horse_history_html_with_race_names(html: str, n_runs: int | None = None) -> dict[str, Any]:
    """研究用：戦績表の有無と、レース名つきの過去走を返す。

    本番の parse_horse_history_html の戻り値（past_runs[]）は変えずに、
    研究 artifact だけで使う race_name を同じ行から足す。
    戻り値: {"table_present": bool, "source_total_rows": int, "runs": [{...past_run, "race_name": str|None}]}
    source_total_rows は戦績表の有効な全行数。n_runs=None（既定）なら全行を返す。
    研究側は「全行 → 日付の cutoff → long_window 走に切る」の順で使う（先に切ると、レース後の取得で
    先頭に入る当日の行のぶん、過去走が1走少なくなる）。
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.db_h_race_results")
    if table is None:
        return {"table_present": False, "source_total_rows": 0, "runs": []}
    rows = [row for row in table.select("tbody tr") if len(row.find_all("td")) >= 20]
    runs = []
    for row in (rows if n_runs is None else rows[:n_runs]):
        tds = row.find_all("td")
        run = _parse_run_row(tds)
        run["race_name"] = tds[4].get_text(strip=True) or None
        runs.append(run)
    return {"table_present": True, "source_total_rows": len(rows), "runs": runs}


def fetch_horse_history_for_research(horse_ref: str, n_runs: int | None = None) -> dict[str, Any]:
    """研究用の長期履歴取得。失敗しても例外にせず status で返す（推測で埋めない）。

    status: ok | fetch_failed（HTTP失敗）| blocked（200でも戦績表が無い＝bot判定ページ等）
    """
    url = HORSE_URL_TMPL.format(horse_id=horse_ref)
    resp = http_get(url)
    if resp is None:
        return {"status": "fetch_failed", "runs": [], "url": url}
    parsed = parse_horse_history_html_with_race_names(resp.text, n_runs=n_runs)
    if not parsed["table_present"]:
        # 「bot 判定のページ」なのか「ページの作りが変わって戦績表が別の読み込みになった」のかを
        # 後から見分けられるよう、返ってきたページの手がかりだけを残す（ページ本体は保存しない）
        return {"status": "blocked", "runs": [], "url": url,
                "diagnostics": describe_page_without_results_table(resp)}
    return {"status": "ok", "runs": parsed["runs"], "url": url,
            "source_total_rows": parsed["source_total_rows"]}


_PAGE_MARKERS = {
    # bot 判定・アクセス制限らしい文言
    "captcha": re.compile(r"captcha|recaptcha|hcaptcha", re.IGNORECASE),
    "access_denied": re.compile(r"access denied|forbidden|アクセスが制限|不正なアクセス", re.IGNORECASE),
    "cloudflare": re.compile(r"cloudflare|cf-chl|just a moment", re.IGNORECASE),
    # 戦績表の名残（クラス名が残っているのに表が無い＝別の読み込みになった可能性）
    "results_table_class_in_source": re.compile(r"db_h_race_results"),
    "horse_results_word_in_source": re.compile(r"horse_results|race_results|result_box", re.IGNORECASE),
}
_AJAX_URL_RE = re.compile(r"[\w/.-]*ajax[\w/.-]*\.html", re.IGNORECASE)


def describe_page_without_results_table(resp: Any, excerpt_chars: int = 200) -> dict[str, Any]:
    """戦績表が見つからなかったページの手がかり（研究用の診断。予想には使わない）。

    残すのは HTTP の状態・最終URL・大きさ・タイトル・表のクラス・本文の先頭だけ。
    文字コードは <meta charset> をもとに BeautifulSoup に判定させる（netkeiba の db は EUC-JP）。
    """
    raw = getattr(resp, "content", b"") or b""
    soup = BeautifulSoup(raw, "lxml")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    try:
        source = raw.decode(soup.original_encoding or "utf-8", errors="replace")
    except LookupError:
        source = raw.decode("utf-8", errors="replace")
    return {
        "http_status": getattr(resp, "status_code", None),
        "final_url": getattr(resp, "url", None),
        "content_bytes": len(raw),
        "detected_encoding": soup.original_encoding,
        "title": soup.title.get_text(strip=True) if soup.title else None,
        "table_classes": [" ".join(t.get("class") or []) or None for t in soup.find_all("table")][:10],
        "text_excerpt": text[:excerpt_chars],
        "markers": {name: bool(rx.search(source)) for name, rx in _PAGE_MARKERS.items()},
        "ajax_urls": sorted(set(_AJAX_URL_RE.findall(source)))[:10],
    }
