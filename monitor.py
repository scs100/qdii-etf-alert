#!/usr/bin/env python3
"""国内场内纳斯达克100 / 标普500 ETF 溢价与额度监控。"""

from __future__ import annotations

import argparse
import csv
import fcntl
import html as htmlmod
import json
import os
import re
import smtplib
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG_PATHS = [
    Path.home() / ".config/qdii-etf-alert/config.json",
    ROOT / "config.json",
]
STATE_DIR = Path.home() / ".local/share/qdii-etf-alert"
STATE_PATH = STATE_DIR / "state.json"
ACK_PATH = STATE_DIR / "ack.json"
LOG_PATH = STATE_DIR / "monitor.log"
DAILY_DIR = ROOT / "daily"
OTC_ANN_CACHE = STATE_DIR / "otc_ann_cache.json"
OTC_ETF_PREFIX = ("15", "51", "52", "56", "58")
HOT_QUOTA_CNY = 500
HOT_DISMISS_PATH = STATE_DIR / "otc_hot_dismiss.json"
HOT_DISMISS_STOP = 3
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TZ = timezone(timedelta(hours=8))
ACK_PORT = 18765
SOUND = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga"
US_MARKETS = (
    ("spx500", "标普500", "us.INX", "spx_weekly_drop_threshold_pct"),
    ("nasdaq100", "纳指100", "us.NDX", "nasdaq_weekly_drop_threshold_pct"),
)

NASDAQ_INCLUDE = ("纳斯达克100", "纳指100", "纳指etf", "纳斯达克etf")
SPX_INCLUDE = ("标普500",)
NAME_EXCLUDE = (
    "科技",
    "生物",
    "医疗",
    "油气",
    "原油",
    "信息",
    "消费",
    "等权重",
    "精选",
    "美元",
    "现汇",
    "现钞",
    "美汇",
    "美钞",
)
LISTED_PREFIX = ("15", "16", "50", "51", "52", "56", "58")
MANAGERS = (
    "华泰柏瑞",
    "汇添富",
    "易方达",
    "嘉实",
    "大成",
    "华安",
    "招商",
    "广发",
    "国泰",
    "华夏",
    "博时",
    "富国",
    "南方",
    "摩根",
    "建信",
    "万家",
    "天弘",
    "宝盈",
)


