#!/usr/bin/env python3
"""Quick test to find correct Gamma API parameter for condition_id lookup."""
import json
import requests

# A condition_id and asset from the ivy56 trades (eth-15min-up-or-down-2026-05-16-00:00 Up)
CONDITION_ID = "0x4151692f2007dd9ddb3343236c1c1a405e70feb07e0c2fccaf6acb1157df939c"
ASSET_TOKEN  = "14673682790148906296345933966306922312429468715183055650646387058033966352277"

BASE = "https://gamma-api.polymarket.com"

tests = [
    ("conditionId",    {"conditionId":    CONDITION_ID}),
    ("conditionIds",   {"conditionIds":   CONDITION_ID}),
    ("clob_token_ids", {"clob_token_ids": ASSET_TOKEN}),
    ("id",             {"id":             CONDITION_ID}),
]

for name, params in tests:
    r = requests.get(f"{BASE}/markets", params=params, timeout=15)
    data = r.json() if r.status_code == 200 else []
    hit = data[0] if data else {}
    returned_cid = hit.get("conditionId", "")
    match = returned_cid == CONDITION_ID
    print(f"{name:<20} status={r.status_code} count={len(data):>4} match={match} closed={hit.get('closed')} returned={returned_cid[:30]}")

# Also look at what fields a resolved market has
print("\n--- Full first resolved market ---")
r = requests.get(f"{BASE}/markets", params={"closed": "true", "limit": 1}, timeout=15)
if r.status_code == 200 and r.json():
    m = r.json()[0]
    print(json.dumps({k: m[k] for k in sorted(m) if m[k] is not None}, indent=2, default=str))
