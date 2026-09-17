"""
Fページ：レース結果・払戻ページ

出典：取得項目_共通内部フォーマット仕様_v1.md §1.1（表の# F行）
取得タイミング：レース後 / 1週あたり4〜6ページ
取れるもの：確定着順、公式配当表 → results.json用（データスキーマ仕様v1.2 §5）

※ タスク6のスコープでは他フェッチャーと同じ枠組みで用意しておくが、
  results.json への変換自体はタスク7（成績集計）の仕事。

**集計側（results/build_results.py）は実装済み**なので、このフェッチャーが下記の形を返せば
そのまま results.json が作れる（`build_results(predictions, {race_id: fetch_results(...)}）`）。

    {
      "finish": [4, 9, 1],                       # 1着から順の馬番
      "dividends": {                             # 公式配当表（100円あたり）。**的中した組み合わせだけ**
        "ワイド": [ {"horses": [4, 9], "pay": 310},
                   {"horses": [4, 1], "pay": 240},
                   {"horses": [9, 1], "pay": 520} ],   # ワイドは3組
        "馬連":   [ {"horses": [4, 9], "pay": 1840} ],
        "3連複":  [ {"horses": [4, 9, 1], "pay": 2090} ]
      }
    }

的中判定は「買い目が dividends に無ければ外れ」で行うため、**同着で配当が複数出るレースでも
その分だけ配列に足せばよい**（集計側は順不同で照合する）。単勝・複勝は使わないので取らなくてよい。

TODO（実装フェーズ・HTMLサンプル入手後）：
- netkeibaレース結果ページのURL形式（`race.netkeiba.com/race/result.html?race_id=...` と思われる）
- 確定着順（馬番の配列）のパース
- 公式配当表のパース。払戻テーブルは組番が "6 - 8" のような表記で、金額に "1,840円" のように
  カンマと単位が付く想定（要検証）
- A・B2と同じくJS/AJAX描画の可能性があるので、まず素のHTMLで払戻表が埋まっているか確認する
"""
from __future__ import annotations

from typing import Any


def fetch_results(race_source_ref: str) -> dict[str, Any]:
    """
    レース結果ページから確定着順・公式配当表を取得する。

    race_source_ref: netkeibaのレースID
    戻り値: {"finish": [...], "dividends": {...}}
            dividends の型は データスキーマ仕様v1.2 §5 を参照
    """
    raise NotImplementedError("HTMLサンプル入手後に実装（取得項目仕様§1.1 Fページ）")
