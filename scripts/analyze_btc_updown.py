#!/usr/bin/env python3
"""Analyze BTC hourly up/down distribution over 1 year.

Fetches BTC OHLC data from Binance public API and counts how many hours
the price closed higher vs lower than it opened.

Usage:
    python3 scripts/analyze_btc_updown.py

This validates whether BTC hourly markets are truly 50/50 coin flips.
"""

import json
import math
import ssl
import urllib.request
from datetime import datetime, timedelta
from collections import defaultdict

# Binance public API - no auth required
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def fetch_binance_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Fetch klines (OHLC) data from Binance."""
    url = f"{BINANCE_KLINES_URL}?symbol={symbol}&interval={interval}&startTime={start_ms}&endTime={end_ms}&limit=1000"
    
    # Skip SSL verification for corporate proxies
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as response:
        return json.loads(response.read().decode())


def analyze_updown(klines: list) -> dict:
    """Analyze up/down distribution from klines data."""
    up = 0
    down = 0
    flat = 0
    
    for k in klines:
        open_price = float(k[1])
        close_price = float(k[4])
        
        if close_price > open_price:
            up += 1
        elif close_price < open_price:
            down += 1
        else:
            flat += 1
    
    return {"up": up, "down": down, "flat": flat}


def main():
    print("BTC HOURLY UP/DOWN ANALYSIS")
    print("=" * 60)
    print()
    
    # Fetch 1 year of hourly data in chunks (Binance limit is 1000 per request)
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=365)
    
    print(f"Period: {start_time.date()} to {end_time.date()} (365 days)")
    print(f"Fetching data from Binance...")
    print()
    
    all_klines = []
    current_start = start_time
    
    while current_start < end_time:
        start_ms = int(current_start.timestamp() * 1000)
        end_ms = int(min(current_start + timedelta(days=41), end_time).timestamp() * 1000)
        
        try:
            klines = fetch_binance_klines("BTCUSDT", "1h", start_ms, end_ms)
            all_klines.extend(klines)
            print(f"  Fetched {len(klines)} hours from {current_start.date()}")
        except Exception as e:
            print(f"  Error fetching from {current_start.date()}: {e}")
        
        current_start += timedelta(days=41)
    
    print()
    print(f"Total hours fetched: {len(all_klines):,}")
    print()
    
    # Analyze full dataset
    stats = analyze_updown(all_klines)
    total = stats["up"] + stats["down"] + stats["flat"]
    
    if total == 0:
        print("ERROR: No data fetched. Binance API may be blocked.")
        print("Try using CryptoCompare instead (see inline comments).")
        return
    
    print("=" * 60)
    print("RESULTS: BTC HOURLY UP/DOWN (1 YEAR)")
    print("=" * 60)
    print(f"Up hours:   {stats['up']:,} ({stats['up']/total*100:.1f}%)")
    print(f"Down hours: {stats['down']:,} ({stats['down']/total*100:.1f}%)")
    print(f"Flat:       {stats['flat']:,} ({stats['flat']/total*100:.1f}%)")
    print()
    
    # Statistical significance test
    n = stats["up"] + stats["down"]
    p = stats["up"] / n if n > 0 else 0.5
    se = math.sqrt(0.5 * 0.5 / n)
    z = (p - 0.5) / se
    
    print(f"Z-score: {z:.2f} (deviation from 50/50)")
    if abs(z) > 2.58:
        print(f"→ HIGHLY SIGNIFICANT (p < 0.01): Markets are NOT 50/50")
    elif abs(z) > 1.96:
        print(f"→ SIGNIFICANT (p < 0.05): Markets may not be 50/50")
    else:
        print(f"→ NOT SIGNIFICANT: Consistent with 50/50 coin flip")
    
    print()
    print("=" * 60)
    print("BREAKDOWN BY TIME OF DAY (UTC)")
    print("=" * 60)
    
    # Analyze by hour of day
    by_hour = defaultdict(lambda: {"up": 0, "down": 0})
    for k in all_klines:
        ts = datetime.utcfromtimestamp(k[0] / 1000)
        hour = ts.hour
        open_price = float(k[1])
        close_price = float(k[4])
        
        if close_price > open_price:
            by_hour[hour]["up"] += 1
        elif close_price < open_price:
            by_hour[hour]["down"] += 1
    
    print(f"{'Hour':<6} {'Up':>6} {'Down':>6} {'Up %':>8}")
    print("-" * 28)
    for hour in range(24):
        h = by_hour[hour]
        total_h = h["up"] + h["down"]
        up_pct = h["up"] / total_h * 100 if total_h > 0 else 0
        bar = "█" * int(up_pct / 2) + "░" * (50 - int(up_pct / 2))
        print(f"{hour:02d}:00  {h['up']:>6} {h['down']:>6} {up_pct:>7.1f}%")
    
    print()
    print("=" * 60)
    print("BREAKDOWN BY DAY OF WEEK")
    print("=" * 60)
    
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_day = defaultdict(lambda: {"up": 0, "down": 0})
    for k in all_klines:
        ts = datetime.utcfromtimestamp(k[0] / 1000)
        day = ts.weekday()
        open_price = float(k[1])
        close_price = float(k[4])
        
        if close_price > open_price:
            by_day[day]["up"] += 1
        elif close_price < open_price:
            by_day[day]["down"] += 1
    
    print(f"{'Day':<6} {'Up':>6} {'Down':>6} {'Up %':>8}")
    print("-" * 28)
    for day in range(7):
        d = by_day[day]
        total_d = d["up"] + d["down"]
        up_pct = d["up"] / total_d * 100 if total_d > 0 else 0
        print(f"{days[day]:<6} {d['up']:>6} {d['down']:>6} {up_pct:>7.1f}%")


if __name__ == "__main__":
    main()
