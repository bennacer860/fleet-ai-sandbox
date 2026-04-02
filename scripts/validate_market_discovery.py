#!/usr/bin/env python3
"""Validate category discovery and end-time extraction for production smoke tests."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.gamma_client import discover_markets_by_category
from src.markets.fifteen_min import extract_market_end_ts
from src.markets.discovery import discover_slugs
from src.gamma_client import fetch_event_by_slug
from src.utils.market_data import get_market_evaluation


def _parse_iso_end_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _extract_slug(market: dict[str, Any]) -> str:
    for key in ("slug", "market_slug", "marketSlug", "event_slug", "eventSlug"):
        val = market.get(key)
        if isinstance(val, str) and val:
            return val
    event = market.get("event")
    if isinstance(event, dict):
        for key in ("slug", "event_slug", "eventSlug"):
            val = event.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def _extract_raw_end_date(market: dict[str, Any]) -> str | None:
    raw = market.get("endDate")
    if isinstance(raw, str) and raw:
        return raw
    event = market.get("event")
    if isinstance(event, dict):
        raw = event.get("endDate")
        if isinstance(raw, str) and raw:
            return raw
    return None


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate market discovery and end-time fields for a category.",
    )
    parser.add_argument(
        "--category",
        default="weather/temperature",
        help="Category path to discover (default: weather/temperature)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="Max Gamma pages to scan (default: 5)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Gamma page size (default: 100)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Max markets to print (default: 30)",
    )
    parser.add_argument(
        "--only-active",
        action="store_true",
        default=True,
        help="Discover active/open markets only (default: true)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON instead of table output",
    )
    parser.add_argument(
        "--mode",
        choices=["markets", "slugs"],
        default="slugs",
        help="Discovery source mode (default: slugs)",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include inactive/upcoming markets/events in discovery scans",
    )
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    if args.mode == "markets":
        markets = discover_markets_by_category(
            args.category,
            only_active=(False if args.include_inactive else args.only_active),
            max_pages=args.max_pages,
            page_size=args.page_size,
        )
        candidates = []
        seen: set[str] = set()
        for market in markets:
            slug = _extract_slug(market)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            candidates.append((slug, _extract_raw_end_date(market)))
    else:
        slugs = discover_slugs(
            args.category,
            durations=None,
            only_active=(False if args.include_inactive else args.only_active),
            lead_time_seconds=None,
            max_pages=args.max_pages,
            page_size=args.page_size,
        )
        candidates = []
        for slug in slugs:
            event = fetch_event_by_slug(slug)
            raw_end_date = event.get("endDate") if isinstance(event, dict) else None
            candidates.append((slug, raw_end_date))

    for slug, raw_end_date in candidates:
        gamma_end_ts = _parse_iso_end_ts(raw_end_date)
        slug_end_ts = extract_market_end_ts(slug)
        eval_data = get_market_evaluation(slug) or {}
        eval_end_ts = eval_data.get("end_ts")

        row = {
            "slug": slug,
            "raw_end_date": raw_end_date or "-",
            "gamma_end_ts": gamma_end_ts,
            "gamma_end_utc": _fmt_ts(gamma_end_ts),
            "slug_parsed_end_ts": slug_end_ts,
            "slug_parsed_end_utc": _fmt_ts(slug_end_ts),
            "eval_end_ts": eval_end_ts,
            "eval_end_utc": _fmt_ts(eval_end_ts),
            "slug_vs_gamma_delta_s": (slug_end_ts - gamma_end_ts)
            if slug_end_ts is not None and gamma_end_ts is not None
            else None,
            "eval_vs_gamma_delta_s": (eval_end_ts - gamma_end_ts)
            if isinstance(eval_end_ts, (int, float)) and gamma_end_ts is not None
            else None,
        }
        rows.append(row)
        if len(rows) >= args.limit:
            break

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(
        f"category={args.category} mode={args.mode} discovered={len(rows)} "
        f"(showing up to {args.limit})"
    )
    print(
        "slug | gamma_end_utc | slug_parsed_end_utc | eval_end_utc | "
        "slug_vs_gamma_delta_s | eval_vs_gamma_delta_s"
    )
    print("-" * 160)
    for row in rows:
        print(
            f"{row['slug']} | {row['gamma_end_utc']} | {row['slug_parsed_end_utc']} | "
            f"{row['eval_end_utc']} | {row['slug_vs_gamma_delta_s']} | {row['eval_vs_gamma_delta_s']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
