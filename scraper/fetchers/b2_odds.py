"""
オッズ取得（netkeiba 内部AJAX API）

出典：実サンプル（府中牝馬S 2026オッズページのXHR）を解析して確定。

--- 重要な発見 ---
出馬表ページ（Bページ）の単勝オッズ・人気セルは空の<span>で、JS/AJAXが後から描画する。
静的HTML取得（requests + BeautifulSoup）では取れない。
そこで、オッズページが内部で叩いている下記APIを直接叩く：

  URL: https://race.netkeiba.com/api/api_get_jra_odds.html
  クエリ:
    pid=api_get_jra_odds
    race_id={12桁のnetkeibaレースID}
    type=1             （券種。1=単勝。netkeiba自身のJSの既定値がこれ。
                        2026-09-19 の本番実行では type=all が空を返した）
    action=init
    sort=odds          （netkeiba自身のJSが常に送っている。省くと空が返るとみられる）
    output=jsonp または json
    compress=1         （レスポンスのdataをbase64+zlib圧縮する）
  必須ヘッダー（推定）:
    x-requested-with: XMLHttpRequest
    referer: https://race.netkeiba.com/odds/index.html?race_id={race_id}

--- レスポンス形式 ---
output=jsonp の場合： jQueryXXX({...})  ← 外側の関数呼び出しを剥がす必要あり
  → output=json を指定すれば生JSONで返るはず（jsonp剥がし不要。実装ではこちらを優先）
compress=1 の場合： {"status":"result","data":"<base64(zlib(本体JSON))>", ...}
  → data を base64デコード → zlib解凍 → 本体JSON

--- パラメータの出典（2026-09-19 追記）---
`jquery.odds_update.js` の `_getOdds()` が実際に投げている値に合わせてある。
初回の本番実行で単勝が1件も取れず、そのJSを読み直して type / sort の食い違いが判明した。
JSをこの目で読んだうえでの値なので、推測ではない。

--- 本体JSON構造 ---
{
  "official_datetime": "2026-06-21 15:54:02",   # オッズ確定時刻（odds_updated_at に使える）
  "odds": {
    "1": { 馬番(2桁ゼロ埋めstr): [単勝オッズ(str), 0, 人気(int)], ... },   # 単勝
    "2": { 馬番(2桁ゼロ埋めstr): [複勝下限(str), 複勝上限(str), 人気(int)], ... },  # 複勝
    "3": 枠連(組番4桁), "4": 馬連(組番4桁), "5": ワイド(組番4桁),
    "6": 馬単(組番4桁), "7": 3連複(組番6桁), "8": 3連単(組番6桁)
  }
}
★ **馬番は配列の中ではなくキー側**。2026-09-19 の本番実行の実レスポンスで確認した
（例 `"1": {"01": ["40.5", 0, 12], ...}` ＝ 1番の単勝40.5倍・12人気。
保存した出馬表ページの表示「40.5 (12人気)」と一致）。
以前は4要素 [オッズ, "0.0", 人気, 馬番] だと想定して `arr[3]` を馬番として読んでおり、
IndexError で全件スキップ＝**オッズが全馬null**になっていた。
組番は2桁ゼロ埋め馬番の連結（例 ワイド "0608" = 6番-8番）。

MVPで使うのは type=1(単勝) と type=2(複勝)。
将来の combo_odds 予約枠（取得項目仕様§2.4）には type=4/5/7（馬連/ワイド/3連複）がそのまま入る。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import zlib
from typing import Any

from scraper.common.http import get as http_get

logger = logging.getLogger("scraper.b2_odds")

ODDS_API_URL = "https://race.netkeiba.com/api/api_get_jra_odds.html"
ODDS_PAGE_URL_TMPL = "https://race.netkeiba.com/odds/index.html?race_id={race_id}"

# 券種コード → 意味（本体JSONの odds キー）
ODDS_TYPE_TAN = "1"
ODDS_TYPE_FUKU = "2"
ODDS_TYPE_UMAREN = "4"
ODDS_TYPE_WIDE = "5"
ODDS_TYPE_SANRENPUKU = "7"


def _unwrap_jsonp(text: str) -> str:
    """jsonp（callback({...})）なら外側を剥がす。生JSONならそのまま返す。"""
    text = text.strip()
    if text.startswith("{"):
        return text
    m = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def _decode_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    {"status":..., "data": <str>, ...} の data を本体JSONに復元する。
    compress=1 のときは data が base64(zlib(json))。無圧縮ならそのままJSON文字列のこともある。
    """
    data_field = envelope.get("data")
    if isinstance(data_field, dict):
        return data_field  # 無圧縮でそのままオブジェクトの場合
    if not isinstance(data_field, str):
        raise ValueError("オッズAPIレスポンスに data がありません")
    # まず base64+zlib を試す
    try:
        decompressed = zlib.decompress(base64.b64decode(data_field))
        return json.loads(decompressed.decode("utf-8"))
    except Exception:
        # 圧縮でなければ素のJSON文字列として解釈
        return json.loads(data_field)


def parse_odds_response(text: str) -> dict[str, Any]:
    """
    APIレスポンス文字列（jsonp or json）から本体JSON（official_datetime, odds）を取り出す。
    テスト・オフライン解析用に公開。
    """
    inner = _unwrap_jsonp(text)
    envelope = json.loads(inner)
    if envelope.get("status") != "result":
        # status が result 以外でも、data があれば解こうと試みる。無ければ空扱い
        if "data" not in envelope:
            return {"official_datetime": None, "odds": {}}
    return _decode_payload(envelope)


