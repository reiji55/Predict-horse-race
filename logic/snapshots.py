"""
発走前予想の凍結（スナップショット）

**なぜ必要か。** `data/predictions.json` はパイプラインを回すたびに上書きされる。
オッズは発走直前まで動くので、レース後に回すと「確定オッズで作り直した予想」が
そこに載る。それを成績集計に食わせると、**結果を見てから買い目を決めた予想**を
採点することになる（2026-09-19 に実際に起きた：17:42 JST の再生成分が採点され、
妙味 78.2→87.5・鳳が後付けで降臨し、中山の買い目も 9-14 から 9-13 に変わっていた）。

そこで、レースごとに **発走時刻より前に観測した最後の状態** を
`data/snapshots/{race_id}.json` に固めて残し、成績集計はそれだけを採点する。

--- 凍結のルール ---

各ビルド時刻 now について、レースごとに：

  now <  発走時刻 … まだ発走前。より新しいオッズなので**上書きする**
  now >= 発走時刻 … 発走後。既存のスナップショットには**一切触らない**
                    （無ければ pre_race=false で記録だけ残す。採点対象にはしない）

この非対称性が肝で、「発走後のビルドは過去を書き換えられない」ことだけを保証する。

--- ファイルの中身 ---

予想の再現に要るものを全部入れる：印・買い目・妙味に加えて、
`frozen_at`（＝ビルド時刻＝**オッズの観測時刻**）と `post_time`（発走時刻）。
あとから「何分前のオッズで決めた買い目か」を検証できるようにするため。
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("logic.snapshots")

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = ROOT / "data" / "snapshots"

JST = datetime.timezone(datetime.timedelta(hours=9))

# スナップショットに写し取るレースの項目（predictions.json の races[] と同じ形にしておく）
_RACE_FIELDS = (
    "id", "source_refs", "day", "venue", "race_no", "name", "grade",
    "post_time", "course", "myomi", "myomi_parts", "legendary", "marks", "cards",
)


def post_datetime(race: dict[str, Any]) -> datetime.datetime | None:
    """
    レースの発走日時（JST）を返す。判らなければ None。

    日付は race_id の先頭8桁（YYYYMMDD）から取る。`day` は "土" のような曜日なので使えない。
    """
    race_id = race.get("id") or race.get("race_id") or ""
    post_time = race.get("post_time")
    if len(race_id) < 8 or not race_id[:8].isdigit() or not post_time:
        return None
    try:
        hour, minute = (int(x) for x in str(post_time).split(":")[:2])
        date = datetime.datetime.strptime(race_id[:8], "%Y%m%d").date()
    except ValueError:
        logger.warning("発走時刻を読めませんでした: id=%s post_time=%r", race_id, post_time)
        return None
    return datetime.datetime(date.year, date.month, date.day, hour, minute, tzinfo=JST)


def snapshot_path(race_id: str, directory: Path | None = None) -> Path:
    return (directory or SNAPSHOT_DIR) / f"{race_id}.json"


def build_snapshot(race: dict[str, Any], week_id: str | None, frozen_at: str,
                   pre_race: bool) -> dict[str, Any]:
    """predictions.json の races[] 1件から、保存するスナップショットの中身を作る。"""
    snapshot = {
        "race_id": race.get("id"),
        "week_id": week_id,
        # frozen_at はビルド時刻。**この時点のオッズで買い目を決めた**という意味なので、
        # 発走時刻との差がそのまま「何分前の判断か」になる。
        "frozen_at": frozen_at,
        "pre_race": pre_race,
    }
    snapshot.update({k: race.get(k) for k in _RACE_FIELDS if k != "id"})
    return snapshot


def freeze(predictions: dict[str, Any], now: datetime.datetime | None = None,
           directory: Path | None = None) -> list[dict[str, Any]]:
    """
    predictions.json の内容を、レースごとのスナップショットに固める。

    戻り値は各レースの処理内容 [{"race_id", "action", "pre_race"}]。
    action は "frozen"（発走前なので保存・上書き）/ "kept"（発走後なので既存を保護）/
    "late"（発走後で既存も無い＝採点対象外の記録）/ "unknown"（発走時刻が不明）。
    """
    directory = directory or SNAPSHOT_DIR
    directory.mkdir(parents=True, exist_ok=True)

    now = now or datetime.datetime.now(JST)
    week_id = predictions.get("week_id")
    frozen_at = predictions.get("generated_at") or now.isoformat(timespec="seconds")

    report: list[dict[str, Any]] = []
    for race in predictions.get("races", []):
        race_id = race.get("id")
        if not race_id:
            continue
        path = snapshot_path(race_id, directory)
        post_at = post_datetime(race)

        if post_at is None:
            # 発走時刻が判らないときは、既存を壊さないことを優先する（保守的な側に倒す）
            action = "unknown"
            if not path.exists():
                _write(path, build_snapshot(race, week_id, frozen_at, pre_race=False))
            logger.warning("発走時刻が判らないため凍結を保留しました: %s", race_id)
        elif now < post_at:
            _write(path, build_snapshot(race, week_id, frozen_at, pre_race=True))
            action = "frozen"
        elif path.exists():
            action = "kept"
            logger.info("発走後のビルドなので既存のスナップショットを保護しました: %s", race_id)
        else:
            _write(path, build_snapshot(race, week_id, frozen_at, pre_race=False))
            action = "late"
            logger.error(
                "発走後（%s）に初めて予想が作られました: %s。"
                "**結果を見たあとの予想なので成績集計には載せません。**",
                post_at.isoformat(timespec="minutes"), race_id,
            )

        report.append({"race_id": race_id, "action": action,
                       "pre_race": action == "frozen"})
    return report


def _write(path: Path, snapshot: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_all(directory: Path | None = None) -> list[dict[str, Any]]:
    """保存済みのスナップショットを race_id 順に読み込む。"""
    directory = directory or SNAPSHOT_DIR
    if not directory.is_dir():
        return []
    snapshots = []
    for path in sorted(directory.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                snapshots.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            logger.warning("スナップショットを読めませんでした: %s", path.name)
    return snapshots


def as_predictions(directory: Path | None = None) -> dict[str, Any]:
    """
    スナップショット群を predictions.json と同じ形（{"races": [...]}）にして返す。

    **発走前に凍結できたものだけ**を入れる。発走後に作られた予想（pre_race=false）は
    結果を知ったうえでの予想なので、成績集計に混ぜない。
    """
    races = []
    excluded = []
    for snapshot in load_all(directory):
        if not snapshot.get("pre_race"):
            excluded.append(snapshot.get("race_id"))
            continue
        race = {k: snapshot.get(k) for k in _RACE_FIELDS if k != "id"}
        race["id"] = snapshot.get("race_id")
        race["frozen_at"] = snapshot.get("frozen_at")
        races.append(race)

    if excluded:
        logger.warning("発走前に凍結できていないため成績集計から除外します: %s",
                       ", ".join(str(r) for r in excluded))
    return {"races": races}
