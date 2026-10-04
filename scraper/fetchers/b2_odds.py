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

2026-09-20 の優先改修で type=4/5/7（馬連/ワイド/3連複）も実取得するようにした。
鳳の降臨判定を「鳳が実際に買うカード × 実市場オッズ」のEVへ接続するためで、
type=all は初回本番で空を返した実績があるため券種ごとに個別取得する。

--- 券種別の価格時刻（provenance、2026-10 PR-A）---
券種ごとに別々のHTTP取得をしているので、価格の時刻も券種ごとに別々に残す（`market_meta`）。
以前は単勝レスポンスの official_datetime しか残しておらず、「単勝は14:40、3連複は14:52」の
ような違いが後から区別できなかった。時刻は次の2つを混同しない：
  - source_time … netkeiba が返した official_datetime そのもの（provider returned official_datetime）。
                  価格の鮮度はこちらで判定する。欠けていれば None のまま残す（observed_at で代用しない）。
                  ※ これが本当に「その券種の価格の更新時刻」を意味するかは、まだ完全には確かめていない。
  - observed_at … こちらがHTTP取得を終えた時刻（JST）。「いつ取りに行ったか」の証拠であって、
                  「いつの価格か」の証拠ではない。
取得に失敗した券種も消さずに status（ok / fetch_failed / empty / unparsed）で理由を残す。
予想・買い目・EV の計算は従来どおり by_num / combo_odds だけを使い、market_meta は読まない。
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import re
import zlib
from typing import Any, Callable

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

COMBO_TYPE_NAMES = {
    ODDS_TYPE_UMAREN: "馬連",
    ODDS_TYPE_WIDE: "ワイド",
    ODDS_TYPE_SANRENPUKU: "3連複",
}
COMBO_TYPE_SIZE = {
    ODDS_TYPE_UMAREN: 2,
    ODDS_TYPE_WIDE: 2,
    ODDS_TYPE_SANRENPUKU: 3,
}

# 券種別 provenance（market_meta）のキー。combo_odds の券種名（"馬連" など）との対応も持つ。
MARKET_META_SCHEMA = "odds-market-meta-v1"
MARKET_WIN = "win"
MARKET_UMAREN = "umaren"
MARKET_WIDE = "wide"
MARKET_SANRENPUKU = "sanrenpuku"
MARKET_KEYS = (MARKET_WIN, MARKET_UMAREN, MARKET_WIDE, MARKET_SANRENPUKU)
MARKET_BY_TYPE_CODE = {
    ODDS_TYPE_TAN: MARKET_WIN,
    ODDS_TYPE_UMAREN: MARKET_UMAREN,
    ODDS_TYPE_WIDE: MARKET_WIDE,
    ODDS_TYPE_SANRENPUKU: MARKET_SANRENPUKU,
}
# カードの券種名（logic/cards.py の bet["type"]）→ market_meta のキー
MARKET_BY_BET_TYPE = {
    "馬連": MARKET_UMAREN,
    "ワイド": MARKET_WIDE,
    "3連複": MARKET_SANRENPUKU,
}

STATUS_OK = "ok"
STATUS_FETCH_FAILED = "fetch_failed"   # HTTP失敗・応答を解けなかった
STATUS_EMPTY = "empty"                 # 応答はあったが、その券種の表が無い／空
STATUS_UNPARSED = "unparsed"           # 表はあったが1件も読めなかった
STATUS_NOT_FETCHED = "not_fetched"     # この観測ではその券種を取りに行っていない（直前の単勝だけの観測など）

JST = datetime.timezone(datetime.timedelta(hours=9))


def _system_clock() -> datetime.datetime:
    return datetime.datetime.now(JST)


def _market_record(type_code: str, race_source_ref: str, observed_at: datetime.datetime | None,
                   source_time: Any, status: str, rows: int) -> dict[str, Any]:
    """券種1つぶんの provenance。source_time は provider の official_datetime をそのまま残す。"""
    return {
        "source_time": source_time or None,
        "observed_at": observed_at.isoformat(timespec="seconds") if observed_at else None,
        "source_ref": {"race_id": race_source_ref, "type": type_code},
        "status": status,
        "rows": rows,
    }


