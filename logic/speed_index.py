"""
スピード指数 算出ロジック（スピード指数仕様_v1.md 全章）

入力：共通内部フォーマットの past_runs（直近5走）
出力：馬1頭あたりの {"best": float, "avg": float, "latest": float, "n_usable": int} または None

設定：config/speed_index.json（§5）
基準タイム表：config/base_times.json（§4。scripts/build_base_times.py が生成）

--- 1走あたりの計算式（§1） ---

    scale         = 2000 / dist                       # scale_mode="flat" なら 1.0
    going_sec     = going_adj[surface][going] × dist/2000
    impost_sec    = (impost − impost_ref) × k_impost × dist/2000
    class_sec     = class_offset[class] × dist/2000
    expected_time = base_time[venue][surface][dist] + class_credit × class_sec
    adjusted_time = time_sec − going_sec − impost_sec
    指数           = index_base + (expected_time − adjusted_time) × scale × point_per_sec_at2000

--- ⚠ class_credit について（仕様の内部矛盾への対処・要レビュー） ---

仕様§1の式は `expected_time = base_time + class_sec` だが、これをそのまま適用すると
**同§が定める「指数100 = そのコースのOPクラス勝ち馬水準」が成り立たなくなる**。
class_sec を期待タイムに足すと、下級条件では期待タイムも遅くなるため
「未勝利戦で未勝利相当のタイムを出した馬」も指数100になり、クラス差が式の中で相殺されるため。

  例）京都芝1200・base_time 67.8 の場合
    未勝利を 69.5秒 で走った馬 → 指数 96.7
    OPを      68.3秒 で走った馬 → 指数 91.7     ← 1.2秒速いのに低く出る

§4（base_time = median(win_time − class_offset × dist/2000)）でクラス差を取り除いておきながら、
§1で同じ量を足し戻す構造になっており、①スピード指数が「素の時計＝絶対的な能力」を測り
②適性が「条件への当てはまり」を見る、という役割分担（買い目生成仕様§1.3）とも逆に働く。

そこで **`class_credit` 係数を config に追加**し、既定値 0.0（＝クラス補正を指数に足し戻さない
＝絶対時計で評価）とした。`class_credit: 1.0` にすれば仕様書§1の記述そのままの挙動に戻る。
どちらが回収率に効くかは後方検証で決める（スピード指数仕様§0「係数は全部初期値、
チューニングが本丸」の思想に沿った扱い）。**この判断は docs/OPEN_QUESTIONS.md B-1 に記録。**

--- usable run 判定（§2） ---

以下を全て満たす走のみ指数計算に使う：
  1. time_sec が null でない（中止・除外・取消は除外）
  2. venue がJRA中央10場（地方・海外は基準タイム表が無いため除外）
  3. surface が 芝/ダ（障害は除外）
  4. base_time 表に該当コース（venue×surface×dist）が存在する
  5. class が正規化表記で判定できる

going が欠損している走は「良（補正0）」として扱う（usable判定には含めない＝仕様§2の5条件どおり）。
impost が欠損している走も同様に補正0とする。
"""
from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path
from typing import Any

from scraper.common import constants

logger = logging.getLogger("logic.speed_index")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "speed_index.json"
BASE_TIMES_PATH = Path(__file__).resolve().parent.parent / "config" / "base_times.json"

VALID_SURFACES = ("芝", "ダ")


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_base_times() -> dict[str, Any]:
    with BASE_TIMES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def lookup_base_time(base_times: dict[str, Any], venue: str | None, surface: str | None,
                     dist: int | None) -> float | None:
    """基準タイム表から base_time を引く。JSONのキーは文字列なので距離を str 化して照合する。"""
    if venue is None or surface is None or dist is None:
        return None
    by_surface = base_times.get(venue)
    if not isinstance(by_surface, dict):
        return None
    by_dist = by_surface.get(surface)
    if not isinstance(by_dist, dict):
        return None
    value = by_dist.get(str(dist))
    return float(value) if isinstance(value, (int, float)) else None


