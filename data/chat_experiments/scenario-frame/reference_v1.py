#!/usr/bin/env python3
"""Read-only reference generator for scenario-frame/v1.

Preconditions: the rule has been committed, the source raw is the pre-race
version pinned to a Git SHA, and the user has independently checked scratches.
This script never changes the repository or source files.

Registered with rule_v1.md (data/chat_experiments/scenario-frame/reference_v1.py).
Scratches confirmed against the official pre-race announcement are passed with
--scratched (raw has no scratch field). candidates follow snapshot.marks, because
records are checked against the runners of the snapshot.

Inputs are read with git from one commit: the latest commit on main that updated
data/snapshots/{race_id}.json, which also carries raw/{week_id}.json of the same
pipeline run. --write writes only to
data/chat_experiments/scenario-frame/races/{race_id}/{YYYYmmddTHHMMSS}.json and
refuses when a record for the race already exists (working tree or main history).

Usage (record):  git fetch origin main && python reference_v1.py --repo . \
                     --race-id 20261011-tokyo-11 --status-confirmed [--scratched 5] --write
Usage (audit):   python reference_v1.py --repo . --race-id ... --status-confirmed \
                     --git-sha <SHA in summary> --now <created_at>   # prints the same record
"""
import argparse
import copy
import datetime as dt
import itertools
import json
import math
import re
import statistics
import subprocess
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9))
LABELS = ("A", "B", "C")
CLASSES = {"mi": 0, "1win": 1, "2win": 2, "3win": 3,
           "op": 4, "g3": 5, "g2": 6, "g1": 7}


def finite(x):
    return isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x)


def cancelled(e):
    if e.get("scratched") is True or e.get("withdrawn") is True:
        return True
    status = str(e.get("status") or "").lower()
    return any(s in status for s in ("取消", "除外", "withdrawn", "scratched", "cancelled"))


def run_ok(run, race):
    if not isinstance(run, dict):
        return False
    try:
        run_date = dt.date.fromisoformat(str(run["date"]))
        race_date = dt.date.fromisoformat(str(race["date"]))
    except (ValueError, TypeError, KeyError):
        return False
    heads = run.get("heads")
    finish = run.get("finish")
    margin = run.get("margin_sec")
    distance = run.get("dist")
    target_dist = race["course"].get("dist")
    return (run_date < race_date
            and run.get("surface") == race["course"].get("surface")
            and finite(distance) and finite(target_dist)
            and abs(distance - target_dist) <= 400
            and run.get("class") in CLASSES
            and isinstance(finish, int) and not isinstance(finish, bool) and finish >= 1
            and (not isinstance(heads, int) or finish <= heads)
            and finite(margin) and margin >= 0)


def score_b(run, race, index):
    rank = CLASSES[run["class"]]
    venue_eq = run.get("venue") == race["venue"]
    diff = abs(run["dist"] - race["course"]["dist"])
    b1 = rank >= CLASSES["op"] and run["finish"] <= 3
    b2 = (venue_eq and diff <= 200 and
          rank >= CLASSES[race["grade"]] and run["finish"] <= 3)
    if not (b1 or b2):
        return None
    score = (-rank, -int(venue_eq), diff, run["finish"], run["margin_sec"],
             -dt.date.fromisoformat(run["date"]).toordinal(), index)
    label = "B1" if b1 else "B2"
    return score, label


def score_c(run, race, entry, index):
    if CLASSES[run["class"]] < max(0, CLASSES[race["grade"]] - 1):
        return None
    finish, margin = run["finish"], run["margin_sec"]
    diff = abs(run["dist"] - race["course"]["dist"])
    is_handicap = "ハンデ" in str(race["course"].get("note") or "")
    c1 = 4 <= finish <= 8 and margin <= 0.7
    c2 = (is_handicap and 2 <= finish <= 8 and margin <= 1.2 and
          finite(run.get("impost")) and finite(entry.get("impost")) and
          run["impost"] - entry["impost"] >= 2)
    c3 = 2 <= finish <= 8 and margin <= 0.5 and 200 <= diff <= 400
    if c1:
        label, order = "C1", 1
    elif c2:
        label, order = "C2", 2
    elif c3:
        label, order = "C3", 3
    else:
        return None
    score = (order, margin, -CLASSES[run["class"]], diff,
             -dt.date.fromisoformat(run["date"]).toordinal(), index)
    return score, label


