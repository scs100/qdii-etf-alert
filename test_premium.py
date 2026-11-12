#!/usr/bin/env python3
"""Run with python3 test_premium.py; no network, notifications, or real state writes."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import monitor


def quote_blob(code="161125", nav="3.1295", iopv=""):
    fields = [""] * 88
    for i, value in {1: "标普500LOF", 2: code, 3: "3.183", 30: "20261106141730",
                     77: "1.71", 78: iopv, 81: nav}.items():
        fields[i] = value
    return 'v_sz' + code + '="' + "~".join(fields) + '";'


def test_premium():
    q = monitor.parse_tencent_quote(quote_blob())
    assert round(q["computed_premium"], 2) == 1.71  # 用户截图的旧净值口径。
    b = {"nav": 3.1295, "nav_date": "2026-11-04"}
    estimate = {"nav": 2.9992, "date": "2026-11-05", "updated_at": "2026-11-06 14:17:16"}
    r = monitor.premium_metrics(q, b, "LOF", estimate)
    assert round(r["premium"], 2) == 6.13 and r["premium"] > 5
    assert round(r["nav_premium"], 2) == 1.71
    assert r["premium_basis"] == "估算溢价" and r["premium_date"] == "2026-11-05"
    for bad in ({}, {**estimate, "date": "2026-11-04"},
                {**estimate, "updated_at": "2026-11-05 14:17:16"}):
        assert monitor.premium_metrics(q, b, "LOF", bad)["premium"] is None
    official = monitor.premium_metrics(q, {"nav": 2.9991, "nav_date": "2026-11-05"}, "LOF", estimate)
    assert official["premium_basis"] == "净值溢价"
    assert official["nav"] == 2.9991 and official["nav_date"] == "2026-11-05"
    assert round(official["premium"], 2) == 6.13
    assert monitor.premium_metrics(q, {}, "LOF", {})["nav_date"] is None
    assert monitor.premium_metrics(q, {}, "LOF", {})["premium"] is None
    etf = monitor.parse_tencent_quote(quote_blob("513500", nav="2.3353", iopv="2.3371"))
    assert etf["nav"] == 2.3353 and etf["iopv"] == 2.3371
    result = monitor.premium_metrics(etf, {}, "ETF", {})
    assert result["premium"] == monitor.fair_premium(3.183, 2.3371)
    assert result["premium_basis"] == "IOPV溢价"
    assert monitor.premium_metrics(q, b, "ETF", {})["premium"] is None
    assert monitor.parse_tencent_quote(quote_blob(nav="", iopv="2.3371"))["nav"] is None
    for bad in (0, -1, float("nan"), float("inf"), None):
        assert monitor.fair_premium(3.183, bad) is None
    assert monitor.previous_weekday("2026-11-09") == "2026-11-06"


def test_estimate_parser():
    raw = """<p>数据更新时间：2026-11-06 14:17:16</p>
    <table><tr><th>代码</th><th>实时<br>估值</th><!--<th>停用列</th>-->
    <th>最新<br />估值</th><th>估值<br />日期</th></tr>
    <tr><td>161125</td><td>3.0153</td><!--<td>坏数据</td>--><td>2.9992</td><td>11-05</td></tr></table>"""
    parsed = monitor.parse_lof_estimate(raw, "161125")
    assert parsed["nav"] == 2.9992 and parsed["date"] == "2026-11-05"
    year_end = raw.replace("2026-11-06", "2027-01-01").replace("11-05", "12-31")
    assert monitor.parse_lof_estimate(year_end, "161125")["date"] == "2026-12-31"
    for bad in (raw.replace("2.9992", "-"), raw.replace("161125", "161130"), "错误页"):
        try:
            monitor.parse_lof_estimate(bad, "161125")
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid estimate accepted")


def test_alert_flow():
    fund = {"code": "161125", "name": "易方达标普500指数人民币A", "kind": "spx500", "listed_type": "LOF"}
    q = monitor.parse_tencent_quote(quote_blob())
    with patch.multiple(monitor,
                        load_fund_universe=lambda: [fund],
                        fetch_tencent_quotes=lambda _: {"161125": q},
                        fetch_nav_backup=lambda _: {"161125": {"nav": 3.1295, "nav_date": "2026-11-04"}},
                        fetch_lof_estimates=lambda _: {"161125": {"nav": 2.9992, "date": "2026-11-05", "updated_at": "2026-11-06 14:17:16"}},
                        fetch_us_ref=lambda: {}, fetch_trade_status_many=lambda _: {},
                        load_state=lambda: {}, save_state=lambda _: None):
        snap = monitor.build_snapshot({"premium_threshold_pct": 5})
    assert snap["hits"] == [] and round(snap["rows"][0]["premium"], 2) == 6.13
    table = monitor.format_table(snap)
    assert "6.13%" in table and "1.71%" in table and "2026-11-05" in table
    groups = monitor.premium_alert_groups({**snap, "hits": snap["rows"], "spx_threshold": 3, "threshold": 5})
    assert len(groups) == 1 and groups[0][1].startswith("标普溢价<3%")
    assert "161125" in groups[0][2] and "6.13%" in groups[0][2] and "怎么买" not in groups[0][2]
    # 过期记录和旧口径记录均不能作为今天的低溢价机会。
    with TemporaryDirectory() as tmp, patch.object(monitor, "DAILY_DIR", Path(tmp)):
        path = Path(tmp) / "2026-11-06.json"
        path.write_text(json.dumps({**snap, "hits": snap["rows"]}))
        assert monitor.load_latest_premium("2026-11-09")["hits"]
        stale = monitor.load_latest_premium("2026-11-10")
        assert stale["hits"] == [] and stale["stale"]
        _, body = monitor.format_otc_alert({"trade_date": "2026-11-10", "rows": [], "hits": [], "min_limit": 100, "ts": "t"})
        assert "6.13%" not in body and "标普等权 0只/可买0家" in body
        path.write_text(json.dumps({**snap, "premium_method": "old", "hits": snap["rows"]}))
        assert monitor.load_latest_premium("2026-11-09")["hits"] == []


if __name__ == "__main__":
    test_premium()
    test_estimate_parser()
    test_alert_flow()
    print("premium checks ok")