def is_usable(run: dict[str, Any], config: dict[str, Any], base_times: dict[str, Any]) -> bool:
    """スピード指数仕様§2 の5条件を全て満たすか。"""
    if run.get("time_sec") is None:
        return False
    if run.get("venue") not in constants.JRA_VENUES:
        return False
    if run.get("surface") not in VALID_SURFACES:
        return False
    if lookup_base_time(base_times, run.get("venue"), run.get("surface"), run.get("dist")) is None:
        return False
    if run.get("class") not in config["class_offset"]:
        return False
    return True


def compute_run_index(run: dict[str, Any], config: dict[str, Any],
                      base_times: dict[str, Any]) -> float | None:
    """1走分の指数を計算する（スピード指数仕様§1）。usable判定外なら None。"""
    if not is_usable(run, config, base_times):
        return None

    dist = run["dist"]
    surface = run["surface"]
    dist_ratio = dist / 2000

    scale = 1.0 if config.get("scale_mode") == "flat" else 2000 / dist

    going = run.get("going")
    going_adj = config["going_adj"].get(surface, {})
    going_sec = going_adj.get(going, 0.0) * dist_ratio

    impost = run.get("impost")
    if impost is None:
        impost_sec = 0.0
    else:
        impost_sec = (impost - config["impost_ref"]) * config["k_impost"] * dist_ratio

    class_sec = config["class_offset"][run["class"]] * dist_ratio
    class_credit = config.get("class_credit", 0.0)

    base_time = lookup_base_time(base_times, run["venue"], surface, dist)
    expected_time = base_time + class_credit * class_sec
    adjusted_time = run["time_sec"] - going_sec - impost_sec

    return config["index_base"] + (expected_time - adjusted_time) * scale * config["point_per_sec_at2000"]


def compute_horse_speed(past_runs: list[dict[str, Any]], config: dict[str, Any] | None = None,
                        base_times: dict[str, Any] | None = None,
                        target_surface: str | None = None) -> dict[str, Any] | None:
    """
    馬1頭の past_runs（直近5走・新しい順）から speed 集約値を計算する（スピード指数仕様§3）。

    戻り値: {"best": ..., "avg": ..., "latest": ..., "n_usable": ...}
            usable走が1本も無ければ None（＝合成スコア側で①欠損＋不確実フラグ）

    鮮度加重平均の重みは **past_runs 内の元の位置**（0走目=最新=1.0, 1走目=0.9, …）で引く。
    間に usable でない走が挟まっても後続の重みを繰り上げない（「鮮度」の意味を保つため）。
    走数が recency_weights の長さを超える場合は末尾の重みを流用する。
    """
    config = config if config is not None else load_config()
    base_times = base_times if base_times is not None else load_base_times()

    weights = config["recency_weights"]
    guard = config.get("guard", {})
    same_surface_only = bool(guard.get("same_surface_only", False))

    scored: list[tuple[int, float]] = []  # (元の位置, 指数)
    for position, run in enumerate(past_runs):
        # 基準タイム表が疎な段階で芝レースの評価に「たまたま表があるダート走」だけが残ると、
        # その馬だけ不自然に減点される。予測対象の芝/ダが判る場合は同じsurfaceだけで指数を作る。
        if (same_surface_only and target_surface in VALID_SURFACES
                and run.get("surface") != target_surface):
            continue
        index = compute_run_index(run, config, base_times)
        if index is not None:
            scored.append((position, index))

    if not scored:
        return None

    weighted_sum = 0.0
    weight_total = 0.0
    for position, index in scored:
        w = weights[position] if position < len(weights) else weights[-1]
        weighted_sum += w * index
        weight_total += w

    return {
        "best": max(index for _, index in scored),
        "avg": weighted_sum / weight_total,
        "latest": scored[0][1],  # past_runs は新しい順なので、最初のusable走が最新
        "n_usable": len(scored),
    }


def speed_raw(speed: dict[str, Any] | None, config: dict[str, Any] | None = None) -> float | None:
    """
    合成スコア①の入力値（買い目生成仕様§1.3）。

        speed_raw = w_best × best + w_avg × avg     # 初期値 0.6 / 0.4

    `latest`（調子）は v1 では未使用（買い目生成仕様§1.3・スピード指数仕様§6-5）。
    """
    if speed is None:
        return None
    config = config if config is not None else load_config()
    agg = config["agg"]
    return agg["w_best"] * speed["best"] + agg["w_avg"] * speed["avg"]