def now_cn() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{now_cn():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for p in reversed(CONFIG_PATHS):
        if p.exists():
            cfg.update(load_json(p, {}))
    cfg.setdefault("premium_threshold_pct", 5.0)
    cfg.setdefault("min_daily_limit_cny", 100)
    cfg.setdefault("include_lof", True)
    cfg.setdefault("require_quota", False)
    cfg.setdefault("market_drop_lookback_days", 5)
    cfg.setdefault("spx_weekly_drop_threshold_pct", 5.0)
    cfg.setdefault("nasdaq_weekly_drop_threshold_pct", 5.0)
    cfg.setdefault("ntfy_topic", "")
    cfg.setdefault("contacts", {"phone": "", "email": ""})
    cfg.setdefault("smtp", {})
    cfg.setdefault("aliyun", {})
    return cfg


def http_get(url: str, timeout: int = 20, encoding: str = "utf-8", referer: str | None = None) -> str:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode(encoding, "replace")


def http_get_retry(url: str, **kwargs: Any) -> str:
    last: Exception | None = None
    for i in range(3):
        try:
            return http_get(url, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmlmod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def market_of(code: str) -> str:
    if code.startswith(("15", "16", "12", "18")):
        return "sz"
    return "sh"


def tencent_symbol(code: str) -> str:
    return f"{market_of(code)}{code}"


def to_float(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except ValueError:
        return None


def is_listed_code(code: str) -> bool:
    return len(code) == 6 and code.isdigit() and code.startswith(LISTED_PREFIX)


def kind_label(kind: str) -> str:
    return {"nasdaq100": "纳指100", "spx500": "标普500", "spx500ew": "标普等权"}.get(kind, kind)


def premium_limit(kind: str, cfg: dict[str, Any]) -> float:
    if kind in ("spx500", "spx500ew"):
        return float(cfg.get("spx_premium_threshold_pct", 3))
    return float(cfg.get("premium_threshold_pct", 5))


def classify_name(name: str) -> str | None:
    n = name.replace(" ", "").replace("（", "(").replace("）", ")")
    compact = n.lower()
    if any(x in n for x in ("美元", "现汇", "现钞", "美汇", "美钞")):
        return None
    if any(k in compact for k in NASDAQ_INCLUDE):
        if any(bad in n and "纳斯达克100" not in n for bad in ("科技", "生物", "医疗", "精选")):
            return None
        return "nasdaq100"
    if "标普500" in n and "等权" in n:
        return "spx500ew"
    if any(k in n for k in SPX_INCLUDE):
        if any(bad in n for bad in ("信息", "医疗", "生物", "油气", "消费")):
            return None
        return "spx500"
    return None


def pick_premium_hits(rows: list[dict[str, Any]], trade_date: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        r
        for r in rows
        if r.get("premium") is not None
        and r["premium"] < premium_limit(str(r.get("kind") or ""), cfg)
        and r.get("trade_date") == trade_date
    ]


def load_fund_universe() -> list[dict[str, str]]:
    raw = http_get_retry(
        "https://fund.eastmoney.com/js/fundcode_search.js",
        referer="https://fund.eastmoney.com/",
    )
    match = re.search(r"\[\[.*\]\]", raw, re.S)
    if not match:
        raise RuntimeError("无法解析天天基金代码表")
    rows = json.loads(match.group(0))
    funds = []
    for row in rows:
        code, _py, name, ftype = row[0], row[1], row[2], row[3] if len(row) > 3 else ""
        funds.append({"code": code, "name": name, "ftype": ftype})
    return funds


def discover_targets(funds: list[dict[str, str]], include_lof: bool) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for fund in funds:
        code, name = fund["code"], fund["name"]
        kind = classify_name(name)
        if not kind or not is_listed_code(code) or code in seen:
            continue
        if "联接" in name and not code.startswith(("15", "16", "50", "51", "52")):
            continue
        if code.startswith("16") and not include_lof:
            continue
        listed_type = "LOF" if code.startswith(("16", "50")) else "ETF"
        out.append({"code": code, "name": name, "kind": kind, "listed_type": listed_type})
        seen.add(code)
    out.sort(key=lambda x: (x["kind"], x["listed_type"], x["code"]))
    return out


def manager_of(name: str) -> str | None:
    for mgr in sorted(MANAGERS, key=len, reverse=True):
        if mgr in name:
            return mgr
    return None


def find_feeders(etf: dict[str, str], funds: list[dict[str, str]]) -> list[dict[str, str]]:
    manager = manager_of(etf["name"])
    if etf["kind"] == "nasdaq100":
        index_keys = ("纳斯达克100", "纳指100", "纳指")
    elif etf["kind"] == "spx500ew":
        index_keys = ("标普500等权",)
    else:
        index_keys = ("标普500",)
    found: list[dict[str, str]] = []
    for fund in funds:
        n = fund["name"]
        if "联接" not in n:
            continue
        if etf["kind"] != "spx500ew" and "等权" in n:
            continue
        if manager and manager not in n:
            continue
        if not any(k in n for k in index_keys):
            continue
        if etf["kind"] == "nasdaq100" and any(x in n for x in ("科技", "生物", "医疗")):
            continue
        if any(x in n for x in ("美元", "现汇", "现钞", "美汇", "美钞")):
            continue
        found.append(fund)
    if not found and manager:
        for fund in funds:
            n = fund["name"]
            code = fund["code"]
            if code == etf["code"] or is_listed_code(code) and code.startswith(("15", "51", "52", "56", "58")):
                continue
            if etf["kind"] != "spx500ew" and "等权" in n:
                continue
            if manager not in n:
                continue
            if not any(k in n for k in index_keys):
                continue
            if any(x in n for x in ("美元", "现汇", "现钞", "美汇", "美钞", "科技", "生物", "医疗")):
                continue
            found.append(fund)
    # Prefer A / 人民币 share classes first.
    def rank(item: dict[str, str]) -> tuple[int, str]:
        n = item["name"]
        score = 0
        if n.endswith("A") or "人民币A" in n or "(QDII)A" in n:
            score -= 2
        if "C" in n[-3:]:
            score += 2
        return (score, item["code"])

    found.sort(key=rank)
    # unique by code
    uniq, seen = [], set()
    for item in found:
        if item["code"] in seen:
            continue
        uniq.append(item)
        seen.add(item["code"])
    return uniq[:6]


def parse_tencent_quote(blob: str) -> dict[str, Any] | None:
    inner = blob.split('="', 1)[-1].rstrip('";\n ')
    if not inner or inner.startswith("1~~"):
        return None
    p = inner.split("~")
    if len(p) < 82:
        return None
    price = to_float(p[3])
    nav = to_float(p[81])
    tencent_premium = to_float(p[77])
    shares = to_float(p[72]) or to_float(p[73])
    name = p[1]
    code = p[2]
    quote_time = p[30] if len(p) > 30 else ""
    computed = None
    if price and nav and nav > 0:
        computed = (price / nav - 1.0) * 100.0
    return {
        "code": code,
        "name": name,
        "price": price,
        "prev_close": to_float(p[4]),
        "nav": nav,
        "iopv": to_float(p[78]),
        "tencent_premium": tencent_premium,
        "computed_premium": computed,
        "shares": shares,
        "quote_time": quote_time,
        "trade_date": parse_trade_date(quote_time),
        "change_pct": to_float(p[32]),
    }


def parse_trade_date(quote_time: str) -> str | None:
    text = str(quote_time or "").strip()
    m = re.match(r"(\d{4})(\d{2})(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    return None


def fetch_tencent_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(codes), 40):
        chunk = codes[i : i + 40]
        symbols = ",".join(tencent_symbol(c) for c in chunk)
        raw = http_get_retry(f"https://qt.gtimg.cn/q={symbols}", encoding="gbk")
        for line in raw.splitlines():
            if '="' not in line:
                continue
            q = parse_tencent_quote(line)
            if q and q.get("code"):
                out[q["code"]] = q
    return out


def fetch_nav_backup(codes: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    joined = ",".join(codes)
    url = (
        "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo"
        f"?pageIndex=1&pageSize=80&plat=Wap&appType=ttjj&product=EFund"
        f"&Version=1&deviceid=qdii-etf-alert&Fcodes={joined}"
    )
    try:
        data = json.loads(http_get_retry(url, referer="https://fund.eastmoney.com/"))
    except Exception as exc:  # noqa: BLE001
        log(f"天天基金净值备份失败: {exc}")
        return out
    for row in data.get("Datas") or []:
        code = row.get("FCODE")
        if not code:
            continue
        out[code] = {
            "name": row.get("SHORTNAME"),
            "nav": to_float(row.get("NAV")),
            "price": to_float(row.get("NEWPRICE")),
            "nav_date": row.get("PDATE"),
            "hq_date": row.get("HQDATE"),
        }
    return out


def previous_weekday(day: str) -> str:
    dt = datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)
    while dt.weekday() >= 5:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


def parse_lof_estimate(raw: str, code: str) -> dict[str, Any]:
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    stamp = re.search(r"数据更新时间[：:]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", raw)
    if not stamp:
        raise ValueError("估值更新时间缺失")
    updated = datetime.strptime(stamp[1], "%Y-%m-%d %H:%M:%S")
    for table in re.findall(r"<table\b[^>]*>(.*?)</table>", raw, re.S):
        headers = [strip_tags(h).replace(" ", "") for h in re.findall(r"<th\b[^>]*>(.*?)</th>", table, re.S)]
        if not {"代码", "最新估值", "估值日期"}.issubset(headers):
            continue
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.S):
            cells = [strip_tags(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)]
            if len(cells) != len(headers):
                continue
            data = dict(zip(headers, cells))
            if data.get("代码") != code:
                continue
            nav = to_float(data["最新估值"])
            if nav is None or not 0 < nav < float("inf"):
                raise ValueError("估值无效")
            date = datetime.strptime(f"{updated.year}-{data['估值日期']}", "%Y-%m-%d")
            if date.date() > updated.date():
                date = date.replace(year=date.year - 1)
            return {"nav": nav, "date": date.strftime("%Y-%m-%d"), "updated_at": stamp[1]}
    raise ValueError(f"未找到{code}估值")


def fetch_lof_estimates(codes: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for code in codes:
        try:
            raw = http_get_retry(f"https://www.haoetf.com/qdii/{code}", timeout=8)
            out[code] = parse_lof_estimate(raw, code)
        except Exception as exc:  # noqa: BLE001
            log(f"{code} LOF估值不可用: {exc}")
    return out


def premium_metrics(q: dict[str, Any], b: dict[str, Any], listed_type: str,
                    estimate: dict[str, Any]) -> dict[str, Any]:
    # 净值和净值日必须来自同一来源；腾讯的单位净值字段没有日期。
    nav = b.get("nav") or q.get("nav")
    nav_date = b.get("nav_date") if b.get("nav") else None
    price = q.get("price")
    static = fair_premium(price, nav)
    result = {"nav": nav, "nav_date": nav_date, "computed_premium": static,
              "nav_premium": static, "premium": None, "premium_nav": None,
              "premium_date": None, "premium_basis": "估值待更新"}
    trade_date = q.get("trade_date")
    if not trade_date:
        return result
    if listed_type == "ETF":
        value, date, basis = q.get("iopv"), trade_date, "IOPV溢价"
    else:
        expected = previous_weekday(trade_date)
        value, date, basis = None, None, "估值待更新"
        # ponytail: 未引入境外节假日日历；假日后无法核实估值时跳过提醒，需要覆盖时再接交易日历。
        if nav_date and expected <= nav_date <= trade_date:
            value, date, basis = nav, nav_date, "净值溢价"
        elif (estimate.get("date") == expected
              and str(estimate.get("updated_at") or "")[:10] >= trade_date):
            value, date, basis = estimate.get("nav"), estimate["date"], "估算溢价"
    premium = fair_premium(price, value)
    if premium is not None:
        result.update(premium=premium, premium_nav=value, premium_date=date, premium_basis=basis)
    return result


def parse_trade_status(html: str) -> dict[str, Any]:
    match = re.search(r"交易状态：</span>(.{0,500})", html, re.S)
    text = strip_tags(match.group(1)[:350]) if match else ""
    text = text.split("购买手续费")[0].strip(" |;，,")
    limit = None
    mlim = re.search(r"单日累计购买上限\s*([\d.]+)\s*元", html)
    if mlim:
        limit = to_float(mlim.group(1))
    return {"status_text": text, "daily_limit": limit}


def fetch_trade_status(code: str) -> dict[str, Any]:
    html = http_get_retry(f"https://fund.eastmoney.com/{code}.html", referer="https://fund.eastmoney.com/")
    parsed = parse_trade_status(html)
    parsed["code"] = code
    return parsed


def fetch_trade_status_many(codes: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    uniq = list(dict.fromkeys(codes))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch_trade_status, code): code for code in uniq}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                out[code] = fut.result()
            except Exception as exc:  # noqa: BLE001
                out[code] = {"code": code, "status_text": f"抓取失败:{exc}", "daily_limit": None}
    return out


def quota_from_status(status_text: str, daily_limit: float | None, min_limit: float) -> tuple[bool | None, str]:
    text = status_text or ""
    if "场内交易" in text:
        return None, "场内交易(额度看联接/一级申赎)"
    if "暂停" in text:
        extra = f"上限{daily_limit:g}元" if daily_limit is not None else ""
        return False, f"暂停申购{extra}"
    if "开放申购" in text:
        return True, "开放申购"
    if "限" in text:
        if daily_limit is None:
            return True, "限大额"
        if daily_limit >= min_limit:
            return True, f"限大额{daily_limit:g}元"
        return False, f"限额过小({daily_limit:g}元)"
    if not text:
        return None, "未知"
    return None, text[:40]


def fetch_eastmoney_purchase_rows() -> list[list[str]]:
    raw = http_get_retry(
        "https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx?t=8&page=1,50000&js=reData&sort=fcode,asc",
        referer="https://fund.eastmoney.com/",
    )
    idx = raw.find("datas:")
    if idx < 0:
        raise RuntimeError("无法解析天天基金申购状态表")
    rows, _ = json.JSONDecoder().raw_decode(raw, idx + 6)
    if not isinstance(rows, list):
        raise RuntimeError("天天基金申购状态表格式异常")
    return rows


def sane_cny_limit(value: Any) -> float | None:
    limit = to_float(value)
    if limit is None:
        return None
    if limit <= 0:
        return 0.0
    if limit >= 1e10:
        return None
    return limit


def agency_from_purchase_row(status: str, raw_limit: Any) -> tuple[float | None, bool, str]:
    limit = sane_cny_limit(raw_limit)
    text = status or ""
    if "暂停" in text:
        return 0.0, False, "暂停申购"
    if "开放申购" in text:
        if limit is None:
            return 1e12, True, "开放申购"
        return limit, True, f"开放申购上限{limit:g}元"
    if "限" in text:
        if limit is None or limit == 0:
            return 0.0, False, "限大额(代销未标额度)"
        return limit, True, f"限大额{limit:g}元"
    if not text:
        return None, False, "未知"
    return limit, False, text[:40]


def is_quota_notice_title(title: str) -> bool:
    if "费率" in title:
        return False
    return any(k in title for k in ("大额申购", "暂停申购", "限制申购", "金额限制", "申购限额"))


def _yuan(num: str, wan: str | None) -> float:
    value = float(str(num).replace(",", ""))
    return value * 10000.0 if wan else value


_AMT = r"([\d,.]+)(万)?元"
_LIMIT_VERB = r"(?:不超过|不高[于过]|应不超过|限制(?:调整)?为|仍为|限额为|高于|累计金额)"


def _channel_amount(compact: str, key: str) -> float | None:
    for match in re.finditer(key, compact):
        window = compact[match.start() : match.start() + 100]
        if "亿" in window:
            continue
        found = re.search(_LIMIT_VERB + r"[^\d]{0,12}" + _AMT, window)
        if not found:
            found = re.search(_AMT, window[len(key) :])
        if not found:
            continue
        value = _yuan(found.group(1), found.group(2))
        if 0 < value < 1e8:
            return value
    return None


def _generic_notice_limit(compact: str) -> float | None:
    for pat in (
        r"该(?:下属)?基金份额的限制金额[^\d]{0,8}" + _AMT,
        r"限制申购金额[^\d]{0,24}" + _AMT,
        r"调整后限额" + _AMT,
        r"限额" + _AMT,
    ):
        found = re.search(pat, compact)
        if not found:
            continue
        value = _yuan(found.group(1), found.group(2))
        if 0 < value < 1e8:
            return value
    return None


def parse_notice_limits(text: str) -> dict[str, float | None]:
    compact = re.sub(r"\s+", "", text or "")
    return {
        "direct": _channel_amount(compact, "直销"),
        "agency": _channel_amount(compact, "代销"),
        "generic": _generic_notice_limit(compact),
    }


def merge_channel_limits(
    agency_limit: float | None, agency_open: bool, notice: dict[str, float | None]
) -> tuple[float | None, float | None]:
    direct = notice.get("direct")
    agency = notice.get("agency")
    generic = notice.get("generic")
    if direct is None and agency is None and generic is not None:
        direct = generic
        if agency_open and agency_limit not in (0, 0.0):
            agency = generic
    if agency is None:
        agency = agency_limit if agency_open else 0.0
    if direct is None:
        direct = agency if agency_open else None
    return direct, agency


def fetch_notice_list(code: str) -> list[dict[str, Any]]:
    raw = http_get_retry(
        f"https://api.fund.eastmoney.com/f10/JJGG?fundcode={code}&pageIndex=1&pageSize=10&type=5",
        referer="https://fund.eastmoney.com/",
    )
    data = json.loads(raw)
    rows = data.get("Data") or []
    return rows if isinstance(rows, list) else []


def fetch_notice_text(ann_id: str) -> str:
    raw = http_get_retry(
        "https://np-cnotice-fund.eastmoney.com/api/content/ann"
        f"?art_code={ann_id}&page_index=1&page_size=1&client_source=web&ann_type=FUN",
        referer="https://fund.eastmoney.com/",
    )
    return str((json.loads(raw).get("data") or {}).get("notice_content") or "")


def pick_quota_notice(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in items:
        if is_quota_notice_title(str(item.get("TITLE") or "")):
            return item
    return None


def fetch_direct_notices(codes: list[str]) -> dict[str, dict[str, Any]]:
    cache = load_json(OTC_ANN_CACHE, {})
    if not isinstance(cache, dict):
        cache = {}
    picked: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch_notice_list, code): code for code in codes}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                notice = pick_quota_notice(fut.result())
            except Exception as exc:  # noqa: BLE001
                picked[code] = {"error": str(exc)}
                continue
            if notice:
                picked[code] = notice
    parsed_by_id: dict[str, dict[str, float | None]] = {}
    need_ids = []
    for notice in picked.values():
        ann_id = str(notice.get("ID") or "")
        if not ann_id or "error" in notice:
            continue
        if ann_id in cache and isinstance(cache[ann_id], dict) and "direct" in cache[ann_id]:
            parsed_by_id[ann_id] = cache[ann_id]
        else:
            need_ids.append(ann_id)
    uniq_ids = list(dict.fromkeys(need_ids))
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(fetch_notice_text, ann_id): ann_id for ann_id in uniq_ids}
        for fut in as_completed(futs):
            ann_id = futs[fut]
            try:
                limits = parse_notice_limits(fut.result())
            except Exception:  # noqa: BLE001
                limits = {"direct": None, "agency": None, "generic": None}
            parsed_by_id[ann_id] = limits
            cache[ann_id] = limits
    if uniq_ids:
        save_json(OTC_ANN_CACHE, cache)
    out: dict[str, dict[str, Any]] = {}
    for code, notice in picked.items():
        if "error" in notice:
            out[code] = notice
            continue
        ann_id = str(notice.get("ID") or "")
        out[code] = {
            "ann_id": ann_id,
            "title": notice.get("TITLE"),
            "date": notice.get("PUBLISHDATEDesc"),
            **(parsed_by_id.get(ann_id) or {}),
        }
    return out


