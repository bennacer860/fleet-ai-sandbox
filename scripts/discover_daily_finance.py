#!/usr/bin/env python3
"""Discover daily finance markets on Polymarket.

Finds stock/index up-or-down markets for upcoming trading days.
Supports two market types:
  - "opens": resolves at market open (SPX only)
  - "close": resolves at market close (all tickers)

Usage:
  python scripts/discover_daily_finance.py [--days N] [--validate] [--json]

Options:
  --days N      Scan next N calendar days (default: 7)
  --validate    Also check CLOB order books for liquidity (requires P1 creds)
  --json        Output as JSON instead of human-readable
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any

import pytz
import requests

GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com").rstrip("/")
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
EST = pytz.timezone("US/Eastern")

# Tickers confirmed to have daily markets on Polymarket (as of April 2026)
KNOWN_TICKERS = [
    "spx", "spy", "qqq",
    "tsla", "aapl", "googl", "amzn", "msft", "nvda", "meta", "nflx", "coin",
]

# Only SPX has an "opens" variant; all tickers have the "close" variant
SLUG_TEMPLATES = {
    "opens": "{ticker}-opens-up-or-down-on-{month}-{day}-{year}",
    "close": "{ticker}-up-or-down-on-{month}-{day}-{year}",
}

OPENS_TICKERS = {"spx"}

LAZY_SUB_LEAD_MINUTES = 60


def _date_parts(dt: datetime) -> dict[str, Any]:
    return {"month": dt.strftime("%B").lower(), "day": dt.day, "year": dt.year}


def generate_slugs(ticker: str, dt: datetime) -> dict[str, str]:
    """Generate slug variants for a ticker on a given date."""
    parts = _date_parts(dt)
    slugs = {"close": SLUG_TEMPLATES["close"].format(ticker=ticker, **parts)}
    if ticker in OPENS_TICKERS:
        slugs["opens"] = SLUG_TEMPLATES["opens"].format(ticker=ticker, **parts)
    return slugs


def fetch_event(slug: str) -> dict | None:
    """Fetch event from Gamma API by slug."""
    try:
        r = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def parse_event(event: dict, ticker: str, variant: str) -> dict:
    """Extract structured info from a Gamma event response."""
    market = (event.get("markets") or [{}])[0]

    token_ids = market.get("clobTokenIds", [])
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except json.JSONDecodeError:
            token_ids = token_ids.split("|")

    outcomes = market.get("outcomes", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = outcomes.split(",")

    prices = market.get("outcomePrices", "")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            prices = [p.strip() for p in prices.split(",")]

    end_date_str = event.get("endDate", "")
    end_ts = None
    if end_date_str:
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            end_ts = int(end_dt.timestamp())
        except Exception:
            pass

    return {
        "slug": event.get("slug", ""),
        "ticker": ticker.upper(),
        "variant": variant,
        "title": event.get("title", ""),
        "end_date": end_date_str,
        "end_ts": end_ts,
        "active": event.get("active", False),
        "closed": event.get("closed", False),
        "token_ids": token_ids,
        "outcomes": outcomes,
        "prices": prices,
        "condition_id": market.get("conditionId", ""),
        "volume": market.get("volume", ""),
        "liquidity": market.get("liquidity", ""),
        "question": market.get("question", ""),
    }


def broad_scan_finance_events() -> list[dict]:
    """Scan all active events for daily finance patterns we might have missed."""
    crypto_prefixes = {
        "bitcoin", "ethereum", "solana", "xrp", "dogecoin",
        "hyperliquid", "bnb", "btc", "eth", "sol", "doge", "hype",
    }
    found = []
    offset = 0
    while True:
        try:
            r = requests.get(
                f"{GAMMA_API}/events",
                params={"active": "true", "limit": 100, "offset": offset},
                timeout=30,
            )
            r.raise_for_status()
            events = r.json()
        except Exception:
            break
        if not events:
            break
        for e in events:
            slug = e.get("slug", "")
            if "-up-or-down-on-" in slug:
                pfx = slug.split("-")[0]
                if pfx not in crypto_prefixes:
                    variant = "opens" if "-opens-up-or-down-on-" in slug else "close"
                    found.append(parse_event(e, pfx, variant))
        offset += 100
        if len(events) < 100:
            break
        time.sleep(0.1)
    return found


def compute_lazy_sub_schedule(market: dict, lead_minutes: int = LAZY_SUB_LEAD_MINUTES) -> dict | None:
    """Compute when to subscribe for lazy-sub strategy."""
    end_ts = market.get("end_ts")
    if not end_ts:
        return None

    now_ts = int(time.time())
    sub_ts = end_ts - (lead_minutes * 60)

    end_dt = datetime.fromtimestamp(end_ts, tz=pytz.utc).astimezone(EST)
    sub_dt = datetime.fromtimestamp(sub_ts, tz=pytz.utc).astimezone(EST)

    return {
        "subscribe_at": sub_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "subscribe_ts": sub_ts,
        "market_end": end_dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "market_end_ts": end_ts,
        "seconds_until_sub": max(0, sub_ts - now_ts),
        "already_in_window": now_ts >= sub_ts,
        "already_expired": now_ts >= end_ts,
    }


def check_clob_book(token_id: str) -> dict | None:
    """Fetch order book summary from CLOB for a token."""
    try:
        r = requests.get(
            f"{CLOB_HOST}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        if r.status_code == 200:
            book = r.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0
            best_ask = float(asks[0]["price"]) if asks else 0
            bid_depth = sum(float(b.get("size", 0)) for b in bids)
            ask_depth = sum(float(a.get("size", 0)) for a in asks)
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_depth": round(bid_depth, 2),
                "ask_depth": round(ask_depth, 2),
                "bid_levels": len(bids),
                "ask_levels": len(asks),
            }
        return None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Discover daily finance markets")
    parser.add_argument("--days", type=int, default=7, help="Calendar days to scan")
    parser.add_argument("--validate", action="store_true", help="Check CLOB order books")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--lead-minutes", type=int, default=LAZY_SUB_LEAD_MINUTES,
                        help="Minutes before expiry for lazy-sub window")
    args = parser.parse_args()

    now = datetime.now(EST)
    now_ts = int(time.time())

    if not args.json:
        print("=" * 70)
        print("  Polymarket Daily Finance Market Discovery")
        print(f"  Time: {now:%Y-%m-%d %H:%M:%S %Z}")
        print("=" * 70)

    # Phase 1: Deterministic slug probing
    dates = [now + timedelta(days=i) for i in range(args.days)]
    all_markets: list[dict] = []

    if not args.json:
        print(f"\n[1/3] Probing {len(KNOWN_TICKERS)} tickers x {args.days} days...")

    for dt in dates:
        if dt.weekday() >= 5:
            continue
        for ticker in KNOWN_TICKERS:
            slugs = generate_slugs(ticker, dt)
            for variant, slug in slugs.items():
                event = fetch_event(slug)
                if event:
                    market = parse_event(event, ticker, variant)
                    all_markets.append(market)
                    if not args.json:
                        status = "EXPIRED" if market["closed"] else "ACTIVE"
                        print(f"  [{status:7s}] {slug} | vol=${market['volume']}")
            time.sleep(0.02)

    # Phase 2: Broad scan for undiscovered tickers
    if not args.json:
        print(f"\n[2/3] Broad scan for undiscovered finance events...")

    broad = broad_scan_finance_events()
    known_slugs = {m["slug"] for m in all_markets}
    new_from_broad = [m for m in broad if m["slug"] not in known_slugs]
    all_markets.extend(new_from_broad)

    if new_from_broad and not args.json:
        print(f"  Found {len(new_from_broad)} additional markets from broad scan:")
        for m in new_from_broad:
            print(f"    {m['slug']} ({m['ticker']})")

    # Phase 3: Lazy-sub schedule
    if not args.json:
        print(f"\n[3/3] Computing lazy-sub schedule (lead={args.lead_minutes}min)...")

    for market in all_markets:
        schedule = compute_lazy_sub_schedule(market, args.lead_minutes)
        if schedule:
            market["lazy_sub"] = schedule

    # CLOB validation
    if args.validate:
        if not args.json:
            print(f"\n[VALIDATE] Checking CLOB order books...")
        for market in all_markets:
            if market.get("closed") or not market.get("token_ids"):
                continue
            books = {}
            for i, tid in enumerate(market["token_ids"]):
                outcome = market["outcomes"][i] if i < len(market["outcomes"]) else "?"
                book = check_clob_book(tid)
                if book:
                    books[outcome] = book
                    if not args.json:
                        print(f"  {market['slug']} [{outcome}]: "
                              f"bid={book['best_bid']:.3f} ask={book['best_ask']:.3f} "
                              f"depth={book['bid_depth']}/{book['ask_depth']}")
                time.sleep(0.05)
            market["books"] = books

    # Output
    if args.json:
        print(json.dumps(all_markets, indent=2, default=str))
        return

    # Human-readable summary
    print()
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)

    tickers = sorted(set(m["ticker"] for m in all_markets))
    print(f"\nTickers found: {', '.join(tickers)}")
    print(f"Total markets: {len(all_markets)}")

    active = [m for m in all_markets if not m.get("closed") and not m.get("lazy_sub", {}).get("already_expired")]
    expired = [m for m in all_markets if m.get("closed") or m.get("lazy_sub", {}).get("already_expired")]

    print(f"Active: {len(active)}  |  Expired/Closed: {len(expired)}")

    # Group by date
    by_date: dict[str, list] = {}
    for m in active:
        date_key = m.get("end_date", "")[:10]
        by_date.setdefault(date_key, []).append(m)

    for date_key in sorted(by_date.keys()):
        markets = by_date[date_key]
        print(f"\n  --- {date_key} ({len(markets)} markets) ---")
        for m in sorted(markets, key=lambda x: (x["ticker"], x["variant"])):
            sched = m.get("lazy_sub", {})
            if sched.get("already_in_window"):
                sub_info = "IN WINDOW NOW"
            elif sched.get("seconds_until_sub", 0) > 0:
                mins = sched["seconds_until_sub"] // 60
                hrs = mins // 60
                if hrs > 0:
                    sub_info = f"sub in {hrs}h{mins % 60}m"
                else:
                    sub_info = f"sub in {mins}m"
            else:
                sub_info = "?"

            prices_str = ""
            if m.get("prices") and len(m["prices"]) == 2:
                try:
                    up_p = float(m["prices"][0])
                    dn_p = float(m["prices"][1])
                    prices_str = f" Up={up_p:.1%} Dn={dn_p:.1%}"
                except (ValueError, TypeError):
                    pass

            vol_str = ""
            if m.get("volume"):
                try:
                    vol = float(m["volume"])
                    vol_str = f" vol=${vol:,.0f}"
                except (ValueError, TypeError):
                    pass

            print(f"    {m['ticker']:5s} [{m['variant']:5s}] {m['slug']}")
            print(f"          {sub_info}{prices_str}{vol_str}")

    # Lazy-sub schedule sorted by subscribe time
    upcoming = [m for m in active if m.get("lazy_sub") and not m["lazy_sub"].get("already_expired")]
    if upcoming:
        print(f"\n  --- LAZY-SUB SCHEDULE (sorted by subscribe time) ---")
        upcoming.sort(key=lambda m: m["lazy_sub"].get("subscribe_ts", 0))
        for m in upcoming:
            sched = m["lazy_sub"]
            print(f"    {sched['subscribe_at']} -> {m['slug']}")
            if sched["already_in_window"]:
                print(f"      ** SUBSCRIBE NOW (market ends {sched['market_end']})")
            else:
                mins = sched["seconds_until_sub"] // 60
                print(f"      subscribe in {mins}min (market ends {sched['market_end']})")


if __name__ == "__main__":
    main()
