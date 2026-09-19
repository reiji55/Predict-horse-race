"""
妙味メーター計算（妙味メーター仕様_v1.md 全章）

入力：p（logic/prob_model.softmax_scores）, q（logic/prob_model.market_support）,
      n_usable群（信頼度用）, win_odds
出力：myomi (0-100), myomi_parts {"umami": float, "conf": float}, legendary(bool)

設定：config/myomi.json（§8）

--- 3成分（§2） ---

  A. 旨みの深さ（最大EVエッジ）
        EV_i   = p_i × win_odds_i                  # 1円あたり期待払戻。1.0超で期待値プラス
        A_raw  = max(EV) − 1                        （0未満は0にクランプ）
        A_norm = min(A_raw / edge_cap, 1)           （edge_cap=0.5 ＝ +50%エッジで飽和）

  B. 旨みの広がり（過小評価の総量）
        B_raw  = Σ max(p_i − q_i, 0)                （正の乖離だけ合計）
        B_norm = min(B_raw / breadth_cap, 1)        （breadth_cap=0.4）

  C. 予測信頼度
        data_cov = (n_usable ≥ usable_run_min を満たす馬の数) / heads
        conf     = conf_floor + (1 − conf_floor) × data_cov
        ※ **頭数は入れない**（§7 #4 で確定。引き継ぎ書v3 §2.1）

  合成
        umami = w_edge × A_norm + w_breadth × B_norm
        myomi = clamp(100 × umami × conf, 0, 100)
        legendary = myomi > myomi_threshold（=80）

--- 「妙味が高い＝当たりやすい」ではない（§3） ---

A・Bが大きい＝モデルが市場と食い違う＝人気薄を評価している、ということなので、
myomi が高いレースほど構造的に的中率は下がる。この緊張は追加ペナルティ無しで式に内在しており、
ケイ（堅く当てる）⇔源さん（旨みを取る）のキャラ対立がそのまま担う。式は旨み一本に徹する。
"""
from __future__ import annotations

from typing import Any


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_edge_depth(p: list[float | None], win_odds: list[float | None],
                       edge_cap: float) -> tuple[float, float]:
    """A. 旨みの深さ（§2 A）。戻り値は (A_raw, A_norm)。"""
    evs = [
        p_i * odds_i
        for p_i, odds_i in zip(p, win_odds)
        if p_i is not None and odds_i is not None and odds_i > 0
    ]
    if not evs:
        return 0.0, 0.0
    a_raw = max(0.0, max(evs) - 1.0)
    a_norm = min(a_raw / edge_cap, 1.0) if edge_cap > 0 else 0.0
    return a_raw, a_norm


def compute_breadth(p: list[float | None], q: list[float | None],
                    breadth_cap: float) -> tuple[float, float]:
    """B. 旨みの広がり（§2 B）。戻り値は (B_raw, B_norm)。"""
    b_raw = sum(
        max(p_i - q_i, 0.0)
        for p_i, q_i in zip(p, q)
        if p_i is not None and q_i is not None
    )
    b_norm = min(b_raw / breadth_cap, 1.0) if breadth_cap > 0 else 0.0
    return b_raw, b_norm


def compute_confidence(n_usable_list: list[int | None], config: dict[str, Any]) -> float:
    """
    C. 予測信頼度（§2 C）。時計データの揃った馬の割合が主ドライバ。

    heads は n_usable_list の長さ（＝出走頭数）を使う。**頭数そのものは式に入れない**（§7 #4）。
    """
    confidence = config["confidence"]
    conf_floor = confidence["conf_floor"]
    usable_run_min = confidence["usable_run_min"]

    heads = len(n_usable_list)
    if heads <= 0:
        return conf_floor

    covered = sum(1 for n in n_usable_list if n is not None and n >= usable_run_min)
    data_cov = covered / heads
    return conf_floor + (1.0 - conf_floor) * data_cov


def compute_myomi(p: list[float | None], q: list[float | None], n_usable_list: list[int | None],
                  win_odds: list[float | None], config: dict[str, Any]) -> dict[str, Any]:
    """
    レース1件分の p, q, n_usable, win_odds（出走馬順に対応するリスト）から
    {"myomi": float, "myomi_parts": {"umami": float, "conf": float}, "legendary": bool} を返す。

    myomi は小数第1位に丸めた値を返し、legendary もその丸め後の値で判定する
    （表示値と降臨判定が食い違わないようにするため）。
    """
    components = config["components"]

    _, a_norm = compute_edge_depth(p, win_odds, components["edge_cap"])
    _, b_norm = compute_breadth(p, q, components["breadth_cap"])
    conf = compute_confidence(n_usable_list, config)

    umami = components["w_edge"] * a_norm + components["w_breadth"] * b_norm
    myomi = round(_clamp(100.0 * umami * conf, 0.0, 100.0), 1)

    return {
        "myomi": myomi,
        "myomi_parts": {"umami": round(umami, 4), "conf": round(conf, 4)},
        "legendary": myomi > config["myomi_threshold"],
    }


def compute_card_ev_myomi(card_market_ev: dict[str, Any] | None,
                          n_usable_list: list[int | None],
                          config: dict[str, Any]) -> dict[str, Any]:
    """
    鳳が**実際に買うカード**の市場EVから、ユーザー向け妙味メーターを作る。

    旧式の「レース内のどこかに高単勝EV馬がいれば妙味上昇」では、
    その馬を鳳が1点も買っていないのに降臨できた。ここではその不整合を禁止する。

    card_market_ev.expected_roi = Σ(P(的中) × 実オッズ × 購入額) / 総購入額
    edge = expected_roi - 1
    umami = clamp(edge / card_edge_cap, 0, 1)
    myomi = 100 × umami × conf

    式別オッズが1点でも欠けて complete=false の場合は、EVを都合よく過大評価しないため
    myomi=0 / legendary=false とする。モデル確率p自体の較正は次フェーズの課題。
    """
    conf = compute_confidence(n_usable_list, config)
    card_cfg = config.get("card_ev", {})
    edge_cap = card_cfg.get("edge_cap", config["components"].get("edge_cap", 0.5))

    if not card_market_ev or not card_market_ev.get("complete"):
        return {
            "myomi": 0.0,
            "myomi_parts": {"umami": 0.0, "conf": round(conf, 4)},
            "legendary": False,
            "myomi_source": "otori_market_card_ev_unavailable",
        }

    roi = card_market_ev.get("expected_roi")
    if roi is None:
        return {
            "myomi": 0.0,
            "myomi_parts": {"umami": 0.0, "conf": round(conf, 4)},
            "legendary": False,
            "myomi_source": "otori_market_card_ev_unavailable",
        }

    edge = max(0.0, float(roi) - 1.0)
    umami = min(edge / edge_cap, 1.0) if edge_cap > 0 else 0.0
    value = round(_clamp(100.0 * umami * conf, 0.0, 100.0), 1)
    return {
        "myomi": value,
        "myomi_parts": {"umami": round(umami, 4), "conf": round(conf, 4)},
        "legendary": value > config["myomi_threshold"],
        "myomi_source": "otori_market_card_ev",
    }
