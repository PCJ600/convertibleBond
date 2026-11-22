# -*- coding: utf-8 -*-
import math
from datetime import date
import requests

ASOF = date(2026, 11, 22)
SCREEN = date(2026, 11, 19)
pre = [(date(2027, 8, 5), 1.80), (date(2028, 8, 5), 112.0)]
after = [(date(2027, 8, 5), 1.44), (date(2028, 8, 5), 109.60)]


def ytm(price, flows, asof):
    lo, hi = -0.5, 1.5
    for _ in range(80):
        mid = (lo + hi) / 2
        pv = sum(cf / (1 + mid) ** ((d - asof).days / 365) for d, cf in flows)
        if pv > price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def price_at(y, flows, asof):
    return sum(cf / (1 + y) ** ((d - asof).days / 365) for d, cf in flows)


for d in [date(2027, 8, 5), date(2028, 8, 4), date(2028, 8, 5)]:
    print(d, d.strftime("%A"))
print("days", [(d, (d - ASOF).days) for d, _ in pre])
print("screen", ytm(111.030, pre, SCREEN), "years", (date(2028, 8, 5) - SCREEN).days / 365)
print("1120", ytm(111.722, pre, ASOF), ytm(111.722, after, ASOF))
for y in (0, 0.015, 0.03, 0.035, 0.04, 0.045, 0.05, 0.10):
    p = price_at(y, pre, ASOF)
    print(f"  {y:.1%} -> {p:.2f} after {ytm(p, after, ASOF):.2%}")
print("sums", sum(c for _, c in after), sum(c for _, c in pre), "after0", ytm(sum(c for _, c in after), pre, ASOF))

assets, cl, ca, inv = 333.9459, 122.5901, 170.9625, 7.3506
cash, cce = 46.5864, 7.3067
print("ratios", ca / cl, (ca - inv) / cl, cash / cl, cce / cl)
interest = 61.7077 + 18.7941 + 0.1733 + 0.07
print("int", interest, interest / assets, "near", 61.7077 + 0.1733, "cce gap", cce - (61.7077 + 0.1733), "cash gap", cash - (61.7077 + 0.1733))
print("debt", 147.5489 / assets, "ar", 12.1634 / assets)
print("yoy", (6.0325 - 18.3249) / 18.3249, "rev", (93.0026 - 132.1386) / 132.1386)
print("fy", (19.9676 - 25.9913) / 25.9913)
print("maturity", 20 * 1.12)
parent, other, shares = 187.7166, 4.2433, 6.0915
ordi = (parent - other) / shares
print("ye bps", parent / shares, ordi)
print("after div", ordi - 1.23 - 1.237)
qp, qs = 186.0673, 6.0573
print("q3", qp / qs, (qp - 4.2433) / qs)

session = requests.Session()
session.trust_env = False
session.headers["User-Agent"] = "Mozilla/5.0"
rows = session.get("https://web.ifzq.gtimg.cn/appstock/app/kline/kline", params={"param": "sh603833,day,,,25,"}, timeout=20).json()["data"]["sh603833"]["day"]
closes = [float(x[2]) for x in rows[-20:]]
print("avg", round(sum(closes) / 20, 2), "n", len(closes))
line, put = 51.29 * 0.80, 51.29 * 0.70
print("below", round(line, 2), sum(p < line for p in closes), "put", round(put, 2), sum(p < put for p in closes))
for x in rows[-20:]:
    print(x[0], x[2])
print("BOND")
for x in session.get("https://web.ifzq.gtimg.cn/appstock/app/kline/kline", params={"param": "sh113655,day,,,8,"}, timeout=20).json()["data"]["sh113655"]["day"][-6:]:
    print(x[0], x[2])


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def call(S, K, T, r, sig):
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2)


S, conv = 28.70, 51.29
T = (date(2028, 8, 5) - ASOF).days / 365
ratio = 100 / conv
K, cap = 112 / ratio, conv * 1.30
uncap = call(S, K, T, 0.015, 0.344) * ratio
capped = (call(S, K, T, 0.015, 0.344) - call(S, cap, T, 0.015, 0.344)) * ratio
avg = sum(closes) / 20
print("T", round(T, 2), "K", round(K, 2), "cap", round(cap, 2), "uncap", round(uncap, 1), "capped", round(capped, 1))
print("cv", S / conv * 100, "cv avg", S / avg * 100)
