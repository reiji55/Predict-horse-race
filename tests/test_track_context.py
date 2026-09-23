from __future__ import annotations

from scraper import capture_track_context
from scraper.fetchers import g_jra_track


HTML = """
<html>
<head><title>馬場情報（中山競馬場） JRA</title></head>
<body>
<h2>2026年9月26日（土曜）</h2>
<h3>芝のクッション値</h3>
<div><select><option>8.0</option><option selected>9.3</option><option>10.0</option></select></div>
<div>7:30</div>
<h3>含水率</h3>
<table>
<tr><th></th><th>ゴール前</th><th>4コーナー</th></tr>
<tr><th>芝</th><td>12.3%</td><td>13.1%</td></tr>
<tr><th>ダート</th><td>5.8%</td><td>6.2%</td></tr>
</table>
</body>
</html>
"""


def test_jra_track_parser_reads_only_clear_quantitative_values():
    parsed = g_jra_track.parse_baba_html(HTML, "https://www.jra.go.jp/keiba/baba/")
    assert parsed["venue"] == "中山"
    assert parsed["date"] == "2026-09-26"
    assert parsed["cushion"]["value"] == 9.3
    assert parsed["cushion"]["measured_at"] == "7:30"
    assert parsed["moisture"]["芝"] == {"goal":12.3, "turn4":13.1}
    assert parsed["moisture"]["ダート"] == {"goal":5.8, "turn4":6.2}


def test_track_context_attaches_only_same_date_and_venue():
    raw = {"races":[
        {"id":"20260926-nakayama-11","date":"2026-09-26","venue":"中山"},
        {"id":"20260926-hanshin-11","date":"2026-09-26","venue":"阪神"},
    ]}
    metrics = {
        "中山":{
            "venue":"中山","date":"2026-09-26",
            "cushion":{"value":9.3},"moisture":{},
        },
        "阪神":{
            "venue":"阪神","date":"2026-09-25",
            "cushion":{"value":8.8},"moisture":{},
        },
    }
    out = capture_track_context.attach(raw, ["2026-09-26"], metrics)
    assert out["races"][0]["track_metrics"]["cushion"]["value"] == 9.3
    assert "track_metrics" not in out["races"][1]
    assert out["track_context_report"]["captured"] == ["20260926-nakayama-11"]
    assert out["track_context_report"]["skipped"][0]["reason"] == "date_mismatch"


def test_ambiguous_dates_and_duplicate_moisture_rows_become_null():
    """前日測定などで日付・含水率が複数あると断定しない。推測値より null。"""
    html = HTML.replace(
        "<h3>含水率</h3>",
        "<p>2026年9月25日（金曜）測定</p><h3>含水率</h3>",
    ).replace(
        "<tr><th>芝</th><td>12.3%</td><td>13.1%</td></tr>",
        "<tr><th>芝</th><td>12.3%</td><td>13.1%</td></tr>"
        "<tr><th>芝</th><td>14.0%</td><td>15.2%</td></tr>",
    )
    parsed = g_jra_track.parse_baba_html(html)
    assert parsed["date"] is None
    assert "芝" not in parsed["moisture"]
    assert parsed["moisture"]["ダート"] == {"goal": 5.8, "turn4": 6.2}


def test_unknown_page_date_is_not_attached():
    raw = {"races": [{"id": "20260926-nakayama-11", "date": "2026-09-26", "venue": "中山"}]}
    metrics = {"中山": {"venue": "中山", "date": None, "cushion": {"value": 9.3}, "moisture": {}}}
    out = capture_track_context.attach(raw, ["2026-09-26"], metrics)
    assert "track_metrics" not in out["races"][0]
    assert out["track_context_report"]["skipped"][0]["reason"] == "date_unknown"