def discover_otc_rows(purchase_rows: list[list[str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in purchase_rows:
        if len(row) < 10:
            continue
        code, name = str(row[0]), str(row[1])
        if code in seen or code.startswith(OTC_ETF_PREFIX):
            continue
        kind = classify_name(name)
        if not kind:
            continue
        seen.add(code)
        agency_limit, agency_open, agency_note = agency_from_purchase_row(str(row[5]), row[9])
        out.append(
            {
                "code": code,
                "name": name,
                "kind": kind,
                "ftype": str(row[2]),
                "agency_status": str(row[5]),
                "agency_limit": agency_limit,
                "agency_open": agency_open,
                "agency_note": agency_note,
                "nav": to_float(row[3]),
                "nav_date": str(row[4]),
            }
        )
    out.sort(key=lambda x: (x["kind"], x["code"]))
    return out


def channel_meets(limit: float | None, min_limit: float) -> bool:
    if limit is None:
        return False
    return limit >= min_limit


def channel_hot(limit: float | None, threshold: float = HOT_QUOTA_CNY) -> bool:
    return limit is not None and limit > threshold


def pick_hot_institutions(snap: dict[str, Any], threshold: float = HOT_QUOTA_CNY) -> list[dict[str, Any]]:
    return [
        item
        for item in snap.get("hits") or []
        if channel_hot(item.get("direct_limit"), threshold) or channel_hot(item.get("agency_limit"), threshold)
    ]


def fmt_yuan(value: float | None) -> str:
    if value is None:
        return "未知"
    if value <= 0:
        return "无/暂停"
    if value >= 1e10:
        return "不限额"
    if value >= 10000:
        return f"{value / 10000:g}万元"
    return f"{value:g}元"


def _better_limit(new: float | None, old: float | None) -> bool:
    if new is None:
        return False
    if old is None:
        return True
    return new > old


def build_otc_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    min_limit = float(cfg.get("min_daily_limit_cny", 100))
    funds = discover_otc_rows(fetch_eastmoney_purchase_rows())
    if not funds:
        raise RuntimeError("未发现场外纳指/标普基金")
    notices = fetch_direct_notices([f["code"] for f in funds])
    rows = []
    for fund in funds:
        notice = notices.get(fund["code"]) or {}
        parsed = {
            "direct": notice.get("direct"),
            "agency": notice.get("agency"),
            "generic": notice.get("generic"),
        }
        direct, agency = merge_channel_limits(fund["agency_limit"], bool(fund["agency_open"]), parsed)
        hit = channel_meets(direct, min_limit) or channel_meets(agency, min_limit)
        rows.append(
            {
                **fund,
                "direct_limit": direct,
                "merged_agency_limit": agency,
                "notice_date": notice.get("date"),
                "notice_title": notice.get("title"),
                "hit": hit,
                "manager": manager_of(fund["name"]) or fund["name"][:4],
            }
        )
    rows.sort(
        key=lambda r: (
            0 if r["hit"] else 1,
            -(r["direct_limit"] if r["direct_limit"] is not None else -1),
            -(r["merged_agency_limit"] if r["merged_agency_limit"] is not None else -1),
            r["kind"],
            r["code"],
        )
    )
    institutions: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{row['manager']}|{row['kind']}"
        cur = institutions.get(key)
        if not cur:
            institutions[key] = {
                "manager": row["manager"],
                "kind": row["kind"],
                "direct_limit": row["direct_limit"],
                "agency_limit": row["merged_agency_limit"],
                "direct_code": row["code"],
                "agency_code": row["code"],
                "name": row["name"],
            }
            continue
        if _better_limit(row["direct_limit"], cur["direct_limit"]):
            cur["direct_limit"] = row["direct_limit"]
            cur["direct_code"] = row["code"]
            cur["name"] = row["name"]
        if _better_limit(row["merged_agency_limit"], cur["agency_limit"]):
            cur["agency_limit"] = row["merged_agency_limit"]
            cur["agency_code"] = row["code"]
    hits = sorted(
        [
            item
            for item in institutions.values()
            if channel_meets(item["direct_limit"], min_limit) or channel_meets(item["agency_limit"], min_limit)
        ],
        key=lambda x: (
            -(x["direct_limit"] if x["direct_limit"] is not None else -1),
            x["manager"],
        ),
    )
    return {
        "ts": now_cn().isoformat(timespec="seconds"),
        "trade_date": now_cn().strftime("%Y-%m-%d"),
        "min_limit": min_limit,
        "rows": rows,
        "hits": hits,
    }


def fetch_us_ref() -> dict[str, Any]:
    raw = http_get_retry("https://qt.gtimg.cn/q=usQQQ,usSPY", encoding="gbk")
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        if '="' not in line:
            continue
        p = line.split('="', 1)[-1].rstrip('";').split("~")
        if len(p) < 32:
            continue
        key = "QQQ" if "QQQ" in p[2].upper() or "纳指" in p[1] else "SPY"
        out[key] = {
            "price": to_float(p[3]),
            "prev_close": to_float(p[4]),
            "change_pct": to_float(p[32]),
            "time": p[30] if len(p) > 30 else "",
        }
    return out


def completed_index_closes(payload: dict[str, Any], symbol: str,
                           at: datetime | None = None) -> list[tuple[str, float]]:
    """腾讯指数日线：只取已结束的美股交易日，拒绝不完整或陈旧行情。"""
    ny_now = (at or now_cn()).astimezone(ZoneInfo("America/New_York"))
    data = payload["data"][symbol]
    quote_time = datetime.strptime(data["qt"][symbol][30], "%Y-%m-%d %H:%M:%S")
    quote_time = quote_time.replace(tzinfo=ny_now.tzinfo)
    if quote_time > ny_now + timedelta(minutes=5):
        raise ValueError("指数行情时间在未来")
    closes: list[tuple[str, float]] = []
    previous = None
    for row in data["day"]:
        day = datetime.strptime(row[0], "%Y-%m-%d").date()
        if previous is not None and day <= previous:
            raise ValueError("指数日线日期未严格递增")
        previous = day
        if day > ny_now.date():
            raise ValueError("指数日线日期在未来")
        if day == ny_now.date() and ny_now.hour < 16:
            continue
        close = to_float(row[2])
        if day.weekday() >= 5 or close is None or not 0 < close < float("inf"):
            raise ValueError("指数收盘数据无效")
        closes.append((day.isoformat(), close))
    if not closes:
        raise ValueError("没有已完成的指数日线")
    last_day = datetime.strptime(closes[-1][0], "%Y-%m-%d").date()
    # ponytail: 不引入交易日历；超过四个自然日未更新时跳过，不用旧行情触发提醒。
    if (ny_now.date() - last_day).days > 4:
        raise ValueError("指数日线过期")
    if last_day > quote_time.date():
        raise ValueError("日线晚于指数行情时间")
    quote_completed = quote_time.date() < ny_now.date() or quote_time.hour >= 16
    if quote_completed and last_day != quote_time.date():
        raise ValueError("最新已收盘日线缺失")
    # 未接早收盘日历时，保守等待16点后的行情确认，避免跨日缓存的盘中K线误报。
    if last_day == quote_time.date() and quote_time.hour < 16:
        raise ValueError("指数行情尚未取得收盘确认")
    return closes


def build_market_drop_snapshot(cfg: dict[str, Any], at: datetime | None = None) -> dict[str, Any]:
    at = at or now_cn()
    raw_lookback = cfg.get("market_drop_lookback_days", 5)
    lookback = int(raw_lookback)
    if isinstance(raw_lookback, bool) or lookback != float(raw_lookback) or not 1 <= lookback <= 252:
        raise ValueError("跌幅观察窗口需为1至252个交易日")
    rows, errors = [], {}
    for kind, name, symbol, config_key in US_MARKETS:
        try:
            threshold = float(cfg.get(config_key, 5))
            if not 0 < threshold < 100:
                raise ValueError("跌幅阈值需大于0且小于100")
            url = ("https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get"
                   f"?param={symbol},day,,,{max(40, lookback + 10)},qfq")
            payload = json.loads(http_get_retry(url, timeout=8))
            closes = completed_index_closes(payload, symbol, at)
            if len(closes) < lookback + 1:
                raise ValueError(f"不足{lookback + 1}个已完成收盘价")
            start_date, start_close = closes[-lookback - 1]
            end_date, end_close = closes[-1]
            rows.append({
                "kind": kind, "name": name, "symbol": symbol,
                "lookback_days": lookback, "threshold_pct": threshold,
                "start_date": start_date, "end_date": end_date,
                "start_close": start_close, "end_close": end_close,
                "change_pct": (end_close / start_close - 1) * 100,
                "triggered": end_close <= start_close * (1 - threshold / 100),
            })
        except Exception as exc:  # noqa: BLE001
            errors[kind] = str(exc)
            log(f"{name}累计跌幅检查失败: {exc}")
    return {"ts": at.isoformat(timespec="seconds"), "rows": rows, "errors": errors}


def market_drop_alert_groups(snap: dict[str, Any], state: dict[str, Any]) -> list[tuple[str, str, str]]:
    episodes = state.setdefault("market_drop_episodes", {})
    groups = []
    for row in snap["rows"]:
        kind = row["kind"]
        previous = episodes.get(kind) or {}
        if row["end_date"] < previous.get("end_date", ""):
            continue
        if not row["triggered"]:
            episodes[kind] = {"end_date": row["end_date"], "active": False}
            continue
        settings = f"{row['lookback_days']}d-{row['threshold_pct']:g}pct"
        if previous.get("active") and previous.get("settings") == settings:
            alert_id = previous["alert_id"]
        else:
            alert_id = f"drop-{kind}-{row['end_date']}-{settings}"
        episodes[kind] = {"end_date": row["end_date"], "active": True,
                          "settings": settings, "alert_id": alert_id}
        title = f"{row['name']}近{row['lookback_days']}个交易日下跌{-row['change_pct']:.2f}%"
        body = (f"{row['name']}累计涨跌 {row['change_pct']:+.2f}%（提醒阈值：下跌≥{row['threshold_pct']:g}%）\n"
                f"起点：{row['start_date']} 收盘 {row['start_close']:g}\n"
                f"终点：{row['end_date']} 收盘 {row['end_close']:g}\n"
                f"来源：腾讯指数日线 {row['symbol']}；只使用已结束的美股交易日。\n"
                "指数跌幅与国内基金溢价分别判断。同一轮达标确认后不重复提醒，恢复后再次跌破会重新提醒。")
        groups.append((alert_id, title, body))
    return groups


def fair_premium(price: float | None, nav: float | None) -> float | None:
    if price is None or nav is None or not (0 < price < float("inf") and 0 < nav < float("inf")):
        return None
    return (price / nav - 1.0) * 100.0


def in_a_share_session(dt: datetime | None = None) -> bool:
    dt = dt or now_cn()
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return (9 * 60 + 10) <= minutes <= (15 * 60 + 15)


def in_quota_release_window(dt: datetime | None = None) -> bool:
    """工作日 09:00–10:30。隔夜公告在开盘前同步到销售渠道，申购 9:30 起、15:00 截止。"""
    dt = dt or now_cn()
    if dt.weekday() >= 5:
        return False
    minutes = dt.hour * 60 + dt.minute
    return (9 * 60) <= minutes < (10 * 60 + 40)


def load_state() -> dict[str, Any]:
    return load_json(STATE_PATH, {"last_shares": {}, "last_hits": {}, "nags": {}})


def save_state(state: dict[str, Any]) -> None:
    save_json(STATE_PATH, state)


def load_ack() -> dict[str, Any]:
    return load_json(ACK_PATH, {"acked": {}, "pending": None})


def save_ack(data: dict[str, Any]) -> None:
    save_json(ACK_PATH, data)


def mark_ack(alert_id: str) -> None:
    data = load_ack()
    data.setdefault("acked", {})[alert_id] = now_cn().isoformat()
    if data.get("pending") == alert_id:
        data["pending"] = None
    save_ack(data)
    log(f"已确认收到: {alert_id}")


def is_acked(alert_id: str) -> bool:
    return alert_id in (load_ack().get("acked") or {})


def build_snapshot(cfg: dict[str, Any]) -> dict[str, Any]:
    funds = load_fund_universe()
    targets = discover_targets(funds, bool(cfg.get("include_lof", True)))
    if not targets:
        raise RuntimeError("未发现场内纳指/标普ETF")
    codes = [t["code"] for t in targets]
    quotes = fetch_tencent_quotes(codes)
    targets = [t for t in targets if (quotes.get(t["code"]) or {}).get("price")]
    codes = [t["code"] for t in targets]
    if not targets:
        raise RuntimeError("未发现有场内行情的纳指/标普ETF")
    backup = fetch_nav_backup(codes)
    estimates = fetch_lof_estimates([t["code"] for t in targets if t["listed_type"] == "LOF"])
    us_ref = {}
    try:
        us_ref = fetch_us_ref()
    except Exception as exc:  # noqa: BLE001
        log(f"QQQ/SPY 参考行情失败: {exc}")
    min_limit = float(cfg.get("min_daily_limit_cny", 100))
    state = load_state()
    last_shares = state.setdefault("last_shares", {})
    feeder_map = {etf["code"]: find_feeders(etf, funds)[:3] for etf in targets}
    status_codes = list(codes)
    for items in feeder_map.values():
        status_codes.extend(f["code"] for f in items)
    statuses = fetch_trade_status_many(status_codes)
    rows = []
    for etf in targets:
        code = etf["code"]
        q = quotes.get(code, {})
        b = backup.get(code, {})
        price = q.get("price") or b.get("price")
        metrics = premium_metrics(q, b, etf["listed_type"], estimates.get(code, {}))
        tencent_premium = q.get("tencent_premium")
        idx_chg = None
        if etf["kind"] == "nasdaq100":
            idx_chg = (us_ref.get("QQQ") or {}).get("change_pct")
        else:
            idx_chg = (us_ref.get("SPY") or {}).get("change_pct")
        self_st = statuses.get(code) or {"status_text": "未知", "daily_limit": None}
        self_ok, self_note = quota_from_status(self_st.get("status_text") or "", self_st.get("daily_limit"), min_limit)
        feeders = feeder_map.get(code) or []
        feeder_notes = []
        feeder_ok = None
        for feeder in feeders:
            fst = statuses.get(feeder["code"]) or {"status_text": "未知", "daily_limit": None}
            ok, note = quota_from_status(fst.get("status_text") or "", fst.get("daily_limit"), min_limit)
            feeder_notes.append(f"{feeder['code']}{feeder['name'][:18]}:{note}")
            if ok is True:
                feeder_ok = True
            elif ok is False and feeder_ok is not True:
                feeder_ok = False
        shares = q.get("shares")
        prev_shares = last_shares.get(code)
        share_up = False
        if shares and prev_shares and shares > float(prev_shares) * 1.0005:
            share_up = True
        if shares:
            last_shares[code] = shares
        has_quota = False
        quota_reason = ""
        if self_ok is True:
            has_quota, quota_reason = True, self_note
        elif feeder_ok is True:
            good = [n for n in feeder_notes if "暂停" not in n and "过小" not in n]
            has_quota, quota_reason = True, "联接/同公司有额度: " + "; ".join((good or feeder_notes)[:2])
        elif share_up:
            has_quota, quota_reason = True, "场内份额增加(一级申赎仍在发生)"
        else:
            has_quota = False
            quota_reason = self_note if self_ok is False else (("联接:" + "; ".join(feeder_notes[:2])) if feeder_notes else self_note)
        # ETF腾讯溢价基于IOPV；LOF腾讯溢价基于已公布净值，不校验第三方估值。
        comparable = metrics["premium"] if etf["listed_type"] == "ETF" else metrics["computed_premium"]
        mismatch = None
        if comparable is not None and tencent_premium is not None:
            mismatch = abs(comparable - tencent_premium)
        rows.append(
            {
                **etf,
                "price": price,
                **metrics,
                "iopv": q.get("iopv"),
                "tencent_premium": tencent_premium,
                "comparison_premium": comparable,
                "premium_mismatch": mismatch,
                "has_quota": has_quota,
                "quota_reason": quota_reason,
                "self_status": self_st.get("status_text"),
                "feeders": feeder_notes,
                "shares": shares,
                "quote_time": q.get("quote_time"),
                "trade_date": q.get("trade_date") or parse_trade_date(str(b.get("hq_date") or "")),
                "us_index_chg": idx_chg,
            }
        )
    save_state(state)
    dates = [r.get("trade_date") for r in rows if r.get("trade_date")]
    trade_date = max(dates) if dates else now_cn().strftime("%Y-%m-%d")
    threshold = premium_limit("nasdaq100", cfg)
    spx_threshold = premium_limit("spx500", cfg)
    hits = pick_premium_hits(rows, trade_date, cfg)
    if cfg.get("require_quota"):
        hits = [r for r in hits if r.get("has_quota")]
    return {
        "ts": now_cn().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "us_ref": us_ref,
        "rows": rows,
        "hits": hits,
        "threshold": threshold,
        "spx_threshold": spx_threshold,
        "require_quota": bool(cfg.get("require_quota")),
        "premium_method": "dated-v2",
    }


def fmt_pct(v: Any) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}%"


def premium_summary(row: dict[str, Any]) -> str:
    basis = row.get("premium_basis") or "静态净值溢价"
    value = row.get("premium_nav")
    reference = f" 参考值{value:.4f}({row.get('premium_date') or '-'})" if value is not None else ""
    return (f"{row.get('code')} {row.get('name')} {basis}{fmt_pct(row.get('premium'))} "
            f"现价{row.get('price')}({row.get('trade_date') or '-'}){reference}")


def format_table(snap: dict[str, Any]) -> str:
    extra = "且有额度" if snap.get("require_quota") else ""
    lines = [
        f"行情日期 {snap.get('trade_date') or '-'}  采集 {snap['ts']}  "
        f"提醒: 标普(含等权)<{float(snap.get('spx_threshold') or 3):.0f}%  纳指<{float(snap.get('threshold') or 5):.0f}%{extra}",
        "提醒口径: ETF使用IOPV；LOF使用最近交易日已公布净值，尚未公布时使用HaoETF最新估值。公式均为(采集时价格/参考值-1)×100%。",
        "静态净值溢价仅作对照，可能未反映隔夜涨跌。估值不是实时净值；参考值缺失或过期时不触发低溢价提醒。",
        "",
        f"{'代码':<8}{'类型':<6}{'指数':<10}{'现价':>8}{'参考值':>10}{'参考溢价':>10}  {'口径':<10}{'参考日':<12}{'静态净值溢价':>10}{'腾讯溢价':>10}  说明",
    ]
    for r in snap["rows"]:
        flag = "有" if r["has_quota"] else "无"
        px = f"{r['price']:.3f}" if r["price"] is not None else "-"
        value = r.get("premium_nav")
        nv = f"{value:.4f}" if value is not None else "-"
        nd = str(r.get("premium_date") or "-")
        lines.append(
            f"{r['code']:<8}{r['listed_type']:<6}{kind_label(r['kind']):<10}"
            f"{px:>8}{nv:>10}{fmt_pct(r['premium']):>10}  "
            f"{r.get('premium_basis') or '旧口径':<10}{nd:<12}"
            f"{fmt_pct(r.get('nav_premium', r.get('computed_premium'))):>10}{fmt_pct(r['tencent_premium']):>10}  "
            f"净值{r.get('nav')}({r.get('nav_date') or '日期未知'}) 额度{flag} {r['quota_reason'][:42]}"
        )
    if snap["hits"]:
        lines.append("")
        lines.append("低于阈值:")
        for r in snap["hits"]:
            lines.append(
                f"  {premium_summary(r)} {r['quota_reason']}"
            )
    else:
        lines.append("")
        lines.append("已核实参考值的品种中，没有低于阈值的品种；参考值缺失或过期的品种不参与判断。")
    mismatch = [r for r in snap["rows"] if r.get("premium_mismatch") is not None and r["premium_mismatch"] > 0.25]
    if mismatch:
        lines.append("溢价交叉核对偏差>0.25个百分点:")
        for r in mismatch:
            lines.append(f"  {r['code']} 同口径计算{fmt_pct(r.get('comparison_premium', r['computed_premium']))} 腾讯{fmt_pct(r['tencent_premium'])}")
    else:
        checked = sum(r.get("premium_mismatch") is not None for r in snap["rows"])
        lines.append(f"腾讯同口径核对{checked}只，偏差≤0.25个百分点；此核对不验证估算净值。")
    qqq = (snap.get("us_ref") or {}).get("QQQ") or {}
    spy = (snap.get("us_ref") or {}).get("SPY") or {}
    if qqq or spy:
        lines.append(
            f"参考指数 QQQ {fmt_pct(qqq.get('change_pct'))}  SPY {fmt_pct(spy.get('change_pct'))}"
        )
    return "\n".join(lines)


BUY_GUIDE = (
    "提醒后怎么买：交易日 9:30–15:00 用证券账户在场内直接买入即可，和买股票一样，脚本不会代下单。\n"
    "暂停申购/没额度只影响场外申购和一级申赎，一般不影响场内成交。\n"
    "提醒采用标明日期的参考值；估算溢价不是实时净值溢价，下单前需重新核对价格与估值。"
)


def premium_alert_groups(snap: dict[str, Any]) -> list[tuple[str, str, str]]:
    trade_date = str(snap.get("trade_date") or "")
    groups: list[tuple[str, str, str]] = []
    specs = (
        ("spx", "标普", float(snap.get("spx_threshold") or 3), ("spx500", "spx500ew")),
        ("nq", "纳指100", float(snap.get("threshold") or 5), ("nasdaq100",)),
    )
    for key, label, limit, kinds in specs:
        rows = [h for h in snap.get("hits") or [] if h.get("kind") in kinds]
        if not rows:
            continue
        lines = [f"{h.get('code')} {h.get('name')} {fmt_pct(h.get('premium'))}" for h in rows]
        gid = f"hit-{trade_date}-{key}-" + "-".join(str(h.get("code")) for h in rows)
        groups.append((gid, f"{label}溢价<{limit:.0f}% {len(rows)}只", "\n".join(lines)))
    return groups


def run_cmd(cmd: list[str], timeout: int = 20) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        ok = proc.returncode == 0
        return ok, (proc.stdout or proc.stderr or "")[:400]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def speak(text: str) -> tuple[bool, str]:
    return run_cmd(["spd-say", "-l", "zh", "-w", text[:80]], timeout=15)


def play_sound() -> tuple[bool, str]:
    if Path(SOUND).exists():
        return run_cmd(["paplay", SOUND], timeout=10)
    return False, "no sound file"


def send_ntfy(topic: str, title: str, body: str) -> tuple[bool, str]:
    if not topic:
        return False, "未配置 ntfy"
    url = "https://ntfy.sh/" + urllib.parse.quote(topic)
    req = urllib.request.Request(
        url,
        data=body[:4000].encode("utf-8"),
        headers={
            "User-Agent": UA,
            "Title": title.encode("utf-8").decode("latin-1"),
            "Priority": "urgent",
            "Tags": "warning,chart_with_upwards_trend",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, resp.read().decode("utf-8", "replace")[:200]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def send_email(cfg: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    smtp_cfg = cfg.get("smtp") or {}
    to_addr = (cfg.get("contacts") or {}).get("email")
    host = smtp_cfg.get("host")
    if not host or not to_addr:
        return False, "未配置 SMTP（无法发到邮箱）"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = smtp_cfg.get("from") or smtp_cfg.get("username") or to_addr
    msg["To"] = to_addr
    try:
        port = int(smtp_cfg.get("port") or 587)
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            if smtp_cfg.get("username"):
                smtp.login(smtp_cfg["username"], smtp_cfg.get("password") or "")
            smtp.send_message(msg)
        return True, f"已发送 {to_addr}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def send_aliyun_sms(cfg: dict[str, Any], title: str, body: str) -> tuple[bool, str]:
    ali = cfg.get("aliyun") or {}
    phone = (cfg.get("contacts") or {}).get("phone")
    if not ali.get("access_key_id") or not ali.get("sms_template_code") or not phone:
        return False, "未配置阿里云短信密钥/模板"
    try:
        from aliyunsdkcore.client import AcsClient  # type: ignore
        from aliyunsdkcore.request import CommonRequest  # type: ignore
    except Exception:
        return False, "未安装 aliyunsdkcore，无法发短信"
    try:
        client = AcsClient(ali["access_key_id"], ali["access_key_secret"], "cn-hangzhou")
        req = CommonRequest()
        req.set_accept_format("json")
        req.set_domain("dysmsapi.aliyuncs.com")
        req.set_method("POST")
        req.set_protocol_type("https")
        req.set_version("2017-05-25")
        req.set_action_name("SendSms")
        req.add_query_param("PhoneNumbers", phone)
        req.add_query_param("SignName", ali.get("sms_sign_name") or "")
        req.add_query_param("TemplateCode", ali.get("sms_template_code") or "")
        req.add_query_param("TemplateParam", json.dumps({"content": (title + " " + body)[:40]}, ensure_ascii=False))
        resp = client.do_action_with_exception(req)
        return True, resp.decode("utf-8")[:200]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def send_aliyun_voice(cfg: dict[str, Any], title: str) -> tuple[bool, str]:
    ali = cfg.get("aliyun") or {}
    phone = (cfg.get("contacts") or {}).get("phone")
    if not ali.get("access_key_id") or not ali.get("voice_called_show_number") or not phone:
        return False, "未配置阿里云语音通知"
    try:
        from aliyunsdkcore.client import AcsClient  # type: ignore
        from aliyunsdkcore.request import CommonRequest  # type: ignore
    except Exception:
        return False, "未安装 aliyunsdkcore，无法打电话"
    try:
        client = AcsClient(ali["access_key_id"], ali["access_key_secret"], "cn-hangzhou")
        req = CommonRequest()
        req.set_accept_format("json")
        req.set_domain("dyvmsapi.aliyuncs.com")
        req.set_method("POST")
        req.set_protocol_type("https")
        req.set_version("2017-05-25")
        req.set_action_name("SingleCallByTts")
        req.add_query_param("CalledNumber", phone)
        req.add_query_param("CalledShowNumber", ali.get("voice_called_show_number") or "")
        req.add_query_param("TtsCode", ali.get("voice_tts_code") or "")
        req.add_query_param("TtsParam", json.dumps({"msg": title[:20]}, ensure_ascii=False))
        resp = client.do_action_with_exception(req)
        return True, resp.decode("utf-8")[:200]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def fire_channels(cfg: dict[str, Any], title: str, body: str) -> dict[str, str]:
    results: dict[str, str] = {}
    for name, fn in [
        ("sound", play_sound),
            ("speech", lambda: speak("请确认收到QDII提醒")),
        ("ntfy", lambda: send_ntfy(str(cfg.get("ntfy_topic") or ""), title, body)),
        ("email", lambda: send_email(cfg, title, body)),
        ("sms", lambda: send_aliyun_sms(cfg, title, body)),
        ("voice", lambda: send_aliyun_voice(cfg, title)),
    ]:
        ok, detail = fn()
        results[name] = ("OK " if ok else "FAIL ") + detail.replace("\n", " ")[:180]
        log(f"通道 {name}: {results[name]}")
    return results


class AckHandler(BaseHTTPRequestHandler):
    alert_id = "pending"
    title = "QDII提醒"
    body = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _html(self, inner: str, code: int = 200) -> None:
        page = f"""<!doctype html><meta charset=utf-8><title>确认收到</title>
<style>body{{font-family:sans-serif;background:#111;color:#eee;padding:40px}}
a.btn,button{{display:inline-block;font-size:28px;padding:16px 28px;background:#e53935;color:#fff;border:0;border-radius:10px;text-decoration:none}}
pre{{white-space:pre-wrap}}</style>
<h1>{htmlmod.escape(self.title)}</h1>
<p><a class="btn" href="/ack">确认收到</a></p>
<pre>{htmlmod.escape(inner)}</pre>"""
        data = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/ack":
            mark_ack(self.alert_id)
            self._html("已确认收到，提醒将停止。")
            return
        if path == "/status":
            payload = json.dumps({"acked": is_acked(self.alert_id), "id": self.alert_id}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._html(self.body)


def start_ack_server(alert_id: str, title: str, body: str) -> ThreadingHTTPServer | None:
    AckHandler.alert_id = alert_id
    AckHandler.title = title
    AckHandler.body = body
    try:
        server = ThreadingHTTPServer(("127.0.0.1", ACK_PORT), AckHandler)
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def zenity_markup(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def zenity_popup(title: str, body: str) -> bool:
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":1")
    try:
        proc = subprocess.run(
            [
                "zenity",
                "--warning",
                "--width=560",
                "--height=240",
                f"--title={zenity_markup(title)}",
                f"--text={zenity_markup(body[:700])}",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return False
    err = (proc.stderr or "").lower()
    if "cannot open display" in err or "failed to" in err:
        return False
    return proc.returncode in (0, 1)


def hot_dismissed_today() -> int:
    data = load_json(HOT_DISMISS_PATH, {})
    if data.get("date") != now_cn().strftime("%Y-%m-%d"):
        return 0
    return int(data.get("count") or 0)


def mark_hot_dismiss() -> int:
    count = hot_dismissed_today() + 1
    save_json(HOT_DISMISS_PATH, {"date": now_cn().strftime("%Y-%m-%d"), "count": count})
    return count


def zenity_confirm(title: str, body: str) -> bool:
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":1")
    try:
        short = body.split("扫描时间")[0].strip() or body[:500]
        proc = subprocess.run(
            [
                "zenity",
                "--question",
                "--width=560",
                "--height=280",
                f"--title={zenity_markup(title)}",
                f"--text={zenity_markup(short[:700])}\n\n点「确认收到」才停止。",
                "--ok-label=确认收到",
                "--cancel-label=稍后",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        err = (proc.stderr or "").lower()
        if "cannot open display" in err or proc.returncode not in (0, 1):
            return None
        return proc.returncode == 0
    except Exception:
        return None


def open_local_page() -> None:
    subprocess.Popen(["xdg-open", f"http://127.0.0.1:{ACK_PORT}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def nag_until_ack(cfg: dict[str, Any], alert_id: str, title: str, body: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ack = load_ack()
    ack["pending"] = alert_id
    save_ack(ack)
    server = start_ack_server(alert_id, title, body)
    log(f"弹窗提醒 {alert_id}")
    while not is_acked(alert_id):
        got = zenity_confirm(title, body)
        if got:
            mark_ack(alert_id)
            break
        log(f"未点确认，继续弹窗 {alert_id}")
        time.sleep(15 if got is None else 1)
    if server:
        server.shutdown()
    log(f"nag 结束 {alert_id}")


@contextmanager
def nag_queue_lock():
    # ponytail: 本机少量提醒共用文件锁；多用户并发时再拆独立队列。
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "nag-queue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def spawn_nag_queue(items: list[tuple[str, str, str]]) -> None:
    if not items:
        return
    with nag_queue_lock():
        pending = {item["id"]: item for item in load_json(STATE_DIR / "nag-queue.json", [])
                   if not is_acked(item["id"])}
        for alert_id, title, body in items:
            if not is_acked(alert_id):
                pending[alert_id] = {"id": alert_id, "title": title, "body": body}
        save_json(STATE_DIR / "nag-queue.json", list(pending.values()))
        if pending:
            first = next(iter(pending.values()))
            spawn_nag(first["id"], first["title"], first["body"], queue=True)


def spawn_nag(alert_id: str, title: str, body: str, queue: bool = False) -> None:
    if not queue:
        spawn_nag_queue([(alert_id, title, body)])
        return
    nag_pid = STATE_DIR / "nag.pid"
    if nag_pid.exists():
        try:
            pid = int(nag_pid.read_text().strip())
            os.kill(pid, 0)
            log(f"nag 已在运行 pid={pid}")
            return
        except (ValueError, OSError):
            pass
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "nag-queue"],
        start_new_session=True,
        stdout=open(STATE_DIR / "nag.log", "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    nag_pid.write_text(str(proc.pid), encoding="utf-8")
    log(f"已启动 nag pid={proc.pid} id={alert_id}")


def cmd_nag_queue(cfg: dict[str, Any]) -> int:
    pid_path = STATE_DIR / "nag.pid"
    try:
        while True:
            with nag_queue_lock():
                pending = [item for item in load_json(STATE_DIR / "nag-queue.json", [])
                           if not is_acked(item["id"])]
                if not pending:
                    if pid_path.exists() and pid_path.read_text().strip() == str(os.getpid()):
                        pid_path.unlink()
                    return 0
                item = pending[0]
            nag_until_ack(cfg, str(item["id"]), str(item.get("title") or ""), str(item.get("body") or ""))
    finally:
        with nag_queue_lock():
            if pid_path.exists() and pid_path.read_text().strip() == str(os.getpid()):
                pid_path.unlink()


def daily_json_path(trade_date: str) -> Path:
    return DAILY_DIR / f"{trade_date}.json"


def rewrite_daily_csv() -> None:
    csv_path = DAILY_DIR / "premium.csv"
    fieldnames = [
        "trade_date",
        "code",
        "name",
        "kind",
        "listed_type",
        "price",
        "nav",
        "nav_date",
        "daily_premium_pct",
        "premium_basis",
        "premium_reference",
        "premium_reference_date",
        "static_nav_premium_pct",
        "tencent_premium_pct",
        "has_quota",
        "quota_reason",
        "alerted",
    ]
    rows_out: list[dict[str, Any]] = []
    for path in sorted(DAILY_DIR.glob("20??-??-??.json")):
        snap = load_json(path, {})
        trade_date = snap.get("trade_date") or path.stem
        hit_codes = {h.get("code") for h in snap.get("hits") or []}
        for r in snap.get("rows") or []:
            rows_out.append(
                {
                    "trade_date": trade_date,
                    "code": r.get("code"),
                    "name": r.get("name"),
                    "kind": r.get("kind"),
                    "listed_type": r.get("listed_type"),
                    "price": r.get("price"),
                    "nav": r.get("nav"),
                    "nav_date": r.get("nav_date"),
                    "daily_premium_pct": None if r.get("premium") is None else round(float(r["premium"]), 4),
                    "premium_basis": r.get("premium_basis") or "旧口径:已公布净值",
                    "premium_reference": r.get("premium_nav"),
                    "premium_reference_date": r.get("premium_date"),
                    "static_nav_premium_pct": r.get("nav_premium", r.get("computed_premium")),
                    "tencent_premium_pct": None if r.get("tencent_premium") is None else round(float(r["tencent_premium"]), 4),
                    "has_quota": r.get("has_quota"),
                    "quota_reason": r.get("quota_reason"),
                    "alerted": r.get("code") in hit_codes,
                }
            )
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)


def save_daily_log(snap: dict[str, Any]) -> Path:
    trade_date = str(snap.get("trade_date") or now_cn().strftime("%Y-%m-%d"))
    snap = dict(snap)
    snap["trade_date"] = trade_date
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    json_path = daily_json_path(trade_date)
    save_json(json_path, snap)
    (DAILY_DIR / f"{trade_date}.txt").write_text(format_table(snap) + "\n", encoding="utf-8")
    rewrite_daily_csv()
    log(f"已保存日溢价日志 {json_path}")
    return json_path


def cmd_scan(cfg: dict[str, Any]) -> int:
    snap = build_snapshot(cfg)
    save_json(STATE_DIR / "last_snapshot.json", snap)
    save_daily_log(snap)
    print(format_table(snap))
    print(f"\n日志目录: {DAILY_DIR}")
    return 0


def check_daily_premium(cfg: dict[str, Any], force: bool = False) -> list[tuple[str, str, str]]:
    snap = build_snapshot(cfg)
    trade_date = str(snap.get("trade_date") or now_cn().strftime("%Y-%m-%d"))
    json_path = daily_json_path(trade_date)
    if json_path.exists() and not force:
        log(f"{trade_date} 日溢价已记录过，跳过。需要重跑请加 --force")
        print(format_table(load_json(json_path, snap)))
        return []
    save_json(STATE_DIR / "last_snapshot.json", snap)
    save_daily_log(snap)
    print(format_table(snap))
    if trade_date != now_cn().strftime("%Y-%m-%d"):
        log(f"{trade_date} 为历史行情，只记录、不发当日提醒")
        return []
    groups = premium_alert_groups(snap)
    if not groups:
        log(f"{trade_date} 标普<{snap['spx_threshold']:.0f}% 纳指<{snap['threshold']:.0f}% 无需提醒或已确认")
    return groups


def cmd_daily(cfg: dict[str, Any], force: bool = False, alert: bool = True) -> int:
    groups = []
    status = 0
    try:
        groups.extend(check_daily_premium(cfg, force))
    except Exception as exc:  # noqa: BLE001
        log(f"场内溢价检查失败: {exc}")
        status = 1
    try:
        market = build_market_drop_snapshot(cfg)
        save_json(STATE_DIR / "last_market_drop.json", market)
        for row in market["rows"]:
            log(f"{row['name']}近{row['lookback_days']}个交易日涨跌{row['change_pct']:+.2f}% "
                f"({row['start_date']}至{row['end_date']})")
        if alert:
            state = load_state()
            groups.extend(market_drop_alert_groups(market, state))
            save_state(state)
    except Exception as exc:  # noqa: BLE001
        log(f"指数累计跌幅检查失败: {exc}")
        status = 1
    if alert:
        groups = [item for item in groups if not is_acked(item[0])]
        if groups:
            spawn_nag_queue(groups)
    return status


def cmd_watch(cfg: dict[str, Any]) -> int:
    log("改为每个交易日14:10跑一次，watch 现在等同 daily")
    return cmd_daily(cfg, force=False, alert=True)


def cmd_test(cfg: dict[str, Any]) -> int:
    log("开始覆盖与溢价核对，随后发送测试提醒")
    snap = build_snapshot(cfg)
    save_json(STATE_DIR / "last_snapshot.json", snap)
    print(format_table(snap))
    print()
    print("覆盖核对:")
    print(f"  场内纳指100: {sum(1 for r in snap['rows'] if r['kind']=='nasdaq100')}")
    print(f"  场内标普500: {sum(1 for r in snap['rows'] if r['kind']=='spx500')}")
    print(f"  场内标普等权: {sum(1 for r in snap['rows'] if r['kind']=='spx500ew')}")
    print(f"  合计: {len(snap['rows'])}")
    title, body = "QDII提醒测试", "这是测试。点「确认收到」停止。"
    alert_id = f"test-{now_cn():%Y%m%d-%H%M%S}"
    # 测试必须重新确认，清掉旧测试 ack 不影响，新 id 即可。
    print()
    print(f"测试提醒 id={alert_id}")
    print(f"请在桌面弹窗或浏览器打开 http://127.0.0.1:{ACK_PORT}/ 点「确认收到」")
    print(f"手机可订阅 ntfy 主题: {cfg.get('ntfy_topic')}")
    nag_until_ack(cfg, alert_id, title, body)
    return 0


def cmd_ack(alert_id: str | None) -> int:
    data = load_ack()
    target = alert_id or data.get("pending")
    if not target:
        print("没有待确认的提醒")
        return 1
    mark_ack(str(target))
    print(f"已确认 {target}")
    return 0


def otc_json_path(trade_date: str) -> Path:
    return DAILY_DIR / f"{trade_date}-otc.json"


def otc_stat_line(snap: dict[str, Any]) -> str:
    parts = []
    for kind in ("spx500ew", "spx500", "nasdaq100"):
        funds = sum(r.get("kind") == kind for r in snap.get("rows") or [])
        buys = sum(h.get("kind") == kind for h in snap.get("hits") or [])
        parts.append(f"{kind_label(kind)} {funds}只/可买{buys}家")
    return "  ".join(parts)


def format_otc_table(snap: dict[str, Any]) -> str:
    lines = [
        f"场外额度 {snap.get('trade_date') or '-'}  采集 {snap['ts']}  提醒: 直销或代销 ≥ {snap['min_limit']:g}元",
        otc_stat_line(snap),
        "代销=天天基金申购状态表；直销=最近限额公告。I类等直销专属份额代销常为0。",
        "",
        f"{'代码':<8}{'指数':<8}{'机构':<8}{'直销':<10}{'代销':<10}名称",
    ]
    for r in snap["rows"]:
        lines.append(
            f"{r['code']:<8}{kind_label(r['kind']):<8}{r['manager']:<8}"
            f"{fmt_yuan(r['direct_limit']):<10}{fmt_yuan(r['merged_agency_limit']):<10}"
            f"{r['name'][:28]}"
        )
    lines.append("")
    if snap["hits"]:
        lines.append(f"≥{snap['min_limit']:g}元的场外机构:")
        for h in snap["hits"]:
            lines.append(
                f"  {h['manager']} {kind_label(h['kind'])} "
                f"直销{fmt_yuan(h['direct_limit'])}({h['direct_code']}) "
                f"代销{fmt_yuan(h['agency_limit'])}({h['agency_code']})"
            )
    else:
        lines.append(f"今日没有直销或代销 ≥ {snap['min_limit']:g}元的场外纳指/标普机构，不提醒。")
    return "\n".join(lines)


def load_latest_premium(as_of: str | None = None) -> dict[str, Any]:
    paths = sorted(DAILY_DIR.glob("20??-??-??.json"))
    if not paths:
        return {}
    data = load_json(paths[-1], {})
    if not isinstance(data, dict):
        return {}
    expected = previous_weekday(as_of or now_cn().strftime("%Y-%m-%d"))
    if data.get("trade_date") != expected or data.get("premium_method") != "dated-v2":
        return {**data, "hits": [], "stale": True}
    return data


def format_otc_alert(snap: dict[str, Any]) -> tuple[str, str]:
    hits = snap.get("hits") or []
    lines = [otc_stat_line(snap)]
    lines.extend(
        f"{kind_label(h['kind'])} {h['manager']} 直销{fmt_yuan(h['direct_limit'])} 代销{fmt_yuan(h['agency_limit'])}"
        for h in hits
    )
    return f"场外可买{len(hits)}家", "\n".join(lines)


def save_otc_log(snap: dict[str, Any]) -> Path:
    trade_date = str(snap.get("trade_date") or now_cn().strftime("%Y-%m-%d"))
    snap = dict(snap)
    snap["trade_date"] = trade_date
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    json_path = otc_json_path(trade_date)
    save_json(json_path, snap)
    (DAILY_DIR / f"{trade_date}-otc.txt").write_text(format_otc_table(snap) + "\n", encoding="utf-8")
    log(f"已保存场外额度日志 {json_path}")
    return json_path


def cmd_otc(cfg: dict[str, Any], force: bool = False, alert: bool = True) -> int:
    trade_date = now_cn().strftime("%Y-%m-%d")
    json_path = otc_json_path(trade_date)
    if json_path.exists() and not force:
        snap = load_json(json_path, {})
        log(f"{trade_date} 已有场外额度日志，按记录直接提醒")
    else:
        snap = build_otc_snapshot(cfg)
        trade_date = str(snap.get("trade_date") or trade_date)
        json_path = otc_json_path(trade_date)
        save_json(STATE_DIR / "last_otc.json", snap)
        save_otc_log(snap)
    print(format_otc_table(snap))
    if not alert:
        return 0
    if not snap.get("hits"):
        log(f"{trade_date} 无可买机构，不提醒")
        return 0
    title, body = format_otc_alert(snap)
    names = "-".join(f"{h['manager']}{h['kind']}" for h in snap.get("hits") or [])
    alert_id = f"otc-{trade_date}-{names}"
    if is_acked(alert_id):
        log(f"{alert_id} 已确认过，不再提醒")
        return 0
    spawn_nag(alert_id, title, body)
    return 0


def cmd_otc_hot(cfg: dict[str, Any]) -> int:
    if not in_quota_release_window():
        log("非额度放开时段(09:00-10:30)，跳过>500元巡检")
        return 0
    closed = hot_dismissed_today()
    if closed >= HOT_DISMISS_STOP:
        log(f"今日已关闭弹窗{closed}次，停止>500元提醒")
        return 0
    snap = build_otc_snapshot(cfg)
    hot = pick_hot_institutions(snap)
    save_json(STATE_DIR / "last_otc.json", snap)
    if not hot:
        log(f"场外额度无>{HOT_QUOTA_CNY:g}元机会")
        return 0
    title = f"场外额度>{HOT_QUOTA_CNY:.0f}元 {len(hot)}家"
    lines = [
        f"{h['manager']} {kind_label(h['kind'])} "
        f"直销{fmt_yuan(h['direct_limit'])}({h['direct_code']}) "
        f"代销{fmt_yuan(h['agency_limit'])}({h['agency_code']})"
        for h in hot
    ]
    left = HOT_DISMISS_STOP - closed
    body = (
        "\n".join(lines)
        + "\n\n直销走基金公司APP/官网，代销走天天基金等平台。"
        + f"\n点「确定」或关掉窗口记 1 次。再关 {left} 次，今日不再弹。"
    )
    log(f"{title}: {'; '.join(lines)}")
    if zenity_popup(title, body):
        log(f"已关闭弹窗 {mark_hot_dismiss()}/{HOT_DISMISS_STOP}")
    return 0


def cmd_install() -> int:
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "qdii-etf-alert.service",
        "qdii-etf-alert.timer",
        "qdii-otc-quota.service",
        "qdii-otc-quota.timer",
        "qdii-otc-hot.service",
        "qdii-otc-hot.timer",
    ):
        (unit_dir / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    desktop = Path.home() / ".config/autostart/qdii-etf-alert.desktop"
    if desktop.exists():
        desktop.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "disable", "--now", "qdii-etf-alert.service"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "qdii-etf-alert.timer"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "qdii-otc-quota.timer"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "qdii-otc-hot.timer"], check=False)
    print("场内溢价: 每个交易日 14:10。标普(含等权)<3% 与 纳指<5% 各弹一条，点确认收到才停")
    print("指数跌幅: 同在14:10检查，标普500/纳指100近5个已完成美股交易日累计下跌≥5%时独立提醒")
    print("场外额度: 工作日 09:00，以及开机后若当天还没点确认收到")
    print("场外>500元: 工作日 09:00-10:30 每10分钟弹窗，当天关掉3次即停")
    print(f"日志: {DAILY_DIR}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="场内纳指100/标普500 ETF 日溢价监控")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("scan")
    p_daily = sub.add_parser("daily")
    p_daily.add_argument("--force", action="store_true", help="覆盖当日已有日志")
    p_daily.add_argument("--no-alert", action="store_true", help="只记日志不提醒")
    p_otc = sub.add_parser("otc")
    p_otc.add_argument("--force", action="store_true", help="覆盖当日已有场外额度日志")
    p_otc.add_argument("--no-alert", action="store_true", help="只记日志不提醒")
    sub.add_parser("otc-hot")
    sub.add_parser("watch")
    sub.add_parser("test")
    p_ack = sub.add_parser("ack")
    p_ack.add_argument("alert_id", nargs="?")
    sub.add_parser("install")
    sub.add_parser("nag-queue")
    p_nag = sub.add_parser("nag")
    p_nag.add_argument("alert_id")
    p_nag.add_argument("title")
    p_nag.add_argument("body")
    args = parser.parse_args()
    cfg = load_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = args.cmd or "scan"
    if cmd == "scan":
        return cmd_scan(cfg)
    if cmd == "daily":
        return cmd_daily(cfg, force=args.force, alert=not args.no_alert)
    if cmd == "otc":
        return cmd_otc(cfg, force=args.force, alert=not args.no_alert)
    if cmd == "otc-hot":
        return cmd_otc_hot(cfg)
    if cmd == "watch":
        return cmd_watch(cfg)
    if cmd == "test":
        return cmd_test(cfg)
    if cmd == "ack":
        return cmd_ack(args.alert_id)
    if cmd == "install":
        return cmd_install()
    if cmd == "nag-queue":
        return cmd_nag_queue(cfg)
    if cmd == "nag":
        nag_until_ack(cfg, args.alert_id, args.title, args.body)
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