def explain_run(label, run, race, entry):
    d = f"{run['date']} {run.get('venue','?')} {run.get('surface','?')}{run.get('dist','?')}m"
    x = f"{label} {d} {run['class']} {run['finish']}着・着差{run['margin_sec']}秒"
    if label == "C2":
        x += f"・斤量{run['impost']}→{entry['impost']}kg"
    if label == "C3":
        x += f"・距離差{abs(run['dist'] - race['course']['dist'])}m"
    return x


def select_frames(race, entries, snapshot):
    marks = snapshot["marks"]
    n = len(marks)
    ranks = {m["num"]: (i + 1, m.get("top3_rank")) for i, m in enumerate(marks)}
    rank_a, rank_b, rank_c = [], [], []
    evidence = {e["num"]: {} for e in entries}
    for e in entries:
        num = e["num"]
        if cancelled(e):
            continue
        base, t3 = ranks[num]
        base = base if isinstance(base, int) else math.inf
        t3 = t3 if isinstance(t3, int) and t3 > 0 else math.inf
        if base <= 4 or t3 <= 3:
            rank_a.append(((min(base, t3), base + t3, base, num), num))
            evidence[num]["A"] = (f"A 共通順位{base}位・Top3順位"
                                   f"{t3 if t3 != math.inf else '欠損'}位")
        br = []
        cr = []
        for i, run in enumerate((e.get("past_runs") or [])[:5]):
            if not run_ok(run, race):
                continue
            b = score_b(run, race, i)
            if b is not None:
                br.append((b[0], b[1], run))
            c = score_c(run, race, e, i)
            if c is not None:
                cr.append((c[0], c[1], run))
        if br:
            br.sort(key=lambda x: x[0])
            key, label, run = br[0]
            rank_b.append((key + (num,), num))
            evidence[num]["B"] = explain_run(label, run, race, e)
        if cr:
            cr.sort(key=lambda x: x[0])
            key, label, run = cr[0]
            rank_c.append((key + (num,), num))
            evidence[num]["C"] = explain_run(label, run, race, e)
    picks = {"A": [num for _, num in sorted(rank_a)[:3]],
             "B": [num for _, num in sorted(rank_b)[:2]],
             "C": [num for _, num in sorted(rank_c)[:2]]}
    ranks_by_num = {num: {} for num in evidence}
    for label in LABELS:
        for i, num in enumerate(picks[label]):
            ranks_by_num[num][label] = i + 1
    candidates = []
    for e in sorted(entries, key=lambda x: x["num"]):
        num = e["num"]
        frames = [label for label in LABELS if num in picks[label]]
        rec = {"num": num, "frames": frames}
        if cancelled(e):
            rec["evidence"] = "発走前取消／除外"
        elif frames:
            rec["evidence"] = "; ".join(evidence[num][l] for l in frames)
        candidates.append(rec)
    return candidates, ranks_by_num


def assignment(horses, rank_by_num):
    """Best maximum distinct-frame matching, deterministic under ties."""
    for k in range(min(len(horses), 3), 0, -1):
        possibilities = []
        for sub in itertools.combinations(horses, k):
            for labs in itertools.permutations(LABELS, k):
                if all(lab in rank_by_num[h] for h, lab in zip(sub, labs)):
                    d = dict(zip(sub, labs))
                    # tie: rank total, then labels assigned to horses sorted by number
                    code = ",".join(f"{h}:{d.get(h, '-') }" for h in horses)
                    possibilities.append((sum(rank_by_num[h][d[h]] for h in sub),
                                          code, "+".join(l for l in LABELS if l in labs)))
        if possibilities:
            return k, min(possibilities)[2]
    return 0, ""


