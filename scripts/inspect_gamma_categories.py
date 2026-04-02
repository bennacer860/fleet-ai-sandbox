#!/usr/bin/env python3
"""Inspect Gamma category/tag fields for market discovery debugging."""

from __future__ import annotations

import argparse
import json
import re

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Gamma category taxonomy.")
    parser.add_argument("--needle", default=None, help="Single substring to search (legacy flag)")
    parser.add_argument(
        "--needles",
        nargs="+",
        default=None,
        help="Multiple keywords to inspect (example: weather temperature)",
    )
    parser.add_argument(
        "--word-boundary",
        action="store_true",
        help="Use whole-word matching instead of substring matching",
    )
    parser.add_argument("--limit", type=int, default=400, help="Markets page limit (default: 400)")
    parser.add_argument("--max-print", type=int, default=40, help="Max matches to print (default: 40)")
    args = parser.parse_args()

    rows = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"limit": args.limit, "offset": 0, "active": "true", "closed": "false"},
        timeout=30,
    ).json()
    needles = [n.lower() for n in (args.needles or ([] if args.needle is None else [args.needle]))]
    if not needles:
        needles = ["weather"]

    def _match(blob: str, needle: str) -> bool:
        if args.word_boundary:
            return re.search(rf"\b{re.escape(needle)}\b", blob) is not None
        return needle in blob

    hits_by_needle: dict[str, list[dict]] = {n: [] for n in needles}
    intersection: list[dict] = []
    for market in rows:
        blob = json.dumps(market, default=str).lower()
        matched_needles = [n for n in needles if _match(blob, n)]
        for n in matched_needles:
            hits_by_needle[n].append(market)
        if len(matched_needles) == len(needles):
            intersection.append(market)

    print(
        f"needles={needles} scanned={len(rows)} "
        + " ".join([f"{n}={len(hits_by_needle[n])}" for n in needles])
        + f" intersection={len(intersection)}"
    )

    for market in intersection[: args.max_print]:
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
