#!/usr/bin/env python3
"""Explore Polymarket Gamma API for daily finance markets.

Probes multiple discovery approaches:
1. Direct slug probing for known tickers (SPX, TSLA, AAPL, etc.)
2. Broad event listing with client-side filtering
3. Public search endpoint
"""

import json
import sys
import time
from datetime import datetime, timedelta

import pytz
import requests

GAMMA_API = "https://gamma-api.polymarket.com"
EST = pytz.timezone("US/Eastern")

KNOWN_TICKERS = [
    "spx", "tsla", "aapl", "goog", "amzn", "msft", "nvda", "meta",
    "nflx", "amd", "coin", "spy", "qqq", "iwm", "dia",
    "gold", "silver", "oil", "nasdaq", "dow",
]

SLUG_TEMPLATES = {
    "opens": "{ticker}-opens-up-or-down-on-{month}{day}-{year}",
    "close": "{ticker}-up-or-down-on-{month}{day}-{year}",
}


def format_date(dt):
    return {"month": dt.strftime("%B").lower(), "day": dt.day, "year": dt.year}


def probe_slug(slug):
    try:
        r = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"  ERR {slug}: {e}", file=sys.stderr)
        return None


def list_active(limit=100, offset=0):
    try:
        r = requests.get(
            f"{GAMMA_API}/events",
            params={"active": "true", "limit": limit, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ERR listing: {e}", file=sys.stderr)
        return []


def extract_info(event):
    m = (event.get("markets") or [{}])[0] if event.get("markets") else {}
    tids = m.get("clobTokenIds", [])
    if isinstance(tids, str):
        try:
            tids = json.loads(tids)
        except Exception:
            tids = tids.split("|")
    outs = m.get("outcomes", [])
    if isinstance(outs, str):
        try:
            outs = json.loads(outs)
        except Exception:
            outs = outs.split(",")
    return {
        "slug": event.get("slug", ""),
        "title": event.get("title", ""),
        "end_date": event.get("endDate", ""),
        "active": event.get("active", False),
        "token_ids": tids,
        "outcomes": outs,
        "prices": m.get("outcomePrices", ""),
        "volume": m.get("volume", ""),
        "liquidity": m.get("liquidity", ""),
        "condition_id": m.get("conditionId", ""),
    }


def main():
    now = datetime.now(EST)
    print("=== Polymarket Daily Finance Explorer ===")
    print(f"Time (ET): {now:%Y-%m-%d %H:%M:%S %Z}")
    print()

    # Phase 1: Slug probing
    print("=" * 60)
    print("PHASE 1: Slug probing (known tickers, next 5 days)")
    print("=" * 60)

    dates = [now + timedelta(days=i) for i in range(5)]
    found = []
    for dt in dates:
        parts = format_date(dt)
        print(f"\n--- {dt:%Y-%m-%d (%A)} ---")
        for tk in KNOWN_TICKERS:
            for var, tmpl in SLUG_TEMPLATES.items():
                slug = tmpl.format(ticker=tk, **parts)
                ev = probe_slug(slug)
                if ev:
                    info = extract_info(ev)
                    found.append(info)
                    print(f"  FOUND [{var:5s}] {slug}")
                    print(f"         title={info['title']}")
                    print(f"         end={info['end_date']} vol={info['volume']} liq={info['liquidity']}")
                    print(f"         tokens={info['token_ids']}")
                    print(f"         outcomes={info['outcomes']} prices={info['prices']}")
            time.sleep(0.05)

    print(f"\n=> Slug probing found {len(found)} events")

    # Phase 2: Broad active event scan
    print()
    print("=" * 60)
    print("PHASE 2: Broad active event scan")
    print("=" * 60)

    finance = []
    crypto_prefixes = {
        "bitcoin", "ethereum", "solana", "xrp", "dogecoin",
        "hyperliquid", "bnb", "btc", "eth", "sol", "doge", "hype",
    }
    offset = 0
    total = 0
    while True:
        evts = list_active(100, offset)
        if not evts:
            break
        total += len(evts)
        for e in evts:
            s = e.get("slug", "")
            if "-up-or-down-on-" in s or "-opens-up-or-down-on-" in s:
                pfx = s.split("-")[0]
                if pfx not in crypto_prefixes:
                    finance.append(extract_info(e))
        offset += 100
        if len(evts) < 100:
            break
        time.sleep(0.1)

    print(f"Scanned {total} active events")
    print(f"Found {len(finance)} daily finance events (non-crypto):")
    for i in finance:
        print(f"  {i['slug']}")
        print(f"    title={i['title']} end={i['end_date']} vol={i['volume']}")
        print(f"    outcomes={i['outcomes']} prices={i['prices']}")

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    all_slugs = set(i["slug"] for i in found + finance if i["slug"])
    all_tickers = set(s.split("-")[0] for s in all_slugs)
    print(f"Unique slugs: {len(all_slugs)}")
    print(f"Tickers: {sorted(all_tickers)}")
    print()
    print("All discovered slugs:")
    for s in sorted(all_slugs):
        matching = [i for i in found + finance if i["slug"] == s]
        info = matching[0] if matching else {}
        print(f"  {s}")
        print(f"    end={info.get('end_date','')} tokens={info.get('token_ids','')}")


if __name__ == "__main__":
    main()