def not_fetched_record() -> dict[str, Any]:
    """その観測で取りに行っていない券種。鮮度は証明しない。"""
    return {
        "source_time": None, "observed_at": None, "source_ref": None,
        "status": STATUS_NOT_FETCHED, "rows": 0,
    }


def win_fetch_failed_meta(race_source_ref: str,
                          clock: Callable[[], datetime.datetime] | None = None) -> dict[str, Any]:
    """単勝の取得自体に失敗したレース用。消さずに fetch_failed を残す（式別は取りに行っていない）。"""
    clock = clock or _system_clock
    return market_meta_block({
        MARKET_WIN: _market_record(
            ODDS_TYPE_TAN, race_source_ref, clock(), None, STATUS_FETCH_FAILED, 0
        ),
    })


def market_meta_block(markets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """race["odds_market_meta"] に入れる形。odds_updated_at が何を指すかも明示する。"""
    return {
        "schema": MARKET_META_SCHEMA,
        "source_time_definition": "provider_returned_official_datetime",
        # 後方互換の odds_updated_at は **単勝の** source_time。式別の時刻ではない。
        "odds_updated_at_is": "win.source_time",
        "markets": {key: markets.get(key) or not_fetched_record() for key in MARKET_KEYS},
    }


def _table_status(body: dict[str, Any], type_code: str, parsed_rows: int) -> str:
    if parsed_rows > 0:
        return STATUS_OK
    odds = body.get("odds")
    table = odds.get(type_code) if isinstance(odds, dict) else None
    return STATUS_UNPARSED if table else STATUS_EMPTY


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


def _combo_key(raw_key: Any, size: int) -> str | None:
    """netkeibaの組番キー（例 "0209", "020910"）を "2-9" / "2-9-10" に正規化する。"""
    digits = re.sub(r"\D", "", str(raw_key))
    if len(digits) != size * 2:
        return None
    nums = []
    try:
        for i in range(size):
            num = int(digits[i * 2:(i + 1) * 2])
            if num <= 0:
                return None
            nums.append(num)
    except ValueError:
        return None
    return "-".join(str(n) for n in sorted(nums))


def _positive_float(value: Any) -> float | None:
    try:
        v = float(value)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def extract_combo_odds(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """
    馬連・ワイド・3連複の実オッズを共通形にする。

    戻り値:
      {
        "馬連": {"2-9": 269.9},
        "ワイド": {"2-9": [60.0, 66.9]},  # 幅がある券種は [下限, 上限]
        "3連複": {"2-9-10": 2252.8}
      }

    鳳のEV判定ではワイドは下限を使う（過大評価を避けるため保守的）。
    """
    out: dict[str, dict[str, Any]] = {}
    odds = body.get("odds", {})
    if not isinstance(odds, dict):
        return out

    for type_code, type_name in COMBO_TYPE_NAMES.items():
        table = odds.get(type_code)
        if not isinstance(table, dict):
            continue
        size = COMBO_TYPE_SIZE[type_code]
        parsed: dict[str, Any] = {}
        for raw_key, arr in table.items():
            if not isinstance(arr, (list, tuple)) or not arr:
                continue
            key = _combo_key(raw_key, size)
            if key is None:
                continue
            if type_code == ODDS_TYPE_WIDE:
                low = _positive_float(arr[0])
                high = _positive_float(arr[1]) if len(arr) > 1 else low
                if low is None:
                    continue
                if high is None:
                    high = low
                parsed[key] = [min(low, high), max(low, high)]
            else:
                value = _positive_float(arr[0])
                if value is not None:
                    parsed[key] = value
        if parsed:
            out[type_name] = parsed
    return out


def _fetch_type_body(race_source_ref: str, odds_type: str) -> dict[str, Any]:
    """指定券種1つをnetkeibaのオッズAPIから取得する。"""
    params = {
        "pid": "api_get_jra_odds",
        "race_id": race_source_ref,
        "type": odds_type,
        "action": "init",
        "sort": "odds",
        "output": "json",
        "compress": "1",
    }
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": ODDS_PAGE_URL_TMPL.format(race_id=race_source_ref),
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    resp = http_get(ODDS_API_URL, params=params, headers=headers)
    if resp is None:
        raise RuntimeError(
            f"オッズAPIの取得に失敗しました: race_id={race_source_ref} type={odds_type}"
        )
    return parse_odds_response(resp.text)



def _win_rows(by_num: dict[int, dict[str, Any]]) -> int:
    return sum(1 for v in by_num.values() if v.get("win_odds") is not None)


def fetch_win_odds(race_source_ref: str,
                   clock: Callable[[], datetime.datetime] | None = None) -> dict[str, Any]:
    """単勝だけを軽量取得する。

    直前オッズ観測用。馬連/ワイド/3連複まで取り直す通常の fetch_odds と違い、
    API 1回だけにして、発走前の市場分布を時系列で保存する用途に限定する。
    この取得が証明するのは**単勝の鮮度だけ**（market_scope="win"）。式別は取りに行かない。
    """
    clock = clock or _system_clock
    body = _fetch_type_body(race_source_ref, ODDS_TYPE_TAN)
    observed_at = clock()
    by_num = extract_win_place_odds(body)
    rows = _win_rows(by_num)
    if not rows:
        logger.warning("直前単勝オッズが1件も取れませんでした race_id=%s", race_source_ref)
    return {
        "official_datetime": body.get("official_datetime"),
        "by_num": by_num,
        "market_scope": MARKET_WIN,
        "market_meta": {
            MARKET_WIN: _market_record(
                ODDS_TYPE_TAN, race_source_ref, observed_at, body.get("official_datetime"),
                _table_status(body, ODDS_TYPE_TAN, rows), rows,
            ),
        },
    }

def fetch_odds(race_source_ref: str,
               clock: Callable[[], datetime.datetime] | None = None) -> dict[str, Any]:
    """
    単勝に加え、実際に購入する馬連・ワイド・3連複の市場オッズも取得する。

    type=all は2026-09-19の本番で空を返したため、券種ごとに分けて取得する。
    組み合わせオッズの取得失敗は単勝予想まで巻き込まず、combo_oddsを欠損として続行する。
    券種ごとの取得時刻・元データ時刻・状態は market_meta に別々に残す（予想には使わない）。
    """
    clock = clock or _system_clock
    body = _fetch_type_body(race_source_ref, ODDS_TYPE_TAN)
    win_observed_at = clock()
    by_num = extract_win_place_odds(body)
    win_rows = _win_rows(by_num)
    market_meta: dict[str, dict[str, Any]] = {
        MARKET_WIN: _market_record(
            ODDS_TYPE_TAN, race_source_ref, win_observed_at, body.get("official_datetime"),
            _table_status(body, ODDS_TYPE_TAN, win_rows), win_rows,
        ),
    }

    if not any(v.get("win_odds") is not None for v in by_num.values()):
        odds = body.get("odds")
        tan = odds.get(ODDS_TYPE_TAN) if isinstance(odds, dict) else None
        sample = list(tan.items())[:3] if isinstance(tan, dict) else tan
        logger.warning(
            "単勝オッズが1件も取れませんでした race_id=%s / official_datetime=%r / "
            "oddsのキー=%s / odds['1'] の型=%s・件数=%s / 中身の先頭3件=%r",
            race_source_ref, body.get("official_datetime"),
            sorted(odds)[:8] if isinstance(odds, dict) else type(odds).__name__,
            type(tan).__name__, len(tan) if hasattr(tan, "__len__") else None, sample,
        )

    combo_odds: dict[str, dict[str, Any]] = {}
    for type_code in (ODDS_TYPE_UMAREN, ODDS_TYPE_WIDE, ODDS_TYPE_SANRENPUKU):
        market = MARKET_BY_TYPE_CODE[type_code]
        try:
            combo_body = _fetch_type_body(race_source_ref, type_code)
        except (RuntimeError, ValueError, json.JSONDecodeError):
            logger.warning(
                "式別オッズを取得できませんでした race_id=%s type=%s。"
                "この券種は鳳の市場EV判定から欠損扱いにします",
                race_source_ref, type_code, exc_info=True,
            )
            market_meta[market] = _market_record(
                type_code, race_source_ref, clock(), None, STATUS_FETCH_FAILED, 0
            )
            continue
        observed_at = clock()
        parsed = extract_combo_odds(combo_body)
        for type_name, table in parsed.items():
            combo_odds.setdefault(type_name, {}).update(table)
        rows = len(parsed.get(COMBO_TYPE_NAMES[type_code]) or {})
        if not parsed:
            _warn_unparsed_combo(race_source_ref, type_code, combo_body)
        market_meta[market] = _market_record(
            type_code, race_source_ref, observed_at, combo_body.get("official_datetime"),
            _table_status(combo_body, type_code, rows), rows,
        )

    return {
        # 単勝の観測時刻をそのまま使う。p・q（＝予想の土台）はこの時点の単勝オッズで作るので、
        # 式別の取得時刻を混ぜて max を取ると odds_updated_at が実態より後ろにずれる。
        # 式別の時刻は market_meta に券種ごとに残す。
        "official_datetime": body.get("official_datetime"),
        "by_num": by_num,
        "combo_odds": combo_odds,
        "market_scope": "all",
        "market_meta": market_meta,
    }


def _warn_unparsed_combo(race_source_ref: str, type_code: str, body: dict[str, Any]) -> None:
    """
    応答はあったのに1組も読めなかったときに、**レスポンスの形を必ずログに残す**。

    type=4/5/7 の中身の形（組番キーの書式・配列のどこが倍率か）は、単勝と違って
    まだ実レスポンスで確認できていない（このリポジトリのネットワークからnetkeibaに
    到達できないため）。読めなかったときに黙って空を返すと、次も同じ推測のまま直せない。
    2026-09-19 の単勝の取りこぼしを解けたのは、この形の警告が手がかりを残していたから。
    """
    odds = body.get("odds")
    table = odds.get(type_code) if isinstance(odds, dict) else None
    if isinstance(table, dict):
        sample = list(table.items())[:3]
    elif isinstance(table, (list, tuple)):
        sample = list(table[:3])
    else:
        sample = table
    logger.warning(
        "式別オッズを1組も読めませんでした race_id=%s type=%s（%s）/ "
        "oddsのキー=%s / odds[%r] の型=%s・件数=%s / 中身の先頭3件=%r",
        race_source_ref, type_code, COMBO_TYPE_NAMES.get(type_code, "?"),
        sorted(odds)[:8] if isinstance(odds, dict) else type(odds).__name__,
        type_code, type(table).__name__,
        len(table) if hasattr(table, "__len__") else None, sample,
    )


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
    # 実際に買う券種の市場価格。鳳のカードEV計算ではこの値を使う。
    race["combo_odds"] = odds_result.get("combo_odds") or {}

    # オッズ観測時刻を記録（取得項目仕様§2.3 odds_updated_at）。
    # 後方互換のため残している。中身は**単勝の** source_time（provider の official_datetime）で、
    # 式別の価格時刻ではない。券種ごとの時刻は odds_market_meta を見る。
    if odds_result.get("official_datetime"):
        race["odds_updated_at"] = odds_result["official_datetime"]

    # 券種別の provenance。監査専用で、予想・買い目・EV の計算には使わない。
    if odds_result.get("market_meta"):
        race["odds_market_meta"] = market_meta_block(odds_result["market_meta"])
