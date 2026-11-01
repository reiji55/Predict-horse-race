"""過去データでの探索 v1（edge-hist-v1、observe-only）。

仕様は docs/research/EDGE_LOOP_HIST_V1.md の §0〜§7（結果を見る前にコミット f1f8436 で固定）。この実装は仕様に無い選択をしない。
仕様の節番号（§）をコメントに残す。実行のたびに、今の仕様の §8 より前が固定したコミットの版と同じかを確かめる。

確かめる期間（§6・§9）は封印する：候補と β は、最初にコミットした探す期間の出力からだけ取り、候補が0件ならデータを読む前に止まる。
候補があっても、計算するのは候補と本線だけ。Git の履歴が要るので、浅い clone では確かめる期間を開けない（止まる）。

データは公開データ keiba_core（iinumac/keiba_core の data/warehouse、netkeiba 由来）。ライセンスの表記が無いので、
データはこのリポジトリに置かず、実行するときに場所を渡す。parquet の読み込みにだけ pyarrow を使う（研究用で、本番の依存には入れない）。
組の確率・帯の回収率・券の作り方・bootstrap は標準ライブラリだけで計算し、テストは合成データで行う。

出力は集計した数字だけ（レースごと・馬ごとの値、馬名、取得元の文字列は書かない）。
予想・正式成績・UI・Champion・scenario-frame/v1・事前登録済みの各評価は変えない。
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import random
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from research import market_baseline as mb
from research import portfolio as pf

ROOT = Path(__file__).resolve().parents[1]
SPEC = "docs/research/EDGE_LOOP_HIST_V1.md"
SPEC_COMMIT = "f1f843605ae335c633eaf23febafe279a45466f7"  # §0〜§7 を固定したコミット（結果を見る前）
SPEC_TAIL = "\n## 8. "  # これより前が固定した本文。訂正と追記は §8 以降にだけ書く
OUT_DIR = ROOT / "data" / "research" / "edge_hist"
EXPLORE_OUT = "data/research/edge_hist/explore_v1.json"
DATA_COMMIT = "2be1fafe3d9c"
PERIODS = {  # §2
    "explore": (datetime.date(2014, 7, 1), datetime.date(2021, 12, 31)),
    "confirm": (datetime.date(2022, 1, 1), datetime.date(2026, 10, 4)),
}
MIN_RUNNERS = 8
UNIT = 100
BANDS = [(1, 1), (2, 3), (4, 5), (6, 10), (11, 20), (21, 50), (51, 100), (101, 200), (201, None)]  # §4 A1
P_KS = (1, 3, 5, 10)  # §4 A2
BASELINE = "M-x_out_2"
BOOT_N = 2000
BOOT_SEED = 20261031
TRIO = "三連複"
SCRATCHED = {"取", "除"}
JUMP = re.compile(r"障害|J・G|J\.G")
GRADED = re.compile(r"\(G(?:1|2|3|I{1,3}|Ⅰ|Ⅱ|Ⅲ)\)")

Trio = tuple[int, int, int]


@dataclass(frozen=True)
class Race:
    race_id: str
    day: datetime.date
    race_num: int
    surface: str
    klass: str
    runners: dict[int, float]  # 馬番 → 確定単勝オッズ（出走取消・競走除外を除いた馬）
    order: tuple[int, int, int]  # 1・2・3着
    payout: int  # 3連複100円あたりの払戻
    popularity: int  # 当たった組の人気順


# ------------------------------------------------------------------ 区分（§4 A3）

def class_group(race_name: str, class_info: str) -> str:
    """クラスを6区分にする。旧称（500万下・1000万下・1600万下）は今の1勝・2勝・3勝に読み替える。"""
    name, info = race_name or "", class_info or ""
    if GRADED.search(name):
        return "重賞"
    text = f"{info} {name}"
    if "新馬" in text or "未勝利" in text:
        return "新馬・未勝利"
    if "1600万" in text or "3勝" in text:
        return "3勝"
    if "1000万" in text or "2勝" in text:
        return "2勝"
    if "500万" in text or "1勝" in text:
        return "1勝"
    if "オープン" in text or "(L)" in name or "(OP)" in name:
        return "OP・L"
    return "その他"


def segments(race: Race) -> list[str]:
    """§4 A3 の20区分のうち、このレースが入るもの（各軸で1つずつ）。"""
    n = len(race.runners)
    size = "8〜10頭" if n <= 10 else "11〜13頭" if n <= 13 else "14〜16頭" if n <= 16 else "17〜18頭"
    fav = min(race.runners.values())
    fav_band = "1番人気2.0倍未満" if fav < 2.0 else "1番人気2.0〜3.0倍" if fav < 3.0 else \
        "1番人気3.0〜5.0倍" if fav < 5.0 else "1番人気5.0倍以上"
    return [size, race.surface, race.klass, fav_band, "11R" if race.race_num == 11 else "11R以外"]


SEGMENT_NAMES = ["8〜10頭", "11〜13頭", "14〜16頭", "17〜18頭", "芝", "ダート",
                 "新馬・未勝利", "1勝", "2勝", "3勝", "OP・L", "重賞",
                 "1番人気2.0倍未満", "1番人気2.0〜3.0倍", "1番人気3.0〜5.0倍", "1番人気5.0倍以上",
                 "11R", "11R以外"]


# ------------------------------------------------------------------ 確率（§3）

def win_probs(runners: dict[int, float]) -> dict[int, float]:
    inv = {h: 1.0 / o for h, o in runners.items()}
    total = sum(inv.values())
    return {h: v / total for h, v in inv.items()}


def _stage_terms(races: Iterable[Race], stage: int) -> list[tuple[float, list[float]]]:
    """stage 着目（1〜3）の尤度に要る値：選ばれた馬の log p と、その時点で残っている馬の log p。"""
    out = []
    for r in races:
        lp = {h: math.log(v) for h, v in win_probs(r.runners).items()}
        taken = r.order[:stage - 1]
        out.append((lp[r.order[stage - 1]], [v for h, v in lp.items() if h not in taken]))
    return out


def stage_loglik(terms: list[tuple[float, list[float]]], beta: float) -> float:
    """着順の尤度のうち1つの着目の部分。残りの馬の p^β に比例して選ぶ。各着目の項は他の β に依らない。"""
    total = 0.0
    for chosen, rest in terms:
        m = max(beta * v for v in rest)
        total += beta * chosen - (m + math.log(sum(math.exp(beta * v - m) for v in rest)))
    return total


def fit_beta(races: list[Race], lo: float = 0.2, hi: float = 2.0, tol: float = 1e-4) -> tuple[float, float, float]:
    """着目ごとの指数を、尤度を最大にする値に決める（各項は凹なので黄金分割で足りる）。"""
    out = []
    g = (math.sqrt(5) - 1) / 2
    for stage in (1, 2, 3):
        terms = _stage_terms(races, stage)
        a, b = lo, hi
        c, d = b - g * (b - a), a + g * (b - a)
        fc, fd = stage_loglik(terms, c), stage_loglik(terms, d)
        while b - a > tol:
            if fc > fd:
                b, d, fd = d, c, fc
                c = b - g * (b - a)
                fc = stage_loglik(terms, c)
            else:
                a, c, fc = c, d, fd
                d = a + g * (b - a)
                fd = stage_loglik(terms, d)
        out.append(round((a + b) / 2, 4))
    return out[0], out[1], out[2]


def trio_dist(runners: dict[int, float], beta: tuple[float, float, float]) -> dict[Trio, float]:
    """§3 の q。p^β1 を正規化した勝率に、2・3着の指数 β2/β1・β3/β1 を掛けた Harville の形で組の確率を出す。"""
    b1, b2, b3 = beta
    raw = {h: v ** b1 for h, v in win_probs(runners).items()}
    total = sum(raw.values())
    p = {h: v / total for h, v in raw.items()}
    return mb.harville_trio(p, (b2 / b1, b3 / b1))


def x_horse(dist: dict[Trio, float]) -> int:
    marg = mb.top3_marginals(dist)
    return min(marg, key=lambda h: (-marg[h], h))  # 同点は馬番の小さい方（config/portfolio_v1.json の x_definition）


# ------------------------------------------------------------------ 券（§4 A2）

def band_counts(n_runners: int) -> list[int]:
    """§4 A1：各帯に入る組の数（C(頭数, 3) で頭打ち）。"""
    total = math.comb(n_runners, 3)
    out = []
    for lo, hi in BANDS:
        top = total if hi is None else min(hi, total)
        out.append(max(0, top - lo + 1))
    return out


def band_index(popularity: int) -> int:
    for i, (lo, hi) in enumerate(BANDS):
        if popularity >= lo and (hi is None or popularity <= hi):
            return i
    raise ValueError(popularity)


M_RULES = (("M-top5", None, 0), ("M-x_out_2", None, 2), ("M-cap3", 3, 0))
RULE_NAMES = tuple(f"P-{k}" for k in P_KS) + tuple(name for name, _, _ in M_RULES)


def tickets_by_rule(race: Race, beta: tuple[float, float, float],
                    names: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """1レースの各作り方の結果（投資・払戻・的中）。M- が最適と示せなければ、そのレースの M- は使えない（status に残す）。

    names を渡すと、その作り方だけを計算する（確かめる期間では候補と本線だけ、§9）。
    """
    trio = tuple(sorted(race.order))
    out: dict[str, dict[str, Any]] = {}
    for k in P_KS:
        if names is not None and f"P-{k}" not in names:
            continue
        hit = race.popularity <= k
        out[f"P-{k}"] = {"stake": k * UNIT, "payout": race.payout if hit else 0, "hit": hit, "status": "chosen"}
    m_rules = [m for m in M_RULES if names is None or m[0] in names]
    if not m_rules:
        return out
    dist = trio_dist(race.runners, beta)
    x = x_horse(dist)
    for name, cap, wx in m_rules:
        res = pf.choose(dist, x, 5, cap, wx)
        if res["status"] != "chosen" or not res["proven_optimal"]:
            out[name] = {"stake": 0, "payout": 0, "hit": False, "status": res["status"]}
            continue
        picked = {tuple(sorted(t)) for t in res["tickets"]}
        hit = trio in picked
        out[name] = {"stake": 5 * UNIT, "payout": race.payout if hit else 0, "hit": hit, "status": "chosen",
                     "p_sum": sum(dist[t] for t in picked)}
    return out


# ------------------------------------------------------------------ 集計（§4 指標）

def _quantile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:  # 一度も買わない区分（投資0）は区間を出さない
        return None
    i = q * (len(sorted_vals) - 1)
    lo, hi = math.floor(i), math.ceil(i)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def bootstrap(per_day: dict[str, dict[datetime.date, tuple[int, int]]], baseline: str,
              n: int = BOOT_N, seed: int = BOOT_SEED) -> dict[str, dict[str, float]]:
    """開催日単位の bootstrap。全ての作り方で同じ日の組を引き直し、回収率と、本線との回収率の差の分位点を出す。"""
    days = sorted({d for series in per_day.values() for d in series})
    rng = random.Random(seed)
    arrays = {k: ([series.get(d, (0, 0))[0] for d in days], [series.get(d, (0, 0))[1] for d in days])
              for k, series in per_day.items()}
    roi_draws: dict[str, list[float]] = {k: [] for k in per_day}
    diff_draws: dict[str, list[float]] = {k: [] for k in per_day}
    m = len(days)
    for _ in range(n):
        idx = [rng.randrange(m) for _ in range(m)]
        rois = {}
        for k, (stake, pay) in arrays.items():
            s = sum(stake[i] for i in idx)
            rois[k] = sum(pay[i] for i in idx) / s if s else float("nan")
        for k in per_day:
            roi_draws[k].append(rois[k])
            diff_draws[k].append(rois[k] - rois[baseline])
    out = {}
    for k in per_day:
        r = sorted(x for x in roi_draws[k] if not math.isnan(x))
        d = sorted(x for x in diff_draws[k] if not math.isnan(x))
        out[k] = {"roi_lo95": _quantile(r, 0.025), "roi_hi95": _quantile(r, 0.975),
                  "diff_lo95": _quantile(d, 0.025), "diff_hi95": _quantile(d, 0.975),
                  "diff_lo99": _quantile(d, 0.005), "diff_hi99": _quantile(d, 0.995)}
    return out


def summarise(races: list[Race], beta: tuple[float, float, float], keys: set[str] | None = None) -> dict[str, Any]:
    """§4 の A1・A2・A3 と区間。

    keys を渡すと（確かめる期間、§9）、その作り方と本線だけを計算し、帯（A1）や他の区分は計算しない。
    レースを外す規則は「計算した M- のどれかが最適と示せなければ外す」。探す期間で外れたレースは0件だった。
    """
    if keys is not None:
        return _summarise_only(races, beta, set(keys) | {BASELINE})
    band_stake = [0] * len(BANDS)
    band_pay = [0] * len(BANDS)
    band_hits = [0] * len(BANDS)
    per_day: dict[str, dict[datetime.date, list[int]]] = {}
    per_race_ratio: dict[str, list[float]] = {}
    totals: dict[str, Counter] = {}
    unusable: Counter = Counter()
    p_sums: dict[str, list[float]] = {}

    def add(key: str, day: datetime.date, stake: int, payout: int, hit: bool) -> None:
        series = per_day.setdefault(key, {})
        cur = series.setdefault(day, [0, 0])
        cur[0] += stake
        cur[1] += payout
        t = totals.setdefault(key, Counter())
        t["races"] += 1 if stake else 0
        t["stake"] += stake
        t["payout"] += payout
        t["hits"] += int(hit)
        if stake:
            per_race_ratio.setdefault(key, []).append(payout / stake)

    for r in races:
        for i, cnt in enumerate(band_counts(len(r.runners))):
            band_stake[i] += cnt * UNIT
        bi = band_index(r.popularity)
        band_pay[bi] += r.payout
        band_hits[bi] += 1
        rules = tickets_by_rule(r, beta)
        if any(v["status"] != "chosen" for v in rules.values()):
            unusable.update(k for k, v in rules.items() if v["status"] != "chosen")
            continue  # 本線と同じレースで比べるため、どれかの M- が使えないレースは全部の作り方から外す
        segs = segments(r)
        for name, v in rules.items():
            add(name, r.day, v["stake"], v["payout"], v["hit"])
            if "p_sum" in v:
                p_sums.setdefault(name, []).append(v["p_sum"])
        for seg in segs:
            for name in ("P-5", BASELINE):
                v = rules[name]
                add(f"{name}|{seg}", r.day, v["stake"], v["payout"], v["hit"])
        for seg in SEGMENT_NAMES:  # 区分の外のレースは見送り（投資0）として、同じ日の組に入れる
            if seg not in segs:
                for name in ("P-5", BASELINE):
                    per_day.setdefault(f"{name}|{seg}", {}).setdefault(r.day, [0, 0])

    bands = [{"band": f"{lo}" if hi == lo else f"{lo}〜{'' if hi is None else hi}", "combos": band_stake[i] // UNIT,
              "hits": band_hits[i], "roi": band_pay[i] / band_stake[i] if band_stake[i] else None}
             for i, (lo, hi) in enumerate(BANDS)]
    return {"bands": bands, "rules": _rows(per_day, totals, per_race_ratio, p_sums), "unusable_by_rule": dict(unusable)}


def _rows(per_day: dict[str, dict[datetime.date, list[int]]], totals: dict[str, Counter],
          per_race_ratio: dict[str, list[float]], p_sums: dict[str, list[float]]) -> dict[str, Any]:
    frozen = {k: {d: (v[0], v[1]) for d, v in series.items()} for k, series in per_day.items()}
    boot = bootstrap(frozen, BASELINE)
    rows = {}
    for key, t in sorted(totals.items()):
        ratios = per_race_ratio.get(key, [])
        sd = (sum((x - sum(ratios) / len(ratios)) ** 2 for x in ratios) / (len(ratios) - 1)) ** 0.5 if len(ratios) > 1 else None
        rows[key] = {"races": t["races"], "stake": t["stake"], "payout": t["payout"], "hits": t["hits"],
                     "roi": t["payout"] / t["stake"] if t["stake"] else None,
                     "hit_rate": t["hits"] / t["races"] if t["races"] else None,
                     "sd_ratio_per_race": sd,
                     "mean_p_sum": (sum(p_sums[key]) / len(p_sums[key])) if key in p_sums else None,
                     **boot.get(key, {})}
    return rows


def _summarise_only(races: list[Race], beta: tuple[float, float, float], wanted: set[str]) -> dict[str, Any]:
    """§9：渡した作り方（候補と本線）だけを計算する。"""
    base_names = {k.split("|")[0] for k in wanted}
    per_day: dict[str, dict[datetime.date, list[int]]] = {}
    per_race_ratio: dict[str, list[float]] = {}
    totals: dict[str, Counter] = {}
    p_sums: dict[str, list[float]] = {}
    unusable: Counter = Counter()
    for r in races:
        rules = tickets_by_rule(r, beta, base_names)
        if any(v["status"] != "chosen" for v in rules.values()):
            unusable.update(k for k, v in rules.items() if v["status"] != "chosen")
            continue
        segs = set(segments(r))
        for key in wanted:
            name, _, seg = key.partition("|")
            v = rules[name]
            series = per_day.setdefault(key, {}).setdefault(r.day, [0, 0])
            if seg and seg not in segs:
                continue  # 区分の外は見送り（投資0）として、同じ日の組に入れる
            series[0] += v["stake"]
            series[1] += v["payout"]
            t = totals.setdefault(key, Counter())
            t["races"] += 1
            t["stake"] += v["stake"]
            t["payout"] += v["payout"]
            t["hits"] += int(v["hit"])
            per_race_ratio.setdefault(key, []).append(v["payout"] / v["stake"])
            if "p_sum" in v and not seg:
                p_sums.setdefault(key, []).append(v["p_sum"])
    return {"rules": _rows(per_day, totals, per_race_ratio, p_sums), "unusable_by_rule": dict(unusable)}


def candidates(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """§5：本線（全レースの M-x_out_2）との差の下限が 0 を超えたもの。A2 は 95%、A3 は 99%。差の点推定の大きい順に最大3つ。"""
    base = summary["rules"][BASELINE]["roi"]
    out = []
    for key, row in summary["rules"].items():
        if key == BASELINE or row["roi"] is None:
            continue
        lo = row["diff_lo99"] if "|" in key else row["diff_lo95"]
        if lo is not None and lo > 0:
            out.append({"rule": key, "diff": row["roi"] - base, "diff_lo": lo,
                        "interval": "99%" if "|" in key else "95%"})
    return sorted(out, key=lambda c: -c["diff"])[:3]


# ------------------------------------------------------------------ 読み込み（§2）

def load_races(warehouse: Path, start: datetime.date, end: datetime.date) -> tuple[list[Race], Counter]:
    """keiba_core の warehouse から、§2 の対象のレースだけを作る。外したレースは理由ごとに数える。"""
    import pyarrow.parquet as pq  # 研究用。本番の依存には入れない

    def read(table: str, columns: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for f in sorted((warehouse / table).rglob("*.parquet")):
            year = re.search(r"year=(\d{4})", str(f))
            if year and not (start.year <= int(year.group(1)) <= end.year):
                continue
            rows.extend(pq.read_table(f, columns=columns).to_pylist())
        return rows

    meta = {r["race_id"]: r for r in read("races", ["race_id", "date", "race_num", "surface", "race_name", "class_info"])}
    starters: dict[str, list[dict[str, Any]]] = {}
    for r in read("results", ["race_id", "horse_number", "finish_position", "odds", "dnf_reason"]):
        if (r["dnf_reason"] or "") in SCRATCHED:
            continue
        starters.setdefault(r["race_id"], []).append(r)
    trios: dict[str, list[dict[str, Any]]] = {}
    for r in read("payouts", ["race_id", "bet_type", "horse_numbers", "payout", "popularity"]):
        if r["bet_type"] == TRIO:
            trios.setdefault(r["race_id"], []).append(r)

    races, excluded = [], Counter()
    for rid, m in meta.items():
        day = datetime.date.fromisoformat(m["date"])
        if not (start <= day <= end):
            continue
        if JUMP.search(m["race_name"] or "") or JUMP.search(m["class_info"] or ""):
            excluded["jump"] += 1
            continue
        rows = starters.get(rid, [])
        if any(r["odds"] is None or r["odds"] <= 0 for r in rows):
            excluded["odds_missing"] += 1
            continue
        if len(rows) < MIN_RUNNERS:
            excluded["few_runners"] += 1
            continue
        by_pos = {pos: [r["horse_number"] for r in rows if r["finish_position"] == pos] for pos in (1, 2, 3)}
        if any(len(v) != 1 for v in by_pos.values()):
            excluded["top3_not_unique"] += 1
            continue
        tri = trios.get(rid, [])
        if len(tri) != 1:
            excluded["trio_payout_count"] += 1
            continue
        order = (by_pos[1][0], by_pos[2][0], by_pos[3][0])
        if sorted(tri[0]["horse_numbers"]) != sorted(order):
            excluded["trio_mismatch"] += 1
            continue
        races.append(Race(rid, day, int(m["race_num"]), m["surface"], class_group(m["race_name"], m["class_info"]),
                          {r["horse_number"]: float(r["odds"]) for r in rows}, order,
                          int(tri[0]["payout"]), int(tri[0]["popularity"])))
    return races, excluded


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


# ------------------------------------------------------------------ 仕様と確かめる期間の封印（§8・§9）

class SealError(RuntimeError):
    """固定した仕様・探す期間の出力と食い違う。確かめる期間は開けない。"""


def pinned_spec_matches(head_text: str, pinned_text: str) -> bool:
    """今の仕様のうち §8 より前が、固定したコミットの仕様と同じか（訂正と追記は §8 以降にだけ書く）。"""
    return head_text.split(SPEC_TAIL, 1)[0].rstrip() == pinned_text.rstrip()


def check_spec() -> None:
    try:
        pinned = _git_bytes("show", f"{SPEC_COMMIT}:{SPEC}").decode("utf-8")
    except subprocess.CalledProcessError as exc:
        raise SealError(f"固定した仕様のコミット {SPEC_COMMIT[:12]} を読めない（浅い clone では確かめられない）") from exc
    if not pinned_spec_matches((ROOT / SPEC).read_text(encoding="utf-8"), pinned):
        raise SealError(f"{SPEC} の §8 より前が、固定したコミット {SPEC_COMMIT[:12]} の版と違う")


def explore_output_fixed(on_disk: bytes | None, at_head: bytes | None, first_added: bytes | None) -> bool:
    """探す期間の出力が、最初にコミットした版・HEAD の版・手元のファイルの3つで同じか。"""
    return on_disk is not None and on_disk == at_head == first_added


def load_fixed_explore() -> dict[str, Any]:
    """§9：最初にコミットしたまま書き換えられていない、探す期間の出力を読む。"""
    path = ROOT / EXPLORE_OUT
    on_disk = path.read_bytes() if path.exists() else None
    try:
        at_head = _git_bytes("show", f"HEAD:{EXPLORE_OUT}")
        added = _git_bytes("log", "--first-parent", "--diff-filter=A", "--format=%H", "--", EXPLORE_OUT).split()
        first_added = _git_bytes("show", f"{added[-1].decode()}:{EXPLORE_OUT}") if added else None
    except subprocess.CalledProcessError as exc:
        raise SealError("探す期間の出力の履歴を Git から読めない") from exc
    if not explore_output_fixed(on_disk, at_head, first_added):
        raise SealError(f"{EXPLORE_OUT} が、最初にコミットした版と違う（または手元で書き換えられている）")
    return json.loads(on_disk)


def confirm_plan(explore: dict[str, Any]) -> tuple[tuple[float, float, float], list[str]]:
    """§9：確かめる期間で使う β と候補を、探す期間の出力だけから決める。候補が0件なら止める。"""
    if explore.get("phase") != "explore" or explore.get("spec_commit") != SPEC_COMMIT:
        raise SealError("探す期間の出力が、固定した仕様で作ったものではない")
    found = [c["rule"] for c in explore.get("candidates") or []]
    if not found:
        raise SealError("探す期間の候補が0件なので、確かめる期間は開けない（§5）")
    known = set(explore.get("rules") or {}) - {BASELINE}
    if any(c not in known for c in found):
        raise SealError(f"探す期間で計算していない作り方が候補に入っている：{found}")
    b1, b2, b3 = (float(x) for x in explore["beta"])
    return (b1, b2, b3), found


def run(warehouse: Path, phase: str, beta: tuple[float, float, float] | None, only: list[str] | None) -> dict[str, Any]:
    start, end = PERIODS[phase]
    races, excluded = load_races(warehouse, start, end)
    if phase == "explore":
        beta = fit_beta(races)  # §3：探す期間だけで決める
    assert beta is not None
    # 確かめる期間（§9）：候補と本線だけを計算する。帯や他の作り方・区分は計算しない
    summary = summarise(races, beta) if only is None else summarise(races, beta, set(only))
    out = {
        "schema": "edge-hist-v1",
        "phase": phase,
        "period": [start.isoformat(), end.isoformat()],
        "spec": SPEC,
        "spec_commit": SPEC_COMMIT,
        "data": {"source": "iinumac/keiba_core data/warehouse", "commit": DATA_COMMIT},
        "races": len(races),
        "race_days": len({r.day for r in races}),
        "excluded": dict(excluded),
        "beta": list(beta),
        "baseline": BASELINE,
        **summary,
        "used_for_prediction": False,
        "official_results_untouched": True,
    }
    if phase == "explore":
        out["candidates"] = candidates(summary)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", type=Path, required=True, help="keiba_core の data/warehouse")
    ap.add_argument("--phase", choices=sorted(PERIODS), required=True,
                    help="confirm の候補と β は探す期間の出力からだけ取る（§9）。コマンドでは渡せない")
    args = ap.parse_args(argv)
    # データを読む前に、仕様と探す期間の出力を確かめる（§8・§9）
    try:
        check_spec()
        beta, only = confirm_plan(load_fixed_explore()) if args.phase == "confirm" else (None, None)
    except SealError as exc:
        ap.error(str(exc))
    out = run(args.warehouse, args.phase, beta, only)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{args.phase}_v1.json"
    if path.exists():  # 最初にコミットした出力は上書きしない。再実行の結果は別のファイルに書いて比べる
        path = OUT_DIR / f"{args.phase}_v1.rerun.json"
    text = json.dumps(out, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    path.write_text(text, encoding="utf-8")
    first = OUT_DIR / f"{args.phase}_v1.json"
    print(json.dumps({"phase": args.phase, "races": out["races"], "beta": out["beta"],
                      "candidates": out.get("candidates"), "written": str(path.relative_to(ROOT)),
                      "same_as_committed": first.read_text(encoding="utf-8") == text}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