def tickets(rank_by_num):
    pool = sorted(n for n, frames in rank_by_num.items() if frames)
    exposure = {n: 0 for n in pool}
    pairs_seen = set()
    bets = []
    triples = list(itertools.combinations(pool, 3))
    eligible_triples = [(x, *assignment(x, rank_by_num)) for x in triples]
    eligible_triples = [x for x in eligible_triples if x[1] >= 2]
    if not eligible_triples:
        return []
    def quality(horses):
        return sum(min(rank_by_num[n].values()) for n in horses)
    def score(item):
        horses, diversity, _ = item
        ex = [exposure[h] + 1 for h in horses]
        n_new = sum(pair not in pairs_seen for pair in itertools.combinations(horses, 2))
        return (-diversity, max(ex), sum(ex), -n_new, quality(horses), horses)
    def take(item, bet_type):
        horses, div, tag = item
        bets.append({"type": bet_type, "horses": list(horses), "amt": 100, "frame_tag": tag})
        for h in horses:
            exposure[h] += 1
        pairs_seen.update(itertools.combinations(horses, 2))
    while eligible_triples and len(bets) < 5:
        item = min(eligible_triples, key=score)
        take(item, "3連複")
        eligible_triples.remove(item)
    if len(bets) == 5:
        return bets
    wides = [(x, *assignment(x, rank_by_num)) for x in itertools.combinations(pool, 2)]
    wides = [x for x in wides if x[1] >= 2]
    wide_first = None
    while wides and sum(b["amt"] for b in bets) < 500:
        item = min(wides, key=score)
        take(item, "ワイド")
        if wide_first is None:
            wide_first = bets[-1]
        wides.remove(item)
    if sum(b["amt"] for b in bets) < 500:
        target = wide_first or bets[0]
        target["amt"] += 500 - sum(b["amt"] for b in bets)
    assert sum(b["amt"] for b in bets) == 500
    assert len(set((b["type"], tuple(b["horses"])) for b in bets)) == len(bets)
    return bets