def extract_win_place_odds(body: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """
    本体JSONから馬番→{win_odds, place_odds, popularity} のマップを作る。
    win_odds: float|None, place_odds: [下限, 上限]|None, popularity: int|None
    """
    result: dict[int, dict[str, Any]] = {}
    odds = body.get("odds", {})

    def _to_float(s: Any) -> float | None:
        try:
            v = float(s)
            return v if v > 0 else None
        except (ValueError, TypeError):
            return None

    def _num_of(key: Any, arr: Any) -> int | None:
        """馬番は**キー側**（"01" のような2桁ゼロ埋め）。古い形式のため配列末尾も一応見る。"""
        try:
            return int(key)
        except (ValueError, TypeError):
            pass
        try:
            return int(arr[3])
        except (ValueError, TypeError, IndexError):
            return None

    # 単勝：{"01": ["40.5", 0, 12], ...} ＝ {馬番: [単勝オッズ, 0, 人気]}
    # 取消馬など変則エントリ1件で全体を落とさない（マナー設計「失敗はnullで続行」の原則）
    for key, arr in odds.get(ODDS_TYPE_TAN, {}).items():
        num = _num_of(key, arr)
        if num is None or not isinstance(arr, (list, tuple)) or not arr:
            continue
        result.setdefault(num, {})
        result[num]["win_odds"] = _to_float(arr[0])
        result[num]["popularity"] = int(arr[2]) if len(arr) > 2 and str(arr[2]).isdigit() else None

    # 複勝：{馬番: [下限, 上限, 人気]}。単勝と同じ形で2要素目が 0 のこともあるため、
    # 下限・上限の**両方が正の数として読めたときだけ**採用する
    for key, arr in odds.get(ODDS_TYPE_FUKU, {}).items():
        num = _num_of(key, arr)
        if num is None or not isinstance(arr, (list, tuple)) or len(arr) < 2:
            continue
        result.setdefault(num, {})
        low, high = _to_float(arr[0]), _to_float(arr[1])
        result[num]["place_odds"] = [low, high] if (low is not None and high is not None) else None

    return result


def fetch_odds(race_source_ref: str) -> dict[str, Any]:
    """
    オッズAPIを叩いて {official_datetime, by_num} を返す。
    by_num は 馬番 -> {win_odds, place_odds, popularity} のマップ。

    race_source_ref: netkeibaのレースID（12桁、例 "202605030611"）
    """
    # ★ パラメータは netkeiba 自身の `jquery.odds_update.js`（`_getOdds()`）に合わせてある。
    #   2026-09-19 の本番実行で単勝オッズが1件も取れず、そのJSを読み直して判明した差分：
    #     type : "all" ではなく **"1"**（＝単勝。JSの既定 oddsType は 1）
    #     sort : JSは常に送っている（既定 "odds"）。こちらは送っていなかった
    #   compress=1 は「data が base64(zlib(JSON))」の意味で、_decode_payload が復元する。
    params = {
        "pid": "api_get_jra_odds",
        "race_id": race_source_ref,
        "type": ODDS_TYPE_TAN,
        "action": "init",
        "sort": "odds",
        "output": "json",   # jsonp剥がしを避けるため生JSONを要求
        "compress": "1",
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": ODDS_PAGE_URL_TMPL.format(race_id=race_source_ref),
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    resp = http_get(ODDS_API_URL, params=params, headers=headers)
    if resp is None:
        raise RuntimeError(f"オッズAPIの取得に失敗しました: race_id={race_source_ref}")

    body = parse_odds_response(resp.text)
    by_num = extract_win_place_odds(body)
    if not any(v.get("win_odds") is not None for v in by_num.values()):
        # 応答はあるのに単勝が1件も取れない＝パラメータかレスポンス構造の食い違い。
        # 中身が分からないまま「オッズ全馬null」で通ってしまうのを防ぐため、手がかりを必ず残す
        # （2026-09-19 の本番実行で実際にこれが起きた）。
        odds = body.get("odds")
        tan = odds.get(ODDS_TYPE_TAN) if isinstance(odds, dict) else None
        if isinstance(tan, dict):
            sample = list(tan.items())[:3]
        elif isinstance(tan, list):
            sample = tan[:3]
        else:
            sample = tan
        logger.warning(
            "単勝オッズが1件も取れませんでした race_id=%s / official_datetime=%r / "
            "oddsのキー=%s / odds['1'] の型=%s・件数=%s / 中身の先頭3件=%r",
            race_source_ref, body.get("official_datetime"),
            sorted(odds)[:8] if isinstance(odds, dict) else type(odds).__name__,
            type(tan).__name__, len(tan) if hasattr(tan, "__len__") else None, sample,
        )
    return {
        "official_datetime": body.get("official_datetime"),
        "by_num": by_num,
    }


def merge_odds_into_race(race: dict[str, Any], odds_result: dict[str, Any]) -> None:
    """
    fetch_odds の結果を、b_shutuba が作った race dict の entries に反映する。
    win_odds が空だった出馬表を、このオッズAPIの値で埋める。
    """
    by_num = odds_result.get("by_num", {})
    for entry in race.get("entries", []):
        num = entry.get("num")
        if num in by_num:
            od = by_num[num]
            if od.get("win_odds") is not None:
                entry["win_odds"] = od["win_odds"]
            if od.get("place_odds") is not None:
                entry["place_odds"] = od["place_odds"]
            if od.get("popularity") is not None:
                entry["popularity"] = od["popularity"]
    # オッズ確定時刻を記録（取得項目仕様§2.3 odds_updated_at）
    if odds_result.get("official_datetime"):
        race["odds_updated_at"] = odds_result["official_datetime"]
