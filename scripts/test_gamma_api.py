#!/usr/bin/env python3
"""Test the right way to look up Polymarket market outcomes for a condition_id / asset token."""
import json
import csv
import requests

# --- Pick a known condition_id from the trades file ---
# eth-15min-up-or-down-2026-05-16-00:00 Up  (should be closed 10 days ago)
CONDITION_ID = "0x4151692f2007dd9ddb3343236c1c1a405e70feb07e0c2fccaf6acb1157df939c"
ASSET_TOKEN  = "14673682790148906296345933966306922312429468715183055650646387058033966352277"

GAMMA  = "https://gamma-api.polymarket.com"
DATA   = "https://data-api.polymarket.com"

print("=== Gamma API: clob_token_ids (comma-separated) ===")
r = requests.get(f"{GAMMA}/markets", params={"clob_token_ids": ASSET_TOKEN}, timeout=15)
data = r.json() if r.status_code == 200 else []
print(f"  status={r.status_code}  count={len(data)}")
if data:
    m = data[0]
    print(f"  conditionId:   {m.get('conditionId')}")
    print(f"  closed:        {m.get('closed')}")
    print(f"  outcomes:      {m.get('outcomes')}")
    print(f"  outcomePrices: {m.get('outcomePrices')}")
    print(f"  clobTokenIds:  {m.get('clobTokenIds')}")

print("\n=== Data API: /prices?market=<asset_token> ===")
r = requests.get(f"{DATA}/prices", params={"market": ASSET_TOKEN}, timeout=15)
print(f"  status={r.status_code}")
if r.status_code == 200:
    print(f"  response: {json.dumps(r.json(), indent=2)[:500]}")

print("\n=== Data API: /last-trade-price?token=<asset_token> ===")
r = requests.get(f"{DATA}/last-trade-price", params={"token": ASSET_TOKEN}, timeout=15)
print(f"  status={r.status_code}  body={r.text[:300]}")

print("\n=== Gamma API: recent closed crypto up/down market structure ===")
# Search for a recently closed BTC up/down market to see outcomePrices
r = requests.get(f"{GAMMA}/markets",
    params={"closed": "true", "tag_slug": "crypto", "limit": 5, "_order": "startDate:desc"},
    timeout=15)
data = r.json() if r.status_code == 200 else []
print(f"  status={r.status_code}  count={len(data)}")
for m in data:
    slug = m.get("slug", "")
    if "up" in slug or "updown" in slug or "btc" in slug.lower():
        print(f"\n  slug={slug}")
        print(f"  conditionId={m.get('conditionId')}")
        print(f"  closed={m.get('closed')}  outcomePrices={m.get('outcomePrices')}")
        print(f"  outcomes={m.get('outcomes')}")
        print(f"  clobTokenIds={m.get('clobTokenIds','')[:80]}")
        break
else:
    m = data[0] if data else {}
    print(f"  slug={m.get('slug')} closed={m.get('closed')} outcomePrices={m.get('outcomePrices')}")
