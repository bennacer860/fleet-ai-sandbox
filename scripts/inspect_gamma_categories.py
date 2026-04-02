#!/usr/bin/env python3
"""Inspect Gamma category/tag fields for market discovery debugging."""

from __future__ import annotations

import argparse
import json

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Gamma category taxonomy.")
    parser.add_argument("--needle", default="weather", help="Substring to search (default: weather)")
    parser.add_argument("--limit", type=int, default=400, help="Markets page limit (default: 400)")
    parser.add_argument("--max-print", type=int, default=40, help="Max matches to print (default: 40)")
    args = parser.parse_args()

    rows = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"limit": args.limit, "offset": 0, "active": "true", "closed": "false"},
        timeout=30,
    ).json()
    needle = args.needle.lower()

    matches = []
    for market in rows:
        blob = json.dumps(market, default=str).lower()
        if needle in blob:
            matches.append(market)

    print(f"needle={args.needle} matches={len(matches)} scanned={len(rows)}")
    for market in matches[: args.max_print]:
        event = market.get("event") if isinstance(market.get("event"), dict) else {}
        payload = {
            "slug": market.get("slug") or market.get("marketSlug") or market.get("event_slug"),
            "question": market.get("question"),
            "category": market.get("category"),
            "categorySlug": market.get("categorySlug") or market.get("category_slug"),
            "tag": market.get("tag"),
            "tagSlug": market.get("tagSlug") or market.get("tag_slug"),
            "tags": market.get("tags"),
            "event.slug": event.get("slug"),
            "event.category": event.get("category"),
            "event.categorySlug": event.get("categorySlug") or event.get("category_slug"),
            "event.tags": event.get("tags"),
        }
        print(json.dumps(payload, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
