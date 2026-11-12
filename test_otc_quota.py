#!/usr/bin/env python3
from datetime import datetime

from monitor import (
    agency_from_purchase_row,
    zenity_markup,
    channel_hot,
    channel_meets,
    format_otc_alert,
    in_quota_release_window,
    is_quota_notice_title,
    merge_channel_limits,
    parse_notice_limits,
    pick_hot_institutions,
)


def test_title_filter() -> None:
    assert is_quota_notice_title("关于调整大额申购的公告")
    assert is_quota_notice_title("I类基金份额申购、定投及转换转入业务金额限制的公告")
    assert not is_quota_notice_title("参加中原证券认申购费率优惠的公告")
    assert not is_quota_notice_title("新增万联证券为代销机构的公告")


def test_parse_dacheng() -> None:
    text = (
        "投资人通过本公司直销渠道申购累计金额应不超过100元人民币；"
        "投资人通过各代销机构申购累计金额应不超过10元人民币。"
    )
    got = parse_notice_limits(text)
    assert got["direct"] == 100
    assert got["agency"] == 10


def test_parse_wanjia() -> None:
    text = (
        "代销渠道单日单个基金账户申购累计金额限制调整为10元（不含10元）；"
        "直销渠道单日单个基金账户申购累计金额限制仍为100元（不含100元）。"
    )
    got = parse_notice_limits(text)
    assert got["direct"] == 100
    assert got["agency"] == 10


def test_parse_jianxin() -> None:
    text = (
        "人民币份额投资者累计高于10元的申购进行限制；"
        "针对在建信基金直销渠道投资人民币份额，累计申购高于50元，管理人有权拒绝高于50元的部分。"
    )
    got = parse_notice_limits(text)
    assert got["direct"] == 50


def test_parse_nanfang_i() -> None:
    text = "下属基金份额的代码021000该基金份额的限制金额200元。调整后限额200元。"
    got = parse_notice_limits(text)
    assert got["generic"] == 200
    direct, agency = merge_channel_limits(0.0, False, got)
    assert direct == 200
    assert agency == 0


def test_agency_and_threshold() -> None:
    lim, opened, _ = agency_from_purchase_row("限大额", "10.0")
    assert lim == 10 and opened
    assert not channel_meets(lim, 100)
    assert channel_meets(100, 100)
    assert not channel_meets(None, 100)
    assert not channel_hot(500)
    assert channel_hot(500.01)
    assert channel_hot(1e12)
    assert not channel_hot(None)
    snap = {
        "hits": [
            {"direct_limit": 200, "agency_limit": 10},
            {"direct_limit": 1000, "agency_limit": 10},
        ]
    }
    assert [h["direct_limit"] for h in pick_hot_institutions(snap)] == [1000]


def test_zenity_markup() -> None:
    assert zenity_markup("溢价 < 5%") == "溢价 &lt; 5%"


def test_kinds_and_split_alerts() -> None:
    from monitor import classify_name, pick_premium_hits, premium_alert_groups
    assert classify_name("大成标普500等权重指数(QDII)A人民币") == "spx500ew"
    assert classify_name("大成标普500等权重指数(QDII)A美元") is None
    assert classify_name("长信标普100等权重指数人民币") is None
    assert classify_name("易方达标普500指数人民币A") == "spx500"
    assert classify_name("纳指ETF嘉实") == "nasdaq100"
    cfg = {"premium_threshold_pct": 5, "spx_premium_threshold_pct": 3}
    rows = [
        {"kind": "spx500", "premium": 2.5, "trade_date": "2026-11-06", "code": "513500", "name": "标普500ETF博时"},
        {"kind": "spx500ew", "premium": 2.9, "trade_date": "2026-11-06", "code": "096001", "name": "大成标普500等权重A"},
        {"kind": "spx500", "premium": 3.0, "trade_date": "2026-11-06", "code": "159655", "name": "标普500ETF华夏"},
        {"kind": "nasdaq100", "premium": 4.5, "trade_date": "2026-11-06", "code": "513100", "name": "纳指ETF国泰"},
        {"kind": "nasdaq100", "premium": 5.0, "trade_date": "2026-11-06", "code": "159941", "name": "纳指ETF广发"},
    ]
    hits = pick_premium_hits(rows, "2026-11-06", cfg)
    assert [h["code"] for h in hits] == ["513500", "096001", "513100"]
    groups = premium_alert_groups({"trade_date": "2026-11-06", "hits": hits, "threshold": 5, "spx_threshold": 3})
    assert [g[1] for g in groups] == ["标普溢价<3% 2只", "纳指100溢价<5% 1只"]
    assert "513500" in groups[0][2] and "096001" in groups[0][2] and "159655" not in groups[0][2]
    assert groups[1][2].startswith("513100")


def test_morning_alert_is_quota_only() -> None:
    title, body = format_otc_alert(
        {
            "trade_date": "2026-10-15",
            "min_limit": 100,
            "hits": [{
                "manager": "南方", "kind": "nasdaq100",
                "direct_limit": 200, "agency_limit": 10,
                "direct_code": "021000", "agency_code": "016452",
            }],
            "rows": [{"kind": "nasdaq100"}, {"kind": "spx500"}, {"kind": "spx500ew"}],
            "ts": "t",
        },
    )
    assert title == "场外可买1家"
    assert "纳指100 南方 直销200元 代销10元" in body
    assert "标普等权 1只/可买0家" in body and "标普500 1只/可买0家" in body
    assert "161125" not in body


def test_release_window() -> None:
    assert in_quota_release_window(datetime(2026, 10, 13, 9, 0))
    assert in_quota_release_window(datetime(2026, 10, 13, 10, 30))
    assert not in_quota_release_window(datetime(2026, 10, 13, 8, 59))
    assert not in_quota_release_window(datetime(2026, 10, 13, 10, 40))
    assert not in_quota_release_window(datetime(2026, 10, 17, 9, 10))


if __name__ == "__main__":
    test_title_filter()
    test_parse_dacheng()
    test_parse_wanjia()
    test_parse_jianxin()
    test_parse_nanfang_i()
    test_agency_and_threshold()
    test_zenity_markup()
    test_kinds_and_split_alerts()
    test_morning_alert_is_quota_only()
    test_release_window()
    print("ok")
