# -*- coding: utf-8 -*-
import requests

session = requests.Session()
session.trust_env = False
session.headers["User-Agent"] = "Mozilla/5.0"
dates = "2026-09-30,2025-09-30,2026-06-30,2025-12-31,2024-12-31"
keys_bs = [
    "REPORT_DATE", "TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY",
    "TOTAL_PARENT_EQUITY", "MINORITY_EQUITY", "SHARE_CAPITAL", "OTHER_EQUITY_TOOL",
    "UNASSIGN_RPOFIT", "MONETARYFUNDS", "INVENTORY", "TOTAL_CURRENT_ASSETS",
    "TOTAL_CURRENT_LIAB", "ACCOUNTS_RECE", "CONTRACT_ASSET", "NOTE_RECE",
    "FINANCE_RECE", "SHORT_LOAN", "LONG_LOAN", "BOND_PAYABLE", "SHORT_BOND_PAYABLE",
    "NONCURRENT_LIAB_1YEAR", "LEASE_LIAB", "LONG_PAYABLE", "NOTE_PAYABLE",
    "FIXED_ASSET", "CIP", "TRADE_FINASSET",
]
keys_is = ["REPORT_DATE", "PARENT_NETPROFIT", "NETPROFIT", "OPERATE_INCOME", "TOTAL_OPERATE_INCOME"]
keys_cf = ["REPORT_DATE", "NETCASH_OPERATE", "NETCASH_INVEST", "NETCASH_FINANCE", "END_CCE"]


def show(v):
    if isinstance(v, (int, float)) and abs(v) > 1000:
        return f"{v} {round(v / 1e8, 4)}"
    return v


def fetch(kind, keys):
    url = f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/{kind}AjaxNew"
    r = session.get(
        url,
        params={"companyType": "4", "reportDateType": "0", "reportType": "1", "code": "SH603833", "dates": dates},
        timeout=30,
    )
    print(kind, r.status_code, r.text[:80].replace("\n", " "))
    rows = (r.json().get("data") or [])
    print("N", len(rows))
    for row in rows:
        print("---", row.get("REPORT_DATE"))
        for k in keys:
            if k in row and row[k] not in (None, ""):
                print(k, show(row[k]))


fetch("zcfzb", keys_bs)
fetch("lrb", keys_is)
fetch("xjllb", keys_cf)
for code in ("sh113655", "sh603833"):
    print(session.get(f"https://qt.gtimg.cn/q={code}", timeout=20).text[:650])