def apply_race_speed_guard(horses: list[dict[str, Any]],
                           config: dict[str, Any]) -> dict[str, Any]:
    """
    レース内で①スピード指数の**欠損が非対称に効く**のを防ぐ安全弁。

    背景:
      base_times が未充足だと「A馬は指数0本、B馬は悪い1走だけ指数あり」のようになる。
      旧実装は A馬では speed を欠損として残りの要素へ重みを再配分する一方、
      B馬ではその悪い1走を45%相当で使うため、**データがある馬だけが不利になる**ことがあった。

    guard設定:
      min_usable_runs:
        これ未満しか指数化できない馬は speed を使わない（既定2）。
      min_race_coverage:
        上記を満たす馬の比率がこれ未満なら、そのレースではspeed要素を全馬一律で無効化。
      neutral_impute_missing:
        coverageを満たしたレースでは、speed欠損馬を観測馬の平均値で中立補完する。
        z標準化の入力を平均で埋めるので、欠損馬のspeed寄与は**ちょうど z=0**になる。

    この関数は「基準タイム不足を治す」ものではない。未充足の間にランキングを歪めないための
    fail-safe。base_timesが十分埋まれば自然に coverage が上がり、speedが通常利用される。
    """
    guard = config.get("guard", {})
    min_runs = max(1, int(guard.get("min_usable_runs", 1)))
    min_coverage = float(guard.get("min_race_coverage", 0.0))
    neutral_impute = bool(guard.get("neutral_impute_missing", False))

    total = len(horses)
    raw_available = sum(1 for h in horses if h.get("speed_raw") is not None)

    qualified: list[float] = []
    for horse in horses:
        speed = horse.get("speed_raw")
        n_usable = int(horse.get("n_usable") or 0)
        if speed is None or n_usable < min_runs:
            horse["speed_raw"] = None
            horse["speed_imputed"] = False
            horse["uncertain"] = True
            # **使わなかった走は n_usable にも残さない。** ここを残すと、speedを捨てたのに
            # 妙味メーターの信頼度（＝時計データの揃った馬の割合）だけが高いままになる。
            horse["n_usable"] = 0
        else:
            qualified.append(float(speed))
            horse["speed_imputed"] = False

    qualified_count = len(qualified)
    coverage = qualified_count / total if total else 0.0

    report: dict[str, Any] = {
        "raw_available_horses": raw_available,
        "qualified_horses": qualified_count,
        "total_horses": total,
        "coverage": round(coverage, 4),
        "min_usable_runs": min_runs,
        "min_race_coverage": min_coverage,
        "same_surface_only": bool(guard.get("same_surface_only", False)),
        "used": False,
        "imputed_horses": 0,
        "reason": None,
    }

    if total == 0:
        report["reason"] = "no_horses"
        return report

    if not qualified or coverage < min_coverage:
        for horse in horses:
            horse["speed_raw"] = None
            horse["speed_imputed"] = False
            # ①を捨てた以上、時計の裏づけはこのレースには無い。
            # n_usable を残すと妙味の信頼度が「時計が揃っている」と言い続けてしまう。
            horse["n_usable"] = 0
        report["reason"] = "insufficient_race_coverage"
        return report

    if neutral_impute:
        neutral = statistics.fmean(qualified)
        imputed = 0
        for horse in horses:
            if horse.get("speed_raw") is None:
                horse["speed_raw"] = neutral
                horse["speed_imputed"] = True
                horse["uncertain"] = True
                imputed += 1
        report["neutral_value"] = round(neutral, 4)
        report["imputed_horses"] = imputed

    report["used"] = True
    report["reason"] = "ok"
    return report


def warn_if_base_times_empty(base_times: dict[str, Any]) -> None:
    """
    base_times が空だと usable判定の条件4で**全走が落ち**、エラーにならないまま
    全馬 speed=None になる（＝①が丸ごと効かなくなる）。静かに劣化するのを防ぐため警告を出す。
    """
    if not base_times:
        logger.warning(
            "config/base_times.json が空です。全ての過去走が usable 判定を通らないため、"
            "スピード指数は全馬 None になります。run_base_times.yml で基準タイム表を埋めてください。"
        )
