# -*- coding: utf-8 -*-
"""按 cb/method.md 的口径筛选可转债，写出当日表格和短评。"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TODAY = date.today()
AS_OF = TODAY.isoformat()

JSL_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.jisilu.cn/data/cbnew/",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/json",
}

DEBT_ASSET_MAX = 70.0
INTEREST_DEBT_MAX = 50.0
LARGE_MV = 200 * 1e8
LARGE_ASSETS = 500 * 1e8
PREMIUM_LOOK = 15.0
SMALL_SIZE = (3.0, 4.0)

CENTRAL_KEYS = (
    "国务院",
    "财政部",
    "中央汇金",
    "中国投资有限责任公司",
    "全国社会保障基金",
)
LOCAL_KEYS = ("国有资产监督管理委员会", "国资委", "人民政府", "财政厅", "财政局")

THEME_NOISE = (
    "重仓",
    "新高",
    "减仓",
    "增仓",
    "机构",
    "融资融券",
    "沪股通",
    "深股通",
    "标普",
    "MSCI",
    "证金",
    "社保",
    "基金",
    "预盈",
    "预亏",
    "预增",
    "预减",
    "昨日",
    "近期",
    "百日",
    "龙虎榜",
    "破净",
    "高送转",
    "次新股",
    "创业板",
    "科创板",
    "标准普尔",
    "富时",
    "央视",
    "转债标的",
    "可转债",
    "融资标的",
    "融券",
    "板块",
    "亏损",
    "首亏",
    "扭亏",
    "微利",
    "小盘",
    "微盘",
    "破发",
    "破增",
    "超跌",
    "趋势",
    "新进",
    "增持",
    "减持",
    "股权",
    "风格",
    "市盈率",
    "市净率",
    "私募",
    "QFII",
    "外资",
    "中报",
    "三季报",
    "年报",
    "中证",
    "深成",
    "上证",
    "深证",
    "沪深",
    "成份",
    "大盘",
    "中盘",
    "权重股",
    "低价股",
    "周期股",
    "价值股",
    "成长",
    "红利股",
)
REGIONS = {
    "北京",
    "上海",
    "天津",
    "重庆",
    "河北",
    "山西",
    "辽宁",
    "吉林",
    "黑龙江",
    "江苏",
    "浙江",
    "安徽",
    "福建",
    "江西",
    "山东",
    "河南",
    "湖北",
    "湖南",
    "广东",
    "广西",
    "海南",
    "四川",
    "贵州",
    "云南",
    "西藏",
    "陕西",
    "甘肃",
    "青海",
    "宁夏",
    "新疆",
    "内蒙古",
    "深圳",
    "广州",
}

_local = threading.local()


def session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        s.headers["User-Agent"] = "Mozilla/5.0"
        _local.s = s
    return s


def get_json(url, params=None, method="GET", payload=None, headers=None, tries=4):
    last = None
    for i in range(tries):
        try:
            if method == "POST":
                r = session().post(url, params=params, json=payload, headers=headers, timeout=40)
            else:
                r = session().get(url, params=params, headers=headers, timeout=40)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(f"{url} 失败: {last}")


def fnum(value):
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_coupons(text: str) -> list[float]:
    if not text:
        return []
    found = re.findall(r"第[一二三四五六七八九十\d]+年[^0-9%]{0,8}([0-9]+(?:\.[0-9]+)?)\s*%", text)
    if found:
        return [float(x) for x in found]
    return [float(x) for x in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*%", text)]


def add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def ytm_pre_tax(price: float, flows: list[tuple[date, float]]) -> float | None:
    """税前到期收益率，百分数。现金流是每 100 元面值的到期前付款。"""
    future = [(d, c) for d, c in flows if (d - TODAY).days > 0 and c is not None]
    if price is None or price <= 0 or not future:
        return None
    times = [(d - TODAY).days / 365.0 for d, _ in future]

    def npv(y: float) -> float:
        return sum(c / ((1 + y) ** t) for t, (_, c) in zip(times, future)) - price

    lo, hi = -0.95, 5.0
    n_lo, n_hi = npv(lo), npv(hi)
    if n_lo < 0 and n_hi < 0:
        return None
    if n_lo > 0 and n_hi > 0:
        hi = 30.0
        if npv(hi) > 0:
            return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100


def fetch_jsl(path: str) -> list[dict]:
    url = f"https://www.jisilu.cn/data/cbnew/{path}"
    data = get_json(url, method="POST", payload={"rp": 500}, headers=JSL_HEADERS)
    return [row["cell"] for row in data.get("rows") or []]


def maturity_redeem_price(text: str | None) -> float | None:
    """到期赎回价（每 100 元面值）。条款里的有条件赎回价不拿。"""
    if not text:
        return None
    head = re.split(r"有条件赎回|条件赎回", text)[0]
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*[（(]\s*含最后", head)
    if match:
        return float(match.group(1))
    match = re.search(r"面值的\s*([0-9]+(?:\.[0-9]+)?)\s*%", head)
    if match:
        return float(match.group(1))
    match = re.search(r"上浮\s*([0-9]+(?:\.[0-9]+)?)\s*%", head)
    if match:
        return float(match.group(1))
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元", head)
    if match:
        return float(match.group(1))
    return None


def fetch_bond_terms(codes: list[str]) -> dict[str, dict]:
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    out = {}
    for i in range(0, len(codes), 40):
        batch = codes[i : i + 40]
        filt = ",".join(f'"{c}"' for c in batch)
        params = {
            "reportName": "RPT_BOND_CB_LIST",
            "columns": "SECURITY_CODE,INTEREST_RATE_EXPLAIN,VALUE_DATE,EXPIRE_DATE,RATING,REDEEM_CLAUSE",
            "filter": f"(SECURITY_CODE in ({filt}))",
            "pageSize": "200",
            "pageNumber": "1",
            "source": "WEB",
            "client": "WEB",
        }
        data = get_json(url, params=params)
        for row in (data.get("result") or {}).get("data") or []:
            out[row["SECURITY_CODE"]] = row
    return out


def fetch_quote_map() -> dict[str, dict]:
    """当日市值和 PE(TTM)。行情列表接口不稳定，改用数据中心估值表。"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    out = {}
    page = 1
    trade_date = TODAY.isoformat()
    while page <= 30:
        params = {
            "reportName": "RPT_VALUEANALYSIS_DET",
            "columns": "SECURITY_CODE,TOTAL_MARKET_CAP,PE_TTM",
            "filter": f"(TRADE_DATE='{trade_date}')",
            "pageSize": "500",
            "pageNumber": str(page),
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
            "source": "WEB",
            "client": "WEB",
        }
        data = get_json(url, params=params)
        result = data.get("result") or {}
        rows = result.get("data") or []
        for row in rows:
            code = str(row.get("SECURITY_CODE") or "")
            if code:
                out[code] = {"mv": fnum(row.get("TOTAL_MARKET_CAP")), "pe": fnum(row.get("PE_TTM"))}
        pages = result.get("pages") or 1
        if not rows or page >= pages:
            break
        page += 1
    return out


