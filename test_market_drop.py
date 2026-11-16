#!/usr/bin/env python3
"""Offline market-drop checks: python3 test_market_drop.py."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import monitor


AT = datetime(2026, 11, 16, 14, 10, tzinfo=monitor.TZ)
DATES = ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
SPX, NDX = "us.INX", "us.NDX"


def chart_payload(symbol=SPX, closes=None, dates=None, quote_time="2026-11-13 16:01:00"):
    prices = [100, 99, 98, 97, 96, 95] if closes is None else closes
    days = DATES if dates is None else dates
    quote = [""] * 35
    quote[30] = quote_time
    return {"code": 0, "data": {symbol: {
        "day": [[day, "100", str(close), "110", "80", "1000"] for day, close in zip(days, prices)],
        "qt": {symbol: quote},
    }}}


def drop_row(kind="spx500", end_date="2026-11-13", triggered=True):
    return {
        "kind": kind, "name": "标普500" if kind == "spx500" else "纳指100",
        "symbol": SPX if kind == "spx500" else NDX,
        "lookback_days": 5, "threshold_pct": 5,
        "start_date": "2026-11-06", "end_date": end_date,
        "start_close": 100, "end_close": 95 if triggered else 99,
        "change_pct": -5.0 if triggered else -1.0, "triggered": triggered,
    }


def drop_snapshot(rows=None):
    return {"ts": AT.isoformat(), "rows": [drop_row(), drop_row("nasdaq100")] if rows is None else rows, "errors": []}


@contextmanager
def isolated_state():
    """Every actual file write from the exercised code stays under a temporary directory."""
    with TemporaryDirectory(prefix="qdii-market-drop-test-") as tmp:
        root = Path(tmp)
        state_dir, daily_dir = root / "state", root / "daily"
        state_dir.mkdir()
        daily_dir.mkdir()
        with patch.multiple(monitor, STATE_DIR=state_dir, STATE_PATH=state_dir / "state.json",
                            ACK_PATH=state_dir / "ack.json", LOG_PATH=state_dir / "monitor.log",
                            DAILY_DIR=daily_dir, now_cn=lambda: AT), patch.object(monitor, "log"):
            yield root


def test_completed_closes():
    pairs = monitor.completed_index_closes(chart_payload(), SPX, AT)
    assert pairs == list(zip(DATES, [100, 99, 98, 97, 96, 95]))
    # At 04:59 Beijing / 15:59 New York the current US daily candle is unfinished.
    dates, closes = [*DATES, "2026-11-16"], [100, 99, 98, 97, 96, 95, 94]
    before = datetime(2026, 11, 17, 4, 59, tzinfo=monitor.TZ)
    raw = chart_payload(closes=closes, dates=dates, quote_time="2026-11-16 15:59:00")
    assert "2026-11-16" not in dict(monitor.completed_index_closes(raw, SPX, before))
    after = datetime(2026, 11, 17, 5, 1, tzinfo=monitor.TZ)
    raw["data"][SPX]["qt"][SPX][30] = "2026-11-16 16:00:00"
    assert monitor.completed_index_closes(raw, SPX, after)[-1] == ("2026-11-16", 94)
    # Data that cannot be parsed may be rejected or excluded, but never returned as valid.
    for bad in (0, -1, "nan", "inf", "not-a-number"):
        raw = chart_payload(closes=[100, 99, 98, 97, 96, bad])
        try:
            accepted = monitor.completed_index_closes(raw, SPX, AT)
        except (ValueError, RuntimeError):
            continue
        assert "2026-11-13" not in dict(accepted), f"Invalid close accepted: {bad}"
    raw = chart_payload(dates=[*DATES[:-1], DATES[-2]])
    try:
        accepted = monitor.completed_index_closes(raw, SPX, AT)
    except (ValueError, RuntimeError):
        pass
    else:
        assert len(accepted) == len({date for date, _ in accepted}), "Duplicate dates accepted"
    invalid_payloads = [
        chart_payload(quote_time="2026-11-13 15:59:00"),  # 周一也不能把上周五未收盘缓存当收盘。
        chart_payload(quote_time="2026-11-12 16:00:00"),  # 日线晚于行情时间。
        chart_payload(closes=[100, 99, 98, 97, 96], dates=DATES[:-1]),  # 最新已收盘日缺失。
        chart_payload(closes=[100], dates=["2026-11-06"], quote_time="2026-11-06 16:00:00"),
        chart_payload(quote_time="2026-11-16 15:00:00"),  # 相对于测试采集时刻的未来行情。
    ]
    for raw in invalid_payloads:
        try:
            monitor.completed_index_closes(raw, SPX, AT)
        except (ValueError, RuntimeError):
            pass
        else:
            raise AssertionError("Stale or inconsistent daily data accepted")


def test_snapshot_and_partial_failure():
    raw = chart_payload()
    raw["data"].update(chart_payload(NDX)["data"])
    with isolated_state(), patch.object(monitor, "http_get_retry", return_value=json.dumps(raw)):
        snap = monitor.build_market_drop_snapshot({}, AT)
    assert len(snap["rows"]) == 2 and not snap["errors"]
    for row in snap["rows"]:
        assert row["lookback_days"] == 5 and row["start_date"] == DATES[0] and row["end_date"] == DATES[-1]
        assert abs(row["change_pct"] + 5) < 1e-9 and row["triggered"]
        assert row["threshold_pct"] == 5
    # One missing index source and one valid index: a failed source cannot swallow its sibling.
    with isolated_state(), patch.object(monitor, "http_get_retry", return_value=json.dumps(chart_payload())):
        snap = monitor.build_market_drop_snapshot({}, AT)
    assert [r["symbol"] for r in snap["rows"]] == [SPX] and snap["errors"]
    # Six closes are necessary: five observations contain only four daily return intervals.
    too_short = chart_payload(closes=[100, 99, 98, 97, 95], dates=DATES[-5:])
    with isolated_state(), patch.object(monitor, "http_get_retry", return_value=json.dumps(too_short)):
        snap = monitor.build_market_drop_snapshot({}, AT)
    assert not snap["rows"] and snap["errors"]
    # Non-default threshold is honored without changing either existing premium threshold.
    with isolated_state(), patch.object(monitor, "http_get_retry", return_value=json.dumps(raw)):
        snap = monitor.build_market_drop_snapshot({"spx_weekly_drop_threshold_pct": 6}, AT)
    assert not next(r for r in snap["rows"] if r["kind"] == "spx500")["triggered"]
    assert next(r for r in snap["rows"] if r["kind"] == "nasdaq100")["triggered"]
    with isolated_state(), patch.object(monitor, "http_get_retry", return_value=json.dumps(raw)):
        snap = monitor.build_market_drop_snapshot({"market_drop_lookback_days": 3}, AT)
    assert all(r["start_date"] == DATES[-4] and r["lookback_days"] == 3 for r in snap["rows"])


def test_episodes():
    state = {}
    first = monitor.market_drop_alert_groups(drop_snapshot(), state)
    assert len(first) == 2 and len({item[0] for item in first}) == 2
    original_ids = [item[0] for item in first]
    repeated = monitor.market_drop_alert_groups(drop_snapshot([
        drop_row(end_date="2026-11-16"), drop_row("nasdaq100", end_date="2026-11-16"),
    ]), state)
    assert [item[0] for item in repeated] == original_ids
    previous = deepcopy(state["market_drop_episodes"])
    monitor.market_drop_alert_groups({"ts": AT.isoformat(), "rows": [], "errors": ["source failed"]}, state)
    assert state["market_drop_episodes"] == previous, "A data failure incorrectly reset an episode"
    monitor.market_drop_alert_groups(drop_snapshot([drop_row(end_date="2026-11-16", triggered=False)]), state)
    retriggered = monitor.market_drop_alert_groups(drop_snapshot([drop_row(end_date="2026-11-17")]), state)
    assert len(retriggered) == 1 and retriggered[0][0] not in original_ids
    assert "2026-11-17" in retriggered[0][0]


def test_daily_failure_isolation_and_ack():
    with isolated_state(), patch.object(monitor, "check_daily_premium", side_effect=RuntimeError("premium offline")), \
            patch.object(monitor, "build_market_drop_snapshot", return_value=drop_snapshot()), \
            patch.object(monitor, "spawn_nag_queue") as send:
        monitor.cmd_daily({})
        assert send.call_count == 1 and len(send.call_args.args[0]) == 2
        groups = send.call_args.args[0]
        acknowledged = {item[0] for item in groups}
        send.reset_mock()
        with patch.object(monitor, "is_acked", side_effect=lambda alert_id: alert_id in acknowledged):
            monitor.cmd_daily({})
        assert not send.called, "Acknowledged ongoing episodes were sent again"
    with isolated_state(), patch.object(monitor, "check_daily_premium", return_value=[("premium", "p", "p")]), \
            patch.object(monitor, "build_market_drop_snapshot", side_effect=RuntimeError("index offline")), \
            patch.object(monitor, "spawn_nag_queue") as send:
        monitor.cmd_daily({})
        assert send.call_args.args[0] == [("premium", "p", "p")]


def test_existing_premium_record_and_dry_run():
    with isolated_state(), patch.object(monitor, "build_market_drop_snapshot", return_value=drop_snapshot()), \
            patch.object(monitor, "spawn_nag_queue") as send, patch.object(monitor, "format_table", return_value=""):
        # Exercise the real premium helper's existing-record path without fetching any real quotes.
        premium = {"trade_date": "2026-11-16", "ts": AT.isoformat(), "rows": [], "hits": [],
                   "spx_threshold": 3, "threshold": 5}
        monitor.daily_json_path("2026-11-16").write_text(json.dumps(premium))
        with patch.object(monitor, "build_snapshot", return_value=premium):
            monitor.cmd_daily({})
        assert send.call_count == 1 and len(send.call_args.args[0]) == 2
    with isolated_state(), patch.object(monitor, "build_market_drop_snapshot", return_value=drop_snapshot()), \
            patch.object(monitor, "check_daily_premium", return_value=[]), patch.object(monitor, "spawn_nag_queue") as send:
        initial = {"market_drop_episodes": {"keep": {"id": "existing"}}, "last_shares": {}}
        monitor.save_state(initial)
        monitor.cmd_daily({}, alert=False)
        assert monitor.load_state() == initial, "Dry run consumed or changed an episode"
        assert not send.called


def test_queue_merge_and_live_consumer():
    old = {"id": "old", "title": "old title", "body": "old body"}
    new = {"id": "new", "title": "new title", "body": "new body"}
    done = {"id": "done", "title": "done", "body": "done"}
    with isolated_state(), patch.object(monitor, "is_acked", side_effect=lambda alert_id: alert_id == "done"), \
            patch.object(monitor.subprocess, "Popen", return_value=SimpleNamespace(pid=123456)) as start, \
            patch.object(monitor.os, "kill", return_value=None):
        path = monitor.STATE_DIR / "nag-queue.json"
        (monitor.STATE_DIR / "nag.pid").write_text("123456")
        monitor.save_json(path, [old, done])
        monitor.spawn_nag_queue([("new", "new title", "new body"), ("old", "old title", "old body")])
        queued = monitor.load_json(path, [])
        assert [item["id"] for item in queued] == ["old", "new"]
        assert not start.called, "An active queue spawned a second popup consumer"
    with patch.object(monitor, "spawn_nag_queue") as enqueue:
        monitor.spawn_nag("ordinary", "ordinary title", "ordinary body")
        enqueue.assert_called_once_with([("ordinary", "ordinary title", "ordinary body")])
    with isolated_state():
        path = monitor.STATE_DIR / "nag-queue.json"
        monitor.save_json(path, [old])
        acknowledged, seen = set(), []

        def confirm(_cfg, alert_id, _title, _body):
            seen.append(alert_id)
            assert len(seen) <= 2, "Queue consumer did not skip acknowledged items"
            acknowledged.add(alert_id)
            if alert_id == "old":
                # Another job appends an alert while the old dialog is still being handled.
                monitor.save_json(path, [old, new])

        with patch.object(monitor, "is_acked", side_effect=lambda alert_id: alert_id in acknowledged), \
                patch.object(monitor, "nag_until_ack", side_effect=confirm):
            monitor.cmd_nag_queue({})
        assert seen == ["old", "new"], "A newly queued alert was lost after confirming the old dialog"


if __name__ == "__main__":
    test_completed_closes()
    test_snapshot_and_partial_failure()
    test_episodes()
    test_daily_failure_isolation_and_ack()
    test_existing_premium_record_and_dry_run()
    test_queue_merge_and_live_consumer()
    print("market drop checks ok")
