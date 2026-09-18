"""
週のレース結果取得（results/fetch_week_results.py）のテスト。**ネットワーク非依存**。

ここが繋がっていないと results.json が永遠に作られず、後方検証のデータが1件も溜まらない。
「まだ結果が出ていないレースを飛ばす」「取得済みを取り直さない」「土日でマージする」の
3つが運用の肝なので、そこを固定する。
"""
import json

import pytest

from results import fetch_week_results as fwr


@pytest.fixture
def week_raw(tmp_path, monkeypatch):
    """raw/2026-W38.json を用意し、出力先も一時ディレクトリに向ける。"""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "2026-W38.json").write_text(json.dumps({
        "week_id": "2026-W38",
        "races": [
            {"id": "20260919-hanshin-11", "source_refs": {"netkeiba": "202609040511"}},
            {"id": "20260919-nakayama-11", "source_refs": {"netkeiba": "202606040511"}},
            {"id": "20260920-hanshin-11", "source_refs": None},   # refが無い行は無視される
        ],
    }, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(fwr, "RAW_DIR", raw_dir)
    monkeypatch.setattr(fwr, "OUTPUT_PATH", tmp_path / "data" / "race_results.json")
    return tmp_path


def _result(finish):
    return {"finish": finish, "dividends": {"ワイド": [], "馬連": [], "3連複": []}}


def test_load_races_skips_rows_without_a_source_ref(week_raw):
    races = fwr.load_races("2026-W38")
    assert races == [
        ("20260919-hanshin-11", "202609040511"),
        ("20260919-nakayama-11", "202606040511"),
    ]


def test_missing_raw_is_an_explicit_error(week_raw):
    with pytest.raises(FileNotFoundError, match="raw が見つかりません"):
        fwr.load_races("2026-W99")


def test_unfinished_races_are_skipped(week_raw, monkeypatch):
    """土曜の昼に回しても落ちない：着順が3頭に満たないレースは入れない。"""
    monkeypatch.setattr(fwr.f_results, "fetch_results",
                        lambda ref: _result([] if ref == "202609040511" else [4, 5, 8, 10]))

    collected = fwr.fetch_week("2026-W38")

    assert list(collected) == ["20260919-nakayama-11"]


def test_already_fetched_races_are_not_refetched(week_raw, monkeypatch):
    """取得済みのレースは叩き直さない（無駄なリクエストを投げない）。"""
    out = week_raw / "data" / "race_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"20260919-hanshin-11": _result([1, 2, 3])},
                              ensure_ascii=False), encoding="utf-8")

    calls = []

    def fake(ref):
        calls.append(ref)
        return _result([4, 5, 8])

    monkeypatch.setattr(fwr.f_results, "fetch_results", fake)
    collected = fwr.fetch_week("2026-W38")

    assert calls == ["202606040511"]              # 阪神は既にあるので叩かない
    assert len(collected) == 2                    # 既存はそのまま残る
    assert collected["20260919-hanshin-11"]["finish"] == [1, 2, 3]

    # --refetch 相当では取り直す
    calls.clear()
    fwr.fetch_week("2026-W38", skip_existing=False)
    assert len(calls) == 2


def test_fetch_failure_does_not_stop_the_rest(week_raw, monkeypatch):
    def fake(ref):
        if ref == "202609040511":
            raise RuntimeError("ネットワーク断を想定")
        return _result([4, 5, 8])

    monkeypatch.setattr(fwr.f_results, "fetch_results", fake)
    collected = fwr.fetch_week("2026-W38")

    assert list(collected) == ["20260919-nakayama-11"]


def test_output_feeds_build_results_directly(week_raw, monkeypatch):
    """出力がそのまま build_results の入力の形になっていること。"""
    from results import build_results

    monkeypatch.setattr(fwr.f_results, "fetch_results", lambda ref: {
        "finish": [4, 7, 8, 10],
        "dividends": {"ワイド": [{"horses": [4, 7], "pay": 610}], "馬連": [], "3連複": []},
    })
    collected = fwr.fetch_week("2026-W38")

    predictions = {"week_id": "2026-W38", "races": [{
        "id": "20260919-hanshin-11", "venue": "阪神", "race_no": 11, "name": "テスト",
        "cards": [{"char": "kei", "total": 200, "bets": [
            {"type": "ワイド", "horses": [7, 4], "amt": 200}]}],
    }]}
    results = build_results.build_results(predictions, collected)

    assert results["results"][0]["cards"][0]["payout"] == 1220   # 610 × 200/100