def fetch_pledge_map() -> tuple[dict[str, float], str]:
    """中登质押比例，单位是百分数。表里没有的正股稍后按 0 处理。"""
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    day = TODAY
    for _ in range(20):
        if day.weekday() >= 5:
            day -= timedelta(days=1)
            continue
        trade_date = day.isoformat()
        params = {
            "reportName": "RPT_CSDC_LIST",
            "columns": "SECURITY_CODE,PLEDGE_RATIO",
            "filter": f"(TRADE_DATE='{trade_date}')",
            "pageSize": "500",
            "pageNumber": "1",
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
            "source": "WEB",
            "client": "WEB",
        }
        data = get_json(url, params=params)
        result = data.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            day -= timedelta(days=1)
            continue
        out = {}
        pages = result.get("pages") or 1
        page = 1
        while True:
            for row in rows:
                code = str(row.get("SECURITY_CODE") or "")
                ratio = fnum(row.get("PLEDGE_RATIO"))
                if code and ratio is not None:
                    out[code] = ratio
            if page >= pages:
                break
            page += 1
            params["pageNumber"] = str(page)
            data = get_json(url, params=params)
            result = data.get("result") or {}
            rows = result.get("data") or []
            pages = result.get("pages") or page
        if out and max(out.values()) <= 1.5:
            out = {code: ratio * 100 for code, ratio in out.items()}
        return out, trade_date
    return {}, ""


def fetch_report_map(report_name: str, fields: tuple[str, ...], report_date: str = "2026-09-30") -> dict[str, dict]:
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    cols = "SECURITY_CODE," + ",".join(fields)
    filt = (
        '(SECURITY_TYPE_CODE in ("058001001","058001008"))'
        '(TRADE_MARKET_CODE!="069001017")'
        f"(REPORT_DATE='{report_date}')"
    )
    out = {}
    page = 1
    while page <= 30:
        params = {
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
            "pageSize": "500",
            "pageNumber": str(page),
            "reportName": report_name,
            "columns": cols,
            "filter": filt,
        }
        data = get_json(url, params=params)
        result = data.get("result") or {}
        rows = result.get("data") or []
        for row in rows:
            code = str(row.get("SECURITY_CODE") or "")
            if code:
                out[code] = {name: fnum(row.get(name)) for name in fields}
        pages = result.get("pages") or 1
        if not rows or page >= pages:
            break
        page += 1
    return out


def tencent_symbol(code: str, bond: bool) -> str:
    if bond:
        prefix = "sz" if code.startswith("12") else "sh"
    elif code.startswith(("4", "8", "92")):
        prefix = "bj"
    elif code.startswith(("5", "6", "9")):
        prefix = "sh"
    else:
        prefix = "sz"
    return prefix + code


