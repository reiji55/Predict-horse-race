"""edge-loop の試験の版の、券の決め方（純粋な関数だけ）。

登録した版の券は、登録コミットのこのファイルを Git から読み込んで決める（research/edge_trial.py）。
後でこのファイルを変えても、登録済みの版の券は変わらない。決め方を変えるときは、新しい版として登録する。
このファイルは標準ライブラリ以外を import しない（登録コミットの版だけで動くようにするため）。
"""
from __future__ import annotations

import itertools
from typing import Any, Iterable

Trio = tuple[int, int, int]


def marked_horses(marks: Iterable[dict[str, Any]] | None, runners: Iterable[int],
                  symbols: Iterable[str]) -> list[int]:
    """凍結版の印のうち、記号が symbols に入り、発走前の単勝オッズがある馬（runners）の馬番（昇順）。"""
    allowed, wanted = set(runners), set(symbols)
    out = set()
    for m in marks or []:
        num = m.get("num")
        if m.get("mk") in wanted and isinstance(num, int) and num in allowed:
            out.add(num)
    return sorted(out)


def marks_top_n(dist: dict[Trio, float], marked: Iterable[int], n: int, min_marked: int) -> list[Trio]:
    """3頭とも印の馬の組のうち、市場の確率 q が大きい順に n 組（edge-v1 §2）。

    q が同じなら、馬番を小さい順に並べた組の辞書順で前のもの。候補が n 組に満たなければ候補をすべて返す。
    印の馬が min_marked に満たなければ空（見送り）。
    """
    horses = sorted(set(marked))
    if len(horses) < min_marked:
        return []
    candidates = list(itertools.combinations(horses, 3))
    candidates.sort(key=lambda t: (-dist.get(t, 0.0), t))
    return candidates[:n]
