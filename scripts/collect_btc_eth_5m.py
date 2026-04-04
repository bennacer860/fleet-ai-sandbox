#!/usr/bin/env python3
"""Collect BTC/ETH 5m top-of-book samples and upload artifact to S3.

This script intentionally stays simple:
- sample once per second from live WS-cached best prices
- track rolling current/next 5-minute windows
- stop after N completed windows
- persist JSONL.gz artifact for later test replay
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as a standalone script: `python3 scripts/collect_btc_eth_5m.py`.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.event_bus import EventBus
from src.gateway.market_ws import MarketWebSocket
from src.logging_config import get_logger
from src.markets.fifteen_min import (
    MarketSelection,
    get_current_interval_utc,
    get_market_slug,
    get_next_interval_utc,
)

logger = get_logger(__name__)

_DURATION_MINUTES = 5
_DURATION_SECONDS = _DURATION_MINUTES * 60
_DEFAULT_MARKETS: tuple[MarketSelection, ...] = ("BTC", "ETH")
_DEFAULT_S3_PREFIX = "collectors/btc_eth_5m"


@dataclass(frozen=True)
class WindowPlan:
    timestamp: int
    slugs: dict[str, str]


@dataclass(frozen=True)
class RunConfig:
    n_events: int
    interval_seconds: float
    output_dir: Path
    upload: bool
    s3_bucket: str
    s3_prefix: str
    s3_region: str
    dry_run: bool
    startup_wait_seconds: float


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _plan_windows(n_events: int) -> list[WindowPlan]:
    first = get_current_interval_utc(_DURATION_MINUTES)
    # Explicitly call existing helper for parity with runtime code paths.
    _ = get_next_interval_utc(_DURATION_MINUTES)
    plans: list[WindowPlan] = []
    for i in range(n_events):
        ts = first + i * _DURATION_SECONDS
        plans.append(
            WindowPlan(
                timestamp=ts,
                slugs={
                    market: get_market_slug(market, _DURATION_MINUTES, ts)
                    for market in _DEFAULT_MARKETS
                },
            )
        )
    return plans


def _resolve_bucket(explicit: str) -> str:
    if explicit:
        return explicit
    for key in ("COLLECTOR_S3_BUCKET", "LOG_SYNC_S3_BUCKET", "S3_BUCKET"):
        val = os.getenv(key, "").strip()
        if val:
            return val
    return ""


def _resolve_region(explicit: str) -> str:
    if explicit:
        return explicit
    for key in ("COLLECTOR_S3_REGION", "LOG_SYNC_REGION", "AWS_REGION"):
        val = os.getenv(key, "").strip()
        if val:
            return val
    return "eu-west-1"


def _build_output_path(output_dir: Path, n_events: int) -> Path:
    start = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    return output_dir / f"btc_eth_5m_{start}_N{n_events}.jsonl.gz"


def _build_s3_key(prefix: str, file_name: str, when_utc: datetime | None = None) -> str:
    now = when_utc or _utc_now()
    date_prefix = now.strftime("%Y/%m/%d")
    base = prefix.strip("/")
    return f"{base}/{date_prefix}/{file_name}"


def _require_aws_cli() -> None:
    if shutil.which("aws"):
        return
    raise RuntimeError("AWS CLI not found in PATH; required for S3 upload")


def _upload_with_retries(local_path: Path, bucket: str, key: str, region: str, retries: int = 3) -> str:
    cmd = [
        "aws",
        "s3",
        "cp",
        str(local_path),
        f"s3://{bucket}/{key}",
        "--region",
        region,
        "--only-show-errors",
    ]
    delay_s = 2.0
    for attempt in range(1, retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return f"s3://{bucket}/{key}"
        if attempt == retries:
            stderr = result.stderr.strip() or "unknown upload error"
            raise RuntimeError(f"S3 upload failed after {retries} attempts: {stderr}")
        logger.warning("S3 upload attempt %d/%d failed; retrying in %.1fs", attempt, retries, delay_s)
        time.sleep(delay_s)
        delay_s *= 2.0
    raise RuntimeError("unreachable")


def _build_run_config(args: argparse.Namespace) -> RunConfig:
    bucket = _resolve_bucket(args.s3_bucket)
    region = _resolve_region(args.s3_region)
    if args.upload and not bucket:
        raise ValueError(
            "S3 upload enabled but no bucket configured. Set --s3-bucket or env "
            "COLLECTOR_S3_BUCKET/LOG_SYNC_S3_BUCKET/S3_BUCKET."
        )
    if args.upload:
        _require_aws_cli()
    return RunConfig(
        n_events=args.n_events,
        interval_seconds=args.interval_seconds,
        output_dir=Path(args.output_dir),
        upload=args.upload,
        s3_bucket=bucket,
        s3_prefix=args.s3_prefix,
        s3_region=region,
        dry_run=args.dry_run,
        startup_wait_seconds=args.startup_wait_seconds,
    )


def _window_meta(plans: list[WindowPlan]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in plans:
        rows.append(
            {
                "timestamp": p.timestamp,
                "start_utc": _format_utc(p.timestamp),
                "end_utc": _format_utc(p.timestamp + _DURATION_SECONDS),
                "slugs": p.slugs,
            }
        )
    return rows


async def _collect(cfg: RunConfig) -> dict[str, Any]:
    plans = _plan_windows(cfg.n_events)
    if cfg.dry_run:
        return {
            "dry_run": True,
            "duration_minutes": _DURATION_MINUTES,
            "interval_seconds": cfg.interval_seconds,
            "windows": _window_meta(plans),
        }

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = _build_output_path(cfg.output_dir, cfg.n_events)

    active_indexes: list[int] = list(range(min(2, len(plans))))
    next_index = len(active_indexes)
    completed_indexes: list[int] = []
    rows_written = 0
    sample_seq = 0

    initial_slugs: list[str] = []
    for i in active_indexes:
        initial_slugs.extend(plans[i].slugs.values())

    event_bus = EventBus()
    market_ws = MarketWebSocket(event_bus=event_bus, initial_slugs=initial_slugs, book_event_filter=None)
    ws_task = asyncio.create_task(market_ws.run())

    start_wall = time.time()
    end_wall = start_wall
    s3_uri = ""

    try:
        await asyncio.sleep(max(0.0, cfg.startup_wait_seconds))
        with gzip.open(output_path, mode="wt", encoding="utf-8") as fh:
            meta = {
                "type": "meta",
                "run_started_utc": _format_utc(start_wall),
                "duration_minutes": _DURATION_MINUTES,
                "interval_seconds": cfg.interval_seconds,
                "markets": list(_DEFAULT_MARKETS),
                "n_events": cfg.n_events,
                "windows": _window_meta(plans),
            }
            fh.write(json.dumps(meta) + "\n")
            rows_written += 1

            while len(completed_indexes) < len(plans):
                now = time.time()
                now_int = int(now)

                # Rotate completed windows and add next planned windows.
                rotate_out: list[int] = []
                for idx in list(active_indexes):
                    if now_int >= plans[idx].timestamp + _DURATION_SECONDS:
                        rotate_out.append(idx)
                for idx in rotate_out:
                    if idx in active_indexes:
                        active_indexes.remove(idx)
                    if idx not in completed_indexes:
                        completed_indexes.append(idx)
                    await market_ws.remove_markets(list(plans[idx].slugs.values()))
                    if next_index < len(plans):
                        active_indexes.append(next_index)
                        await market_ws.add_markets(list(plans[next_index].slugs.values()))
                        next_index += 1

                sample_utc = _format_utc(now)
                for idx in sorted(active_indexes):
                    plan = plans[idx]
                    for market, slug in plan.slugs.items():
                        token_ids = tuple(market_ws.token_ids.get(slug, ()))
                        if not token_ids:
                            row = {
                                "type": "sample",
                                "sample_seq": sample_seq,
                                "ts_utc": sample_utc,
                                "window_timestamp": plan.timestamp,
                                "slug": slug,
                                "market": market,
                                "token_id": None,
                                "outcome": None,
                                "best_bid": None,
                                "best_ask": None,
                                "data_ready": False,
                            }
                            fh.write(json.dumps(row) + "\n")
                            rows_written += 1
                            continue

                        for token_id in token_ids:
                            top = market_ws.best_prices.get(token_id, {})
                            row = {
                                "type": "sample",
                                "sample_seq": sample_seq,
                                "ts_utc": sample_utc,
                                "window_timestamp": plan.timestamp,
                                "slug": slug,
                                "market": market,
                                "token_id": token_id,
                                "outcome": market_ws.token_outcomes.get(token_id),
                                "best_bid": top.get("bid"),
                                "best_ask": top.get("ask"),
                                "data_ready": bool(top),
                            }
                            fh.write(json.dumps(row) + "\n")
                            rows_written += 1

                fh.flush()
                sample_seq += 1
                end_wall = now
                await asyncio.sleep(max(0.0, cfg.interval_seconds))

        if cfg.upload:
            key = _build_s3_key(cfg.s3_prefix, output_path.name)
            s3_uri = _upload_with_retries(output_path, cfg.s3_bucket, key, cfg.s3_region)

    finally:
        await market_ws.stop()
        ws_task.cancel()
        await asyncio.gather(ws_task, return_exceptions=True)

    return {
        "dry_run": False,
        "run_started_utc": _format_utc(start_wall),
        "run_finished_utc": _format_utc(end_wall),
        "events_completed": len(completed_indexes),
        "rows_written": rows_written,
        "samples_taken": sample_seq,
        "output_path": str(output_path),
        "output_bytes": output_path.stat().st_size if output_path.exists() else 0,
        "s3_uri": s3_uri,
        "windows": _window_meta(plans),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect BTC/ETH 5m top-of-book once per second for next N rolling windows."
    )
    parser.add_argument("--n-events", type=int, default=2, help="Number of rolling 5m windows to complete.")
    parser.add_argument("--interval-seconds", type=float, default=1.0, help="Sampling interval in seconds.")
    parser.add_argument("--output-dir", default="data/collectors", help="Directory for output artifact.")
    parser.add_argument(
        "--s3-prefix",
        default=_DEFAULT_S3_PREFIX,
        help="S3 key prefix for uploads (date/file name is appended).",
    )
    parser.add_argument("--s3-bucket", default="", help="S3 bucket override.")
    parser.add_argument("--s3-region", default="", help="S3 region override.")
    parser.add_argument("--no-upload", action="store_true", help="Disable S3 upload after collection.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned windows/slugs and exit.")
    parser.add_argument(
        "--startup-wait-seconds",
        type=float,
        default=5.0,
        help="Wait time before first sample to allow WS warmup.",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.n_events <= 0:
        parser.error("--n-events must be > 0")
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be > 0")

    args.upload = not args.no_upload

    try:
        cfg = _build_run_config(args)
        summary = asyncio.run(_collect(cfg))
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception as exc:
        logger.error("Collector failed: %s", exc)
        return 1

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
