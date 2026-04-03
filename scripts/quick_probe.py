#!/usr/bin/env python3
"""Quick probe to discover slug format for daily finance markets."""

import json
import sys
import requests
from datetime import datetime
import pytz

GAMMA_API = "https://gamma-api.polymarket.com"
EST = pytz.timezone("US/Eastern")
now = datetime.now(EST)

print(f"Time (ET): {now:%Y-%m-%d %H:%M:%S %Z}")
print()

month = now.strftime("%B").lower()
day = now.day
year = now.year

# Try a few different slug formats for SPX today
test_slugs = [
    f"spx-opens-up-or-down-on-{month}{day}-{year}",
    f"spx-up-or-down-on-{month}{day}-{year}",
    f"spx-opens-up-or-down-on-{month}-{day}-{year}",
    f"spx-up-or-down-on-{month}-{day}-{year}",
    f"sp500-up-or-down-on-{month}{day}-{year}",
    f"s-and-p-500-up-or-down-on-{month}{day}-{year}",
    f"tsla-opens-up-or-down-on-{month}{day}-{year}",
    f"tsla-up-or-down-on-{month}{day}-{year}",
    f"aapl-up-or-down-on-{month}{day}-{year}",
    f"aapl-opens-up-or-down-on-{month}{day}-{year}",
    f"goog-up-or-down-on-{month}{day}-{year}",
    f"nvda-up-or-down-on-{month}{day}-{year}",
    f"msft-up-or-down-on-{month}{day}-{year}",
    f"amzn-up-or-down-on-{month}{day}-{year}",
    f"meta-up-or-down-on-{month}{day}-{year}",
]

print("=== SLUG FORMAT PROBING ===")
for slug in test_slugs:
    try:
        r = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=10)
        if r.status_code == 200:
            ev = r.json()
            m = (ev.get("markets") or [{}])[0]
            tids = m.get("clobTokenIds", "")
            if isinstance(tids, str):
                try: tids = json.loads(tids)
                except: pass
            print(f"  HIT  {slug}")
            print(f"       title={ev.get('title','')}")
            print(f"       end={ev.get('endDate','')} active={ev.get('active')}")
            print(f"       tokens={tids}")
            outs = m.get("outcomes", "")
            if isinstance(outs, str):
                try: outs = json.loads(outs)
                except: pass
            print(f"       outcomes={outs} prices={m.get('outcomePrices','')}")
        else:
            print(f"  MISS {slug} ({r.status_code})")
    except Exception as e:
        print(f"  ERR  {slug}: {e}")

# Also try broad search
print()
print("=== BROAD SEARCH: 'up or down on' daily finance ===")
try:
    r = requests.get(f"{GAMMA_API}/events", params={
        "active": "true", "limit": 50, "offset": 0
    }, timeout=30)
    if r.status_code == 200:
        events = r.json()
        daily = [e for e in events if "-up-or-down-on-" in e.get("slug", "")]
        crypto = {"bitcoin","ethereum","solana","xrp","dogecoin","hyperliquid","bnb"}
        for e in daily:
            slug = e.get("slug", "")
            pfx = slug.split("-")[0]
            tag = "CRYPTO" if pfx in crypto else "FINANCE"
            print(f"  [{tag}] {slug}: {e.get('title','')}")
    else:
        print(f"  Events list returned {r.status_code}")
except Exception as e:
    print(f"  ERR: {e}")

# Also try listing with higher offset to find more
print()
print("=== SCANNING MORE PAGES ===")
for page_offset in [50, 100, 150, 200, 250]:
    try:
        r = requests.get(f"{GAMMA_API}/events", params={
            "active": "true", "limit": 50, "offset": page_offset
        }, timeout=30)
        if r.status_code == 200:
            events = r.json()
            if not events:
                print(f"  Page offset={page_offset}: empty, stopping")
                break
            daily = [e for e in events if "-up-or-down-on-" in e.get("slug", "")
                     or "-opens-up-or-down-on-" in e.get("slug", "")]
            crypto = {"bitcoin","ethereum","solana","xrp","dogecoin","hyperliquid","bnb"}
            for e in daily:
                slug = e.get("slug", "")
                pfx = slug.split("-")[0]
                if pfx not in crypto:
                    print(f"  [offset={page_offset}] {slug}: {e.get('title','')}")
    except Exception as e:
        print(f"  ERR at offset {page_offset}: {e}")

print()
print("=== DONE ===")