def _recent_closes(rows) -> list[float]:
    parsed = []
    for item in rows:
        if len(item) < 3:
            continue
        try:
            bar_date = datetime.strptime(item[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        px = fnum(item[2])
        if px and px > 0:
            parsed.append((bar_date, px))
    if not parsed or parsed[-1][0] < TODAY - timedelta(days=15):
        return []
    start = TODAY - timedelta(days=400)
    return [px for bar_date, px in parsed if bar_date >= start][-252:]


def kline_closes(code: str, bond: bool) -> list[float]:
    """近一年收盘价。东方财富日线不稳定，用腾讯日线；前复权失败就退回不复权。"""
    symbol = tencent_symbol(code, bond)
    urls = []
    if not bond:
        urls.append(
            (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,320,qfq", "qfqday")
        )
    urls.append((f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={symbol},day,,,320,", "day"))
    for url, key in urls:
        try:
            data = get_json(url)
        except Exception:
            continue
        node = (data.get("data") or {}).get(symbol) or {}
        rows = node.get(key) or node.get("day") or []
        closes = _recent_closes(rows)
        if closes:
            return closes
    return []


def annualized_vol(closes: list[float]) -> tuple[float | None, int]:
    if len(closes) < 20:
        return None, len(closes)
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 20:
        return None, len(closes)
    return statistics.pstdev(rets) * math.sqrt(252) * 100, len(closes)


def num_or_zero(row: dict, key: str) -> float:
    return fnum(row.get(key)) or 0.0


def fetch_fundamentals(code: str) -> dict:
    sec = ("SH" if code.startswith(("5", "6", "9")) else "SZ") + code
    out = {
        "code": code,
        "report_date": None,
        "assets": None,
        "liab_ratio": None,
        "int_ratio": None,
        "int_components_missing": False,
        "opinion": None,
        "controller": None,
        "industry": None,
        "themes": [],
        "error": None,
    }
    try:
        balance = None
        for ctype in ("4", "3", "2", "1"):
            data = get_json(
                "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/zcfzbAjaxNew",
                params={
                    "companyType": ctype,
                    "reportDateType": "0",
                    "reportType": "1",
                    "dates": "2026-09-30,2026-06-30,2025-12-31",
                    "code": sec,
                },
            )
            rows = [row for row in (data.get("data") or []) if fnum(row.get("TOTAL_ASSETS"))]
            if not rows:
                continue
            rows.sort(key=lambda row: str(row.get("REPORT_DATE") or ""), reverse=True)
            balance = rows[0]
            annual = next((row for row in data.get("data") or [] if str(row.get("REPORT_DATE") or "").startswith("2025-12-31")), None)
            out["opinion"] = (annual or {}).get("OPINION_TYPE")
            break
        if balance is None:
            out["error"] = "无资产负债表"
            return out
        assets = fnum(balance.get("TOTAL_ASSETS"))
        liab = fnum(balance.get("TOTAL_LIABILITIES"))
        out["assets"] = assets
        out["report_date"] = str(balance.get("REPORT_DATE") or "")[:10]
        if assets and liab is not None:
            out["liab_ratio"] = liab / assets * 100
        keys = ("SHORT_LOAN", "LONG_LOAN", "BOND_PAYABLE", "SHORT_BOND_PAYABLE", "NONCURRENT_LIAB_1YEAR", "LEASE_LIAB")
        present = [fnum(balance.get(k)) for k in keys]
        if all(v is None for v in present):
            out["int_components_missing"] = True
        else:
            interest = sum(v or 0.0 for v in present)
            if assets:
                out["int_ratio"] = interest / assets * 100

        holder = get_json(
            "https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax",
            params={"code": sec},
        )
        sjkzr = holder.get("sjkzr") or []
        if sjkzr:
            out["controller"] = sjkzr[0].get("HOLDER_NAME")

        survey = get_json(
            "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax",
            params={"code": sec},
        )
        jbzl = (survey.get("jbzl") or [{}])[0]
        out["industry"] = jbzl.get("EM2016")

        ops = get_json(
            "https://emweb.securities.eastmoney.com/PC_HSF10/OperationsRequired/PageAjax",
            params={"code": sec},
        )
        industry_parts = set(re.split(r"[-—/]", out["industry"] or ""))
        themes = []
        for board in ops.get("ssbk") or []:
            name = (board.get("BOARD_NAME") or "").strip()
            if not name or name in industry_parts or name in REGIONS:
                continue
            if name.endswith(("Ⅰ", "Ⅱ", "Ⅲ", "_")) or "_" in name or name.upper().startswith("HS"):
                continue
            if any(flag in name for flag in THEME_NOISE):
                continue
            if name not in themes:
                themes.append(name)
            if len(themes) >= 4:
                break
        out["themes"] = themes
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def financial_kind(industry: str | None) -> str | None:
    text = industry or ""
    parts = [p.strip() for p in re.split(r"[-—/]", text) if p.strip()]
    for key in ("银行", "证券", "保险"):
        if any(p == key or p.startswith(key) for p in parts):
            return key
    return None


def owner_kind(controller: str | None) -> str | None:
    name = controller or ""
    if not name:
        return None
    if any(key in name for key in CENTRAL_KEYS):
        return "中央"
    if any(key in name for key in LOCAL_KEYS):
        return "地方"
    return None


def standard_opinion(opinion: str | None) -> bool:
    text = (opinion or "").strip()
    if "强调" in text or "其他事项" in text or "持续经营" in text:
        return False
    return text.startswith("标准无保留")


def redeem_blocked(cell: dict) -> str | None:
    icon = cell.get("redeem_icon") or ""
    if icon == "R":
        return "已公告强赎"
    if icon == "O":
        return "公告要强赎"
    if icon == "B":
        return "已满足强赎条件"
    count = re.sub(r"<[^>]+>", "", str(cell.get("redeem_count") or ""))
    match = re.search(r"(\d+)\s*/\s*(\d+)", count)
    if match and int(match.group(1)) >= int(match.group(2)) > 0 and icon != "G":
        return "已满足强赎条件"
    return None


def build_universe() -> tuple[list[dict], dict]:
    adjust = fetch_jsl("adjust_list/")
    redeem = {row["bond_id"]: row for row in fetch_jsl("redeem_list/")}
    codes = [row["bond_id"] for row in adjust]
    terms = fetch_bond_terms(codes)
    stats = {
        "listed": len(adjust),
        "eb": 0,
        "redeem": 0,
        "no_ytm": 0,
        "ytm_nonpositive": 0,
        "unissued": 0,
    }
    rows = []
    for item in adjust:
        code = item["bond_id"]
        base = redeem.get(code, {})
        btype = base.get("btype") or ""
        name = item.get("bond_nm") or ""
        if btype == "E" or "EB" in name.upper():
            stats["eb"] += 1
            continue
        reason = redeem_blocked(base)
        if reason:
            stats["redeem"] += 1
            continue
        price = fnum(item.get("price"))
        premium = fnum(item.get("premium_rt"))
        maturity = str(base.get("maturity_dt") or item.get("maturity_dt") or "")[:10]
        term = terms.get(code, {})
        value_raw = str(term.get("VALUE_DATE") or "")[:10]
        if value_raw:
            value_date = datetime.strptime(value_raw, "%Y-%m-%d").date()
            if value_date > TODAY:
                stats["unissued"] += 1
                continue
        coupons = parse_coupons(term.get("INTEREST_RATE_EXPLAIN") or "")
        redeem_price = maturity_redeem_price(term.get("REDEEM_CLAUSE"))
        ytm = None
        if price and value_raw and coupons and redeem_price:
            value = datetime.strptime(value_raw, "%Y-%m-%d").date()
            flows = []
            last = len(coupons)
            for i, rate in enumerate(coupons, start=1):
                when = add_years(value, i)
                flows.append((when, redeem_price if i == last else rate))
            ytm = ytm_pre_tax(price, flows)
        if ytm is None:
            stats["no_ytm"] += 1
            continue
        if ytm <= 0:
            stats["ytm_nonpositive"] += 1
            continue
        mat = datetime.strptime(maturity, "%Y-%m-%d").date() if maturity else None
        year_left = (mat - TODAY).days / 365.0 if mat else None
        icon = base.get("redeem_icon") or ""
        rows.append(
            {
                "code": code,
                "name": name,
                "stock_code": str(item.get("stock_id") or base.get("stock_id") or ""),
                "stock_name": item.get("stock_nm") or base.get("stock_nm") or "",
                "price": price,
                "premium": premium if premium is not None else 0.0,
                "dblow": price + (premium or 0.0),
                "size": fnum(item.get("curr_iss_amt")),
                "year_left": year_left,
                "maturity": maturity,
                "ytm": ytm,
                "rating": term.get("RATING") or "",
                "unadj": str(item.get("unadj_tips") or ""),
                "adj": str(item.get("adj_tips") or ""),
                "redeem_note": "公告不强赎" if icon == "G" else "",
                "pb": fnum(item.get("pb")),
            }
        )
    return rows, stats


def passes_debt(row: dict) -> tuple[bool, str]:
    kind = financial_kind(row.get("industry"))
    if kind:
        return True, kind
    owner = owner_kind(row.get("controller"))
    if owner == "中央":
        return True, "央企"
    if owner == "地方":
        mv = row.get("mv") or 0
        assets = row.get("assets") or 0
        if mv >= LARGE_MV or assets >= LARGE_ASSETS:
            return True, "大型地方国企"
    liab = row.get("liab_ratio")
    interest = row.get("int_ratio")
    if liab is None or interest is None:
        return False, ""
    if liab <= DEBT_ASSET_MAX and interest <= INTEREST_DEBT_MAX:
        return True, ""
    return False, ""


def fmt_pct(value, digits=1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def fmt_num(value, digits=2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def pe_text(pe) -> str:
    if pe is None:
        return "缺失"
    if pe < 0:
        return f"亏损（{pe:.1f}）"
    return f"{pe:.1f}"


def vol_text(vol, days) -> str:
    if vol is None:
        return "不足"
    suffix = "" if days >= 252 else f"（{days}日）"
    return f"{vol:.1f}%{suffix}"


def profit_flags(row: dict) -> list[str]:
    flags = []
    pe = row.get("pe")
    profit = row.get("profit")
    yoy = row.get("profit_yoy")
    ocf = row.get("ocf")
    if (pe is not None and pe < 0) or (profit is not None and profit < 0):
        flags.append("亏损")
    if profit is not None and profit > 0 and yoy is not None and yoy <= -30:
        flags.append("净利润同比下滑")
    financial = row.get("exempt") in ("银行", "证券", "保险")
    if ocf is not None and ocf < 0 and not financial:
        flags.append("经营现金流为负")
    return flags


def brief(row: dict) -> str:
    plus = []
    minus = []
    size = row.get("size")
    if size is None:
        size_txt = "剩余规模缺失。"
    elif SMALL_SIZE[0] <= size <= SMALL_SIZE[1]:
        size_txt = f"剩余规模 {size:.2f} 亿，落在 3–4 亿，弹性加分。"
        plus.append("规模")
    elif size < 2:
        size_txt = f"剩余规模 {size:.2f} 亿，不到 2 亿，流动性偏弱。"
        minus.append("规模过小")
    elif size > 30:
        size_txt = f"剩余规模 {size:.2f} 亿，超过 30 亿，弹性有限。"
        minus.append("规模偏大")
    else:
        size_txt = f"剩余规模 {size:.2f} 亿。"

    exempt = row.get("exempt") or ""
    liab = fmt_pct(row.get("liab_ratio"))
    interest = fmt_pct(row.get("int_ratio"))
    if exempt in ("银行", "证券", "保险"):
        quality = (
            f"{exempt}，高负债是行业结构，不因负债率减分。资产负债率 {liab}。"
            "有息负债率按产业债公式，不含吸收存款和同业负债，不拿来跟普通转债比。"
        )
    elif exempt:
        quality = f"{exempt}，负债率豁免，不因高负债减分。资产负债率 {liab}，有息负债率 {interest}。"
    else:
        quality = f"资产负债率 {liab}，有息负债率 {interest}。"
        if (row.get("liab_ratio") or 0) >= 60 or (row.get("int_ratio") or 0) >= 40:
            quality += "负债率不算低，质地要打折。"
            minus.append("负债偏高")
        else:
            quality += "负债率离上限还有距离。"

    pledge = row.get("pledge")
    if pledge is None:
        quality += "质押率缺失，这项不减分。"
    elif pledge >= 50:
        quality += f"正股质押率 {pledge:.1f}%，很高，质地明显减分。"
        minus.append("质押率很高")
    elif pledge >= 20:
        quality += f"正股质押率 {pledge:.1f}%，偏高，质地减分。"
        minus.append("质押偏高")
    else:
        quality += f"正股质押率 {pledge:.1f}%。"

    flags = profit_flags(row)
    if "亏损" in flags:
        quality += "正股 PE(TTM) 或 2026 年前三季净利润为负，经营减分。"
        minus.append("亏损")
    if "净利润同比下滑" in flags:
        quality += f"前三季净利润同比 {row['profit_yoy']:.0f}%，经营变差，减分。"
        minus.append("利润下滑")
    if "经营现金流为负" in flags:
        quality += "2026 年前三季经营现金流为负，减分。"
        minus.append("现金流")
    if not flags and not exempt:
        quality += "前三季没有看到亏损或利润大幅下滑。"

    industry = row.get("industry") or "行业缺失"
    themes = "、".join(row.get("themes") or []) or "没有单独抽出概念"
    theme_txt = f"行业是{industry}。题材：{themes}。"

    premium = row.get("premium") or 0
    stars = (row.get("unadj") or "").count("*")
    revised = "R" in (row.get("adj") or "")
    if premium <= PREMIUM_LOOK:
        prem_txt = f"转股溢价率 {premium:.1f}%，不超过 15%，下修意愿可以不看。"
        plus.append("低溢价")
    else:
        prem_txt = f"转股溢价率 {premium:.1f}%，高于 15%，要看下修。"
        if revised:
            prem_txt += "转股价标记里有下修记录。"
        if stars >= 2:
            prem_txt += f"集思录记下 {stars} 次不下修，下修意愿弱，减分。"
            minus.append("高溢价且多次不下修")
        elif stars == 1:
            prem_txt += "有过 1 次不下修，意愿一般。"
            minus.append("溢价偏高")
        else:
            prem_txt += "集思录没有不下修星号，意愿还不明，高溢价先算减分。"
            minus.append("高溢价")

    years = row.get("year_left")
    if years is None:
        term_txt = "剩余年限缺失。"
    elif years < 1:
        term_txt = f"剩余 {years:.2f} 年，不到 1 年，期限偏短。"
        minus.append("期限偏短")
    elif years > 6:
        term_txt = f"剩余 {years:.2f} 年，超过 6 年，债性要等比较久。"
        minus.append("期限偏长")
    else:
        term_txt = f"剩余 {years:.2f} 年，期限适中。"
        plus.append("期限")

    if row.get("redeem_note"):
        term_txt += "公司公告过本次不强赎，仍要盯下一轮计数。"
    if row.get("bond_vol") is None:
        term_txt += "转债没有近期日线，波动率空着，价格要再对一下成交。"

    head = []
    if plus:
        head.append("加分：" + "、".join(plus))
    if minus:
        head.append("减分：" + "、".join(minus))
    if not head:
        head.append("没有明显的加减分")
    return " ".join(head) + "。" + size_txt + quality + theme_txt + prem_txt + term_txt


def write_outputs(rows: list[dict], stats: dict) -> tuple[Path, Path]:
    DATA.mkdir(parents=True, exist_ok=True)
    stamp = TODAY.strftime("%Y%m%d")
    csv_path = DATA / f"screen-{stamp}.csv"
    md_path = ROOT / f"screen-{stamp}.md"
    fields = [
        "代码",
        "名称",
        "正股代码",
        "正股",
        "剩余规模亿",
        "资产负债率",
        "有息负债率",
        "质押率",
        "财报是否非标",
        "行业",
        "题材",
        "价格",
        "溢价率",
        "剩余年限",
        "税前到期收益率",
        "PE_TTM",
        "正股波动率",
        "转债波动率",
        "双低",
        "豁免原因",
        "评级",
        "强赎备注",
        "实控人",
        "财报期",
        "短评",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "代码": row["code"],
                    "名称": row["name"],
                    "正股代码": row["stock_code"],
                    "正股": row["stock_name"],
                    "剩余规模亿": fmt_num(row.get("size")),
                    "资产负债率": fmt_pct(row.get("liab_ratio")),
                    "有息负债率": fmt_pct(row.get("int_ratio")),
                    "质押率": fmt_pct(row.get("pledge")),
                    "财报是否非标": "否",
                    "行业": row.get("industry") or "",
                    "题材": "、".join(row.get("themes") or []),
                    "价格": fmt_num(row.get("price")),
                    "溢价率": fmt_pct(row.get("premium")),
                    "剩余年限": fmt_num(row.get("year_left")),
                    "税前到期收益率": fmt_pct(row.get("ytm"), 2),
                    "PE_TTM": pe_text(row.get("pe")),
                    "正股波动率": vol_text(row.get("stock_vol"), row.get("stock_days") or 0),
                    "转债波动率": vol_text(row.get("bond_vol"), row.get("bond_days") or 0),
                    "双低": fmt_num(row.get("dblow"), 1),
                    "豁免原因": row.get("exempt") or "",
                    "评级": row.get("rating") or "",
                    "强赎备注": row.get("redeem_note") or "",
                    "实控人": row.get("controller") or "",
                    "财报期": row.get("report_date") or "",
                    "短评": row.get("brief") or "",
                }
            )

    lines = [
        f"# 可转债筛选 {AS_OF}",
        "",
        "口径见 [method.md](method.md)。双低 = 现价 + 转股溢价率 × 100，从小到大。税前到期收益率按集思录口径自算。质押率是质押股数 / 总股本，日期 "
        + (stats.get("pledge_date") or "缺失")
        + "。行业用东方财富三级路径。不构成投资建议。",
        "",
        (
            f"集思录在册 {stats['listed']} 只。去掉可交换债 {stats['eb']} 只、"
            f"已公告或已满足强赎 {stats['redeem']} 只、尚未起息 {stats.get('unissued', 0)} 只、"
            f"算不出收益率 {stats['no_ytm']} 只、"
            f"税前到期收益率不大于 0 的 {stats['ytm_nonpositive']} 只。"
            f"剩下 {stats['after_market']} 只再看财报：非标或没有 2025 年报标准意见 {stats['audit']} 只，"
            f"普通主体负债率超线 {stats['debt']} 只，财报没取全 {stats['missing']} 只。"
            f"还没上市成交 {stats.get('untraded', 0)} 只。"
            f"过筛 {len(rows)} 只。"
        ),
        "",
        "## 筛选结果",
        "",
        "| 代码 | 名称 | 规模 | 资产负债率 | 有息负债率 | 质押率 | 非标 | 行业 | 题材 | 价格 | 溢价率 | 剩余年限 | PE(TTM) | 正股波动 | 转债波动 | 双低 | 豁免 |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        themes = "、".join(row.get("themes") or []) or "—"
        industry = (row.get("industry") or "—").replace("|", "/")
        lines.append(
            "| {code} | {name} | {size} | {liab} | {interest} | {pledge} | 否 | {industry} | {themes} | {price} | {premium} | {years} | {pe} | {sv} | {bv} | {dblow} | {exempt} |".format(
                code=row["code"],
                name=row["name"],
                size=fmt_num(row.get("size")),
                liab=fmt_pct(row.get("liab_ratio")),
                interest=fmt_pct(row.get("int_ratio")),
                pledge=fmt_pct(row.get("pledge")),
                industry=industry,
                themes=themes.replace("|", "/"),
                price=fmt_num(row.get("price")),
                premium=fmt_pct(row.get("premium")),
                years=fmt_num(row.get("year_left")),
                pe=pe_text(row.get("pe")),
                sv=vol_text(row.get("stock_vol"), row.get("stock_days") or 0),
                bv=vol_text(row.get("bond_vol"), row.get("bond_days") or 0),
                dblow=fmt_num(row.get("dblow"), 1),
                exempt=row.get("exempt") or "—",
            )
        )
    lines.extend(["", "## 短评", ""])
    for row in rows:
        lines.append(f"### {row['name']}（{row['code']}）双低 {row['dblow']:.1f}")
        lines.append("")
        extra = (
            f"正股 {row['stock_name']}（{row['stock_code']}），评级 {row.get('rating') or '—'}，"
            f"税前到期收益率 {fmt_pct(row.get('ytm'), 2)}，实控人 {row.get('controller') or '—'}，"
            f"财报期 {row.get('report_date') or '—'}。"
        )
        if row.get("redeem_note"):
            extra += row["redeem_note"] + "。"
        lines.append(extra + row["brief"])
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    write_html(csv_path, ROOT / f"screen-{stamp}.html", stats.get("pledge_date") or "")
    return md_path, csv_path


def revision_will(premium: str, brief: str) -> str:
    """溢价率不超过 15% 时不写下修。其余只留意愿。"""
    number = fnum(str(premium).replace("%", "").replace("—", ""))
    if number is None or number <= PREMIUM_LOOK:
        return ""
    if "下修意愿弱" in brief:
        return "弱"
    if "意愿一般" in brief:
        return "一般"
    if "有下修记录" in brief:
        return "有下修"
    if "意愿还不明" in brief:
        return "不明"
    return ""


def sort_value(text: str) -> str:
    raw = (text or "").strip()
    if raw in ("", "—", "不足", "缺失"):
        return ""
    match = re.search(r"-?\d+(?:\.\d+)?", raw.replace(",", ""))
    if match and not re.search(r"[A-Za-z\u4e00-\u9fff]", raw.replace("亏损", "")):
        return match.group(0)
    if raw.startswith("亏损"):
        inner = re.search(r"-?\d+(?:\.\d+)?", raw)
        return inner.group(0) if inner else ""
    if re.fullmatch(r"-?\d+(?:\.\d+)?%?", raw):
        return raw.replace("%", "")
    vol = re.match(r"^(-?\d+(?:\.\d+)?)%", raw)
    if vol:
        return vol.group(1)
    return ""


def write_html(csv_path: Path, html_path: Path, pledge_date: str) -> None:
    import html as html_lib

    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    columns = [
        ("代码", "代码", "text"),
        ("名称", "名称", "text"),
        ("正股", "正股", "text"),
        ("规模", "剩余规模亿", "num"),
        ("资产负债率", "资产负债率", "num"),
        ("有息负债率", "有息负债率", "num"),
        ("质押率", "质押率", "num"),
        ("行业", "行业", "text"),
        ("题材", "题材", "text"),
        ("价格", "价格", "num"),
        ("溢价率", "溢价率", "num"),
        ("下修意愿", "", "text"),
        ("剩余年限", "剩余年限", "num"),
        ("税前收益", "税前到期收益率", "num"),
        ("PE(TTM)", "PE_TTM", "num"),
        ("正股波动", "正股波动率", "num"),
        ("转债波动", "转债波动率", "num"),
        ("双低", "双低", "num"),
        ("豁免", "豁免原因", "text"),
        ("评级", "评级", "text"),
    ]
    body = []
    for index, row in enumerate(rows):
        will = revision_will(row.get("溢价率") or "", row.get("短评") or "")
        cells = []
        for title, key, kind in columns:
            shown = will if title == "下修意愿" else (row.get(key) or "")
            if kind == "num":
                value = sort_value(shown)
            else:
                value = shown
            css = ' class="name"' if title == "名称" else ""
            field = {
                "规模": "size",
                "资产负债率": "liab",
                "有息负债率": "interest",
                "质押率": "pledge",
                "价格": "price",
                "溢价率": "premium",
                "剩余年限": "years",
                "税前收益": "ytm",
                "PE(TTM)": "pe",
                "评级": "rating",
            }.get(title, "")
            key_attr = f' data-k="{field}"' if field else ""
            cells.append(
                f'<td{css}{key_attr} data-v="{html_lib.escape(value, quote=True)}">{html_lib.escape(shown)}</td>'
            )
        brief = html_lib.escape(row.get("短评") or "")
        body.append(
            f'<tr data-i="{index}">{"".join(cells)}</tr>'
            f'<tr class="note" hidden><td colspan="{len(columns)}">{brief}</td></tr>'
        )
    heads = "".join(
        f'<th data-kind="{kind}">{html_lib.escape(title)}</th>' for title, _, kind in columns
    )
    date_text = csv_path.stem.replace("screen-", "")
    pledge = f"质押率日期 {pledge_date}。" if pledge_date else ""
    rating_order = ["AAA", "AAA-", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-"]
    ratings = sorted(
        {(row.get("评级") or "").strip() for row in rows if (row.get("评级") or "").strip()},
        key=lambda item: rating_order.index(item) if item in rating_order else len(rating_order),
    )
    rating_options = "".join(
        f'<option value="{html_lib.escape(item, quote=True)}">{html_lib.escape(item)}</option>'
        for item in ratings
    )
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>可转债筛选 {date_text}</title>
<style>
body {{ margin: 0; height: 100vh; display: flex; flex-direction: column; font: 14px/1.45 "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; color: #1c1c1c; background: #f4f5f7; }}
header {{ padding: 16px 20px 8px; }}
h1 {{ margin: 0 0 6px; font-size: 20px; font-weight: 650; }}
p {{ margin: 0; color: #555; }}
form {{ display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin: 0 16px; padding: 10px 12px; background: #fff; border: 1px solid #e3e5e8; }}
label {{ display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }}
input, select, button {{ font: inherit; }}
input[type="number"] {{ width: 72px; padding: 3px 6px; border: 1px solid #cfd3d8; border-radius: 4px; }}
select {{ padding: 3px 6px; border: 1px solid #cfd3d8; border-radius: 4px; background: #fff; }}
button {{ padding: 4px 10px; border: 1px solid #cfd3d8; border-radius: 4px; background: #f7f8fa; cursor: pointer; }}
#shown {{ color: #555; }}
.wrap {{ overflow: auto; flex: 1; margin: 12px 16px 20px; background: #fff; border: 1px solid #e3e5e8; }}
table {{ border-collapse: collapse; min-width: 100%; white-space: nowrap; }}
th, td {{ padding: 6px 10px; border-bottom: 1px solid #eceef1; text-align: left; vertical-align: top; }}
th {{ position: sticky; top: 0; background: #f7f8fa; cursor: pointer; font-weight: 650; z-index: 1; }}
th.num, td.num {{ text-align: right; }}
tbody tr:hover td {{ background: #f3f7fb; }}
tr.note td {{ white-space: normal; color: #333; background: #fafbfc; max-width: 960px; }}
tr.filtered {{ display: none; }}
.name {{ cursor: pointer; text-decoration: underline; text-underline-offset: 2px; }}
</style>
</head>
<body>
<header>
<h1>可转债筛选 {date_text}</h1>
<p>双低从小到大。点表头排序，点名称展开短评。上面的条件留空表示不限制。溢价率不超过 15% 时，下修意愿留空。{pledge}不构成投资建议。</p>
</header>
<form id="filters">
<label>价格 <input name="priceMin" type="number" step="any" inputmode="decimal"> 到 <input name="priceMax" type="number" step="any" inputmode="decimal"> 元</label>
<label>剩余规模 ≤ <input name="sizeMax" type="number" step="any" inputmode="decimal"> 亿</label>
<label>资产负债率 ≤ <input name="liabMax" type="number" step="any" inputmode="decimal"> %</label>
<label>有息负债率 ≤ <input name="interestMax" type="number" step="any" inputmode="decimal"> %</label>
<label>转股溢价率 ≤ <input name="premiumMax" type="number" step="any" inputmode="decimal"> %</label>
<label>到期收益率 ≥ <input name="ytmMin" type="number" step="any" inputmode="decimal"> %</label>
<label>剩余年限 <input name="yearsMin" type="number" step="any" inputmode="decimal"> 到 <input name="yearsMax" type="number" step="any" inputmode="decimal"> 年</label>
<label>评级 <select name="rating"><option value="">全部</option>{rating_options}</select></label>
<label><input name="loss" type="checkbox"> 剔除亏损公司</label>
<label>质押率 ≤ <input name="pledgeMax" type="number" step="any" inputmode="decimal"> %</label>
<button type="button" id="clear">清空</button>
<span id="shown"></span>
</form>
<div class="wrap">
<table>
<thead><tr>{heads}</tr></thead>
<tbody>
{"".join(body)}
</tbody>
</table>
</div>
<script>
const table = document.querySelector("table");
const tbody = table.tBodies[0];
const heads = [...table.tHead.rows[0].cells];
heads.forEach((th, col) => {{
  const numeric = th.dataset.kind === "num";
  if (numeric) th.classList.add("num");
  [...tbody.rows].forEach((tr) => {{
    if (!tr.classList.contains("note") && tr.cells[col]) tr.cells[col].classList.toggle("num", numeric);
  }});
  th.addEventListener("click", () => {{
    const dir = th.dataset.dir === "asc" ? "desc" : "asc";
    heads.forEach((item) => {{ item.dataset.dir = ""; item.textContent = item.textContent.replace(/ [↑↓]$/, ""); }});
    th.dataset.dir = dir;
    th.textContent += dir === "asc" ? " ↑" : " ↓";
    const pairs = [];
    for (let i = 0; i < tbody.rows.length; i += 2) pairs.push([tbody.rows[i], tbody.rows[i + 1]]);
    pairs.sort((a, b) => {{
      const av = a[0].cells[col].dataset.v;
      const bv = b[0].cells[col].dataset.v;
      const ae = av === "";
      const be = bv === "";
      if (ae || be) return ae - be;
      if (numeric) return dir === "asc" ? Number(av) - Number(bv) : Number(bv) - Number(av);
      return dir === "asc" ? av.localeCompare(bv, "zh") : bv.localeCompare(av, "zh");
    }});
    pairs.forEach(([main, note]) => {{ tbody.append(main); tbody.append(note); }});
  }});
}});
tbody.addEventListener("click", (event) => {{
  const cell = event.target.closest("td");
  if (!cell || cell.cellIndex !== 1) return;
  const note = cell.parentElement.nextElementSibling;
  if (note && note.classList.contains("note")) note.hidden = !note.hidden;
}});
const form = document.getElementById("filters");
const shown = document.getElementById("shown");
const col = {{}};
document.querySelectorAll("tbody tr:not(.note) td[data-k]").forEach((td) => {{
  if (!(td.dataset.k in col)) col[td.dataset.k] = td.cellIndex;
}});
function pairs() {{
  const out = [];
  for (let i = 0; i < tbody.rows.length; i += 2) out.push([tbody.rows[i], tbody.rows[i + 1]]);
  return out;
}}
function limit(name) {{
  const raw = form.elements[name].value.trim();
  if (raw === "") return null;
  const n = Number(raw);
  return Number.isNaN(n) ? null : n;
}}
function cellNum(tr, key) {{
  const value = tr.cells[col[key]].dataset.v;
  if (value === "") return null;
  const n = Number(value);
  return Number.isNaN(n) ? null : n;
}}
function within(value, low, high) {{
  if (low === null && high === null) return true;
  if (value === null) return false;
  if (low !== null && value < low) return false;
  if (high !== null && value > high) return false;
  return true;
}}
function applyFilters() {{
  const rules = [
    ["price", limit("priceMin"), limit("priceMax")],
    ["size", null, limit("sizeMax")],
    ["liab", null, limit("liabMax")],
    ["interest", null, limit("interestMax")],
    ["premium", null, limit("premiumMax")],
    ["ytm", limit("ytmMin"), null],
    ["years", limit("yearsMin"), limit("yearsMax")],
    ["pledge", null, limit("pledgeMax")],
  ];
  const rating = form.elements.rating.value;
  const dropLoss = form.elements.loss.checked;
  let visible = 0;
  const all = pairs();
  all.forEach(([main, note]) => {{
    let ok = rules.every(([key, low, high]) => within(cellNum(main, key), low, high));
    if (ok && rating && main.cells[col.rating].dataset.v !== rating) ok = false;
    if (ok && dropLoss && cellNum(main, "pe") < 0) ok = false;
    main.classList.toggle("filtered", !ok);
    note.classList.toggle("filtered", !ok);
    if (ok) visible += 1;
  }});
  shown.textContent = "当前 " + visible + " / 共 " + all.length;
}}
form.addEventListener("input", applyFilters);
document.getElementById("clear").addEventListener("click", () => {{
  form.reset();
  applyFilters();
}});
applyFilters();
</script>
</body>
</html>
"""
    html_path.write_text(page, encoding="utf-8")


def main() -> None:
    print("拉取转债行情", flush=True)
    universe, stats = build_universe()
    stats["after_market"] = len(universe)
    print(f"收益率和强赎筛完 {len(universe)} 只", flush=True)
    quotes = fetch_quote_map()
    print(f"行情 {len(quotes)}", flush=True)
    pledges, pledge_date = fetch_pledge_map()
    stats["pledge_date"] = pledge_date
    print(f"质押 {len(pledges)} 日期 {pledge_date} 美锦 {pledges.get('000723')}", flush=True)
    income = fetch_report_map("RPT_DMSK_FN_INCOME", ("PARENT_NETPROFIT", "PARENT_NETPROFIT_RATIO"))
    cash = fetch_report_map("RPT_DMSK_FN_CASHFLOW", ("NETCASH_OPERATE",))
    print(f"利润表 {len(income)} 现金流 {len(cash)}", flush=True)

    stocks = sorted({row["stock_code"] for row in universe if row["stock_code"]})
    fundamentals = {}
    print(f"补财报 {len(stocks)} 家", flush=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(fetch_fundamentals, code): code for code in stocks}
        done = 0
        for fut in as_completed(futs):
            result = fut.result()
            fundamentals[result["code"]] = result
            done += 1
            if done % 25 == 0 or done == len(stocks):
                print(f"财报 {done}/{len(stocks)}", flush=True)

    kept = []
    stats.update({"audit": 0, "debt": 0, "missing": 0})
    for row in universe:
        fund = fundamentals.get(row["stock_code"]) or {}
        quote = quotes.get(row["stock_code"]) or {}
        inc = income.get(row["stock_code"]) or {}
        cf = cash.get(row["stock_code"]) or {}
        row.update(
            {
                "report_date": fund.get("report_date"),
                "assets": fund.get("assets"),
                "liab_ratio": fund.get("liab_ratio"),
                "int_ratio": fund.get("int_ratio"),
                "opinion": fund.get("opinion"),
                "controller": fund.get("controller"),
                "industry": fund.get("industry"),
                "themes": fund.get("themes") or [],
                "mv": quote.get("mv"),
                "pe": quote.get("pe"),
                "profit": inc.get("PARENT_NETPROFIT"),
                "profit_yoy": inc.get("PARENT_NETPROFIT_RATIO"),
                "ocf": cf.get("NETCASH_OPERATE"),
                "pledge": pledges.get(row["stock_code"], 0.0) if pledges else None,
                "fund_error": fund.get("error"),
            }
        )
        if fund.get("error") or row["liab_ratio"] is None:
            stats["missing"] += 1
            continue
        if not standard_opinion(row.get("opinion")):
            stats["audit"] += 1
            continue
        ok, exempt = passes_debt(row)
        if not ok:
            if row.get("int_ratio") is None and financial_kind(row.get("industry")) is None:
                stats["missing"] += 1
            else:
                stats["debt"] += 1
            continue
        row["exempt"] = exempt
        kept.append(row)

    print(f"过筛 {len(kept)}，补波动率", flush=True)

    def vols(row):
        try:
            closes = kline_closes(row["stock_code"], bond=False)
            stock_vol, stock_days = annualized_vol(closes)
        except Exception:
            stock_vol, stock_days = None, 0
        try:
            closes = kline_closes(row["code"], bond=True)
            bond_vol, bond_days = annualized_vol(closes)
        except Exception:
            bond_vol, bond_days = None, 0
        return row["code"], stock_vol, stock_days, bond_vol, bond_days

    with ThreadPoolExecutor(max_workers=4) as pool:
        for code, sv, sd, bv, bd in pool.map(vols, kept):
            for row in kept:
                if row["code"] == code:
                    row["stock_vol"] = sv
                    row["stock_days"] = sd
                    row["bond_vol"] = bv
                    row["bond_days"] = bd
                    break

    no_trade = [row for row in kept if not row.get("bond_days")]
    if no_trade and len(no_trade) * 2 < len(kept):
        stats["untraded"] = len(no_trade)
        kept = [row for row in kept if row.get("bond_days")]
    else:
        stats["untraded"] = 0

    kept.sort(key=lambda row: (row["dblow"], row["code"]))
    for row in kept:
        row["brief"] = brief(row)
    md_path, csv_path = write_outputs(kept, stats)
    print(md_path)
    print(csv_path)
    print(ROOT / f"screen-{TODAY.strftime('%Y%m%d')}.html")
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--html":
        stamp = TODAY.strftime("%Y%m%d")
        md = (ROOT / f"screen-{stamp}.md").read_text(encoding="utf-8")
        found = re.search(r"日期 (\d{4}-\d{2}-\d{2})", md)
        write_html(
            DATA / f"screen-{stamp}.csv",
            ROOT / f"screen-{stamp}.html",
            found.group(1) if found else "",
        )
        print(ROOT / f"screen-{stamp}.html")
    else:
        main()