def make(raw, snapshot, race_id, git_sha, now, status_confirmed, scratched=(), author="ChatGPT"):
    if not status_confirmed:
        raise ValueError("発走前の取消・除外情報を公式に確認した後、--status-confirmedを指定")
    races = [r for r in raw.get("races", []) if r.get("id") == race_id]
    if len(races) != 1:
        raise ValueError("rawに対象race_idが一意に見つからない")
    race = copy.deepcopy(races[0])
    scratched = set(scratched)
    unknown = scratched - {e.get("num") for e in race.get("entries", [])}
    if unknown:
        raise ValueError(f"--scratched の馬番がrawにいない: {sorted(unknown)}")
    for e in race.get("entries", []):
        if e.get("num") in scratched:
            e["scratched"] = True
    if snapshot.get("race_id") != race_id or snapshot.get("pre_race") is not True:
        raise ValueError("Championの発走前snapshotでない")
    frozen = dt.datetime.fromisoformat(snapshot["frozen_at"])
    post = dt.datetime.combine(dt.date.fromisoformat(race["date"]),
                               dt.time.fromisoformat(race["post_time"]), tzinfo=JST)
    if not (frozen <= now < post):
        raise ValueError(f"時系列異常 frozen_at={frozen.isoformat()} now={now.isoformat()} post={post.isoformat()}")
    if race.get("course", {}).get("surface") not in ("芝", "ダ") or race.get("race_no") != 11:
        raise ValueError("対象外レース（JRA11R平地芝／ダートのみ）")
    entries = race.get("entries", [])
    all_nums = [e.get("num") for e in entries]
    mark_nums = [m.get("num") for m in snapshot.get("marks", [])]
    if len(set(all_nums)) != len(all_nums) or len(set(mark_nums)) != len(mark_nums):
        raise ValueError("出走馬番号に重複")
    if not set(mark_nums).issubset(set(all_nums)):
        raise ValueError("snapshotにrawにいない馬がいる")
    mismatch = set(all_nums) - set(mark_nums)
    if any(not cancelled(e) for e in entries if e["num"] in mismatch):
        raise ValueError("snapshotとrawの馬番が相違し、取消でも説明できない")
    if len(entries) < 3 or not all(isinstance(n, int) and n > 0 for n in all_nums):
        raise ValueError("全馬リストが異常")
    if race.get("grade") not in CLASSES:
        candidates = [{"num": e["num"], "frames": []} for e in sorted(entries, key=lambda x: x["num"])]
        p_reason = "race_grade_unknown"
        bets = []
    else:
        # Cancelled entries absent from snapshot cannot be given a base rank.
        for e in entries:
            if e["num"] not in set(mark_nums) and not cancelled(e):
                raise ValueError("取消以外でmarks欠損")
        # Placeholder rank for cancelled entries, ignored in selection.
        snap = dict(snapshot)
        snap["marks"] = list(snapshot["marks"]) + [
            {"num": e["num"], "top3_rank": None} for e in entries
            if e["num"] not in set(mark_nums)]
        candidates, rank_by_num = select_frames(race, entries, snap)
        pool = [c["num"] for c in candidates if c["frames"]]
        counts = {l: sum(l in c["frames"] for c in candidates) for l in LABELS}
        if len(pool) < 3:
            p_reason, bets = "insufficient_unique_frame_candidates", []
        elif counts["A"] == 0:
            p_reason, bets = "no_A_frame", []
        elif counts["B"] + counts["C"] == 0:
            p_reason, bets = "no_B_or_C_frame", []
        else:
            bets = tickets(rank_by_num)
            p_reason = None if bets else "no_diverse_trio"
    # 記録は snapshot の出走馬と照合されるので、candidates は snapshot.marks の馬だけ。
    # raw にだけいる取消・除外馬は summary に馬番を残す。
    candidates = [c for c in candidates if c["num"] in set(mark_nums)]
    raw_only = sorted(set(all_nums) - set(mark_nums))
    scratch_note = f"取消・除外: {sorted(scratched)}" if scratched else "取消・除外なし"
    if raw_only:
        scratch_note += f"（rawのみの取消・除外: {raw_only}）"
    action = "bet" if bets else "pass"
    rec = {
        "schema_version": "chat-experiment-record-v1",
        "experiment_id": "scenario-frame",
        "rule_ref": {"path": "data/chat_experiments/scenario-frame/rule_v1.md", "version": "v1"},
        "race_id": race_id,
        "created_at": now.isoformat(timespec="seconds"),
        "author": author,
        "snapshot_ref": {"path": f"data/snapshots/{race_id}.json", "frozen_at": snapshot["frozen_at"]},
        "baseline_char": "gen", "budget": 500,
        "action": action, "pass_reason": p_reason,
        "candidates": candidates, "bets": bets,
        "summary": f"固定ルールv1。源さん別比較。raw Git SHA={git_sha[:12]}。発走前取消確認済（{scratch_note}）。"
    }
    return rec


# ---------------------------------------------------------------- 入出力（A/B/C と買い目のロジックは上のまま）
# raw と snapshot は、main 上でその snapshot を最後に更新した同じコミットから git で読む（rule §2.1）。
# 記録は正規の置き場所にだけ書き、同じレースの記録が作業ツリーにも main の履歴にも無いときだけ書く（rule §2.8・§7）。
RACE_ID = re.compile(r"\d{8}-[a-z]+-\d{2}")
RECORD_ROOT = "data/chat_experiments/scenario-frame/races"


