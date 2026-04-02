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
    parser.add_argument("--page-size", type=int, default=500, help="Markets page size (default: 500)")
    parser.add_argument("--max-pages", type=int, default=10, help="Max pages to scan (default: 10)")
    parser.add_argument("--max-print", type=int, default=40, help="Max matches to print (default: 40)")
    parser.add_argument("--slug-check", default=None, help="Exact slug to check across scanned pages")
    args = parser.parse_args()

    rows = []
    for page in range(args.max_pages):
        page_rows = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "limit": args.page_size,
                "offset": page * args.page_size,
                "active": "true",
                "closed": "false",
            },
            timeout=30,
        ).json()
        if not isinstance(page_rows, list) or not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < args.page_size:
            break
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

    if args.slug_check:
        target = args.slug_check.strip().lower()
        exact_hits = []
        for market in rows:
            if not isinstance(market, dict):
                continue
            slug = (market.get("slug") or market.get("marketSlug") or market.get("event_slug") or "").lower()
            ev = market.get("event") if isinstance(market.get("event"), dict) else {}
            event_slug = (ev.get("slug") or "").lower()
            if target in {slug, event_slug}:
                exact_hits.append(market)
        print(f"slug_check={args.slug_check} hits={len(exact_hits)}")
        for market in exact_hits[: args.max_print]:
            event = market.get("event") if isinstance(market.get("event"), dict) else {}
            payload = {
                "slug": market.get("slug") or market.get("marketSlug") or market.get("event_slug"),
                "question": market.get("question"),
                "category": market.get("category"),
                "categorySlug": market.get("categorySlug") or market.get("category_slug"),
                "tag": market.get("tag"),
                "tagSlug": market.get("tagSlug") or market.get("tag_slug"),
                "active": market.get("active"),
                "closed": market.get("closed"),
                "endDate": market.get("endDate"),
                "event.slug": event.get("slug"),
                "event.category": event.get("category"),
                "event.categorySlug": event.get("categorySlug") or event.get("category_slug"),
                "event.endDate": event.get("endDate"),
            }
            print(json.dumps(payload, default=str))

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
