#!/usr/bin/env python3
"""Probe stock/index tickers with the correct slug format (month-day-year)."""

import json
import sys
import requests
from datetime import datetime, timedelta
import pytz

GAMMA_API = "https://gamma-api.polymarket.com"
EST = pytz.timezone("US/Eastern")
now = datetime.now(EST)

print(f"Time (ET): {now:%Y-%m-%d %H:%M:%S %Z}")

TICKERS = [
    "spx", "tsla", "aapl", "goog", "googl", "amzn", "msft", "nvda", "meta",
    "nflx", "amd", "coin", "spy", "qqq", "iwm", "dia",
    "gold", "silver", "oil", "nasdaq", "dow",
    "tesla", "apple", "google", "amazon", "microsoft", "nvidia",
    "sp500", "s-and-p-500", "s-p-500",
]

TEMPLATES = {
    "opens": "{ticker}-opens-up-or-down-on-{month}-{day}-{year}",
    "close": "{ticker}-up-or-down-on-{month}-{day}-{year}",
}

dates = []
for i in range(7):
    d = now + timedelta(days=i)
    dates.append(d)

results = []
for dt in dates:
    month = dt.strftime("%B").lower()
    day = dt.day
    year = dt.year
    date_str = dt.strftime("%Y-%m-%d (%A)")
    print(f"\n--- {date_str} ---")

    for tk in TICKERS:
        for var, tmpl in TEMPLATES.items():
            slug = tmpl.format(ticker=tk, month=month, day=day, year=year)
            try:
                r = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=10)
                if r.status_code == 200:
                    ev = r.json()
                    m = (ev.get("markets") or [{}])[0]
                    tids = m.get("clobTokenIds", "")
                    if isinstance(tids, str):
                        try:
                            tids = json.loads(tids)
                        except:
                            pass
                    outs = m.get("outcomes", "")
                    if isinstance(outs, str):
                        try:
                            outs = json.loads(outs)
                        except:
                            pass
                    prices = m.get("outcomePrices", "")
                    print(f"  HIT  [{var:5s}] {slug}")
                    print(f"       title={ev.get('title','')}")
                    print(f"       end={ev.get('endDate','')} active={ev.get('active')}")
                    print(f"       tokens={tids}")
                    print(f"       outcomes={outs} prices={prices}")
                    print(f"       volume={m.get('volume','')} liquidity={m.get('liquidity','')}")
                    results.append({
                        "slug": slug, "ticker": tk, "variant": var,
                        "date": date_str, "title": ev.get("title", ""),
                        "end_date": ev.get("endDate", ""),
                        "active": ev.get("active"),
                        "token_ids": tids, "outcomes": outs, "prices": prices,
                    })
            except Exception as e:
                print(f"  ERR  {slug}: {e}", file=sys.stderr)

print(f"\n\n=== TOTAL RESULTS: {len(results)} ===")
tickers_found = sorted(set(r["ticker"] for r in results))
print(f"Tickers with markets: {tickers_found}")
for tk in tickers_found:
    tk_results = [r for r in results if r["ticker"] == tk]
    print(f"\n  {tk.upper()}: {len(tk_results)} markets")
    for r in tk_results:
        print(f"    [{r['variant']:5s}] {r['slug']} | active={r['active']} | end={r['end_date']}")