def git(repo, *args):
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, timeout=60)
    if out.returncode != 0:
        raise ValueError(f"git {' '.join(args)} が失敗: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout


def week_id_of(race_id):
    year, week, _ = dt.date(int(race_id[:4]), int(race_id[4:6]), int(race_id[6:8])).isocalendar()
    return f"{year}-W{week:02d}"


def load_inputs(repo, race_id, main_ref, git_sha=None):
    """(raw, snapshot, 使ったコミット, main 上でその snapshot を最後に更新したコミット)。両ファイルとも同じコミットから読む。"""
    snap_path = f"data/snapshots/{race_id}.json"
    latest = git(repo, "log", "-1", "--format=%H", main_ref, "--", snap_path).decode().strip()
    if not latest:
        raise ValueError(f"{main_ref} に {snap_path} が無い")
    sha = git(repo, "rev-parse", "--verify", f"{git_sha or latest}^{{commit}}").decode().strip()
    if subprocess.run(["git", "merge-base", "--is-ancestor", sha, main_ref], cwd=repo).returncode != 0:
        raise ValueError(f"{sha[:12]} は {main_ref} の履歴に無い")
    raw = json.loads(git(repo, "show", f"{sha}:raw/{week_id_of(race_id)}.json"))
    snapshot = json.loads(git(repo, "show", f"{sha}:{snap_path}"))
    return raw, snapshot, sha, latest


def existing_records(repo, race_id, main_ref):
    """このレースの記録：作業ツリーにあるファイルと、main の履歴で触れたコミット（削除済みも含む）。"""
    rel = f"{RECORD_ROOT}/{race_id}"
    files = sorted(p.name for p in (Path(repo) / rel).glob("*.json"))
    commits = git(repo, "log", "--format=%H", main_ref, "--", rel).decode().split()
    return files, commits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, default=Path("."), help="リポジトリ（git で raw・snapshot を読む）")
    ap.add_argument("--main-ref", default="origin/main", help="main の参照。実行前に git fetch origin main")
    ap.add_argument("--race-id", required=True)
    ap.add_argument("--git-sha", help="監査の再実行用。省略時は main 上でその snapshot を最後に更新したコミット")
    ap.add_argument("--now", help="監査の再実行用（記録の created_at）。--write とは併用できない")
    ap.add_argument("--status-confirmed", action="store_true", help="I verified scratches against official pre-race status")
    ap.add_argument("--scratched", default="", help="Comma-separated numbers confirmed as scratched/excluded (empty if none)")
    ap.add_argument("--author", default="ChatGPT", help="Who generated this record")
    ap.add_argument("--write", action="store_true", help="正規の置き場所に記録を書く（省略時は標準出力に出すだけ）")
    args = ap.parse_args()
    if not RACE_ID.fullmatch(args.race_id):
        raise ValueError("race_id は 20261011-tokyo-11 の形")
    repo = args.repo.resolve()
    raw, snapshot, sha, latest = load_inputs(repo, args.race_id, args.main_ref, args.git_sha)
    if args.write and sha != latest:
        raise ValueError(f"--write は main 上の最新の snapshot のコミット {latest[:12]} でだけ書ける（指定 {sha[:12]}）")
    if args.write and args.now:
        raise ValueError("--now は監査の再実行用。--write とは併用できない")
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now(JST)
    scratched = [int(x) for x in args.scratched.split(",") if x.strip()]
    rec = make(raw, snapshot, args.race_id, sha, now, args.status_confirmed,
               scratched=scratched, author=args.author)
    payload = json.dumps(rec, ensure_ascii=False, indent=2) + "\n"
    if not args.write:
        print(payload, end="")
        return
    files, commits = existing_records(repo, args.race_id, args.main_ref)
    if files or commits:
        raise ValueError(f"{args.race_id} の記録が既にある（作業ツリー {files}、main の履歴 {len(commits)} コミット）。"
                         "1レース1件なので書かない")
    target = repo / RECORD_ROOT / args.race_id / f"{now.strftime('%Y%m%dT%H%M%S')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # A single canonical pre-race record may not be overwritten by a later run.
    with target.open("x", encoding="utf-8") as handle:
        handle.write(payload)
    print(f"written (immutable first draft): {target.relative_to(repo)}  inputs from {sha[:12]}")

if __name__ == "__main__":
    main()
