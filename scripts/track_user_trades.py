#!/usr/bin/env python3
"""Forward trade tracker: polls Polymarket Data API for a user's trades and uploads to S3.

Usage:
    python scripts/track_user_trades.py --user @pbot-6 --duration 24h
    python scripts/track_user_trades.py --user @pbot-6 --duration 30m --poll-interval 30
    python scripts/track_user_trades.py --user @pbot-6 --duration 1d --no-upload
"""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pytz import timezone as pytz_timezone

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so `src.*` imports work when invoked from
# any directory (e.g. /opt/polymarket-bot on EC2).
# ---------------------------------------------------------------------------
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from src.logging_config import get_logger, setup_logging
from src.trade_fetcher import CSV_COLUMNS, fetch_trades_for_wallet, write_trades_csv

logger = get_logger(__name__)

OVERLAP_BUFFER_S = 30
DEFAULT_POLL_INTERVAL_S = 60
DEFAULT_FLUSH_INTERVAL_S = 300  # 5 minutes
DEFAULT_S3_PREFIX = "research/trade-tracker"
MAX_CONSECUTIVE_ERRORS = 10


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE
)


def parse_duration(raw: str) -> int:
    """Parse a human-readable duration string into seconds.

    Accepts: ``30m``, ``2h``, ``1d``, ``24h``, ``1d12h``, ``90s``, etc.
    """
    m = _DURATION_RE.match(raw.strip())
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(
            f"Invalid duration '{raw}'. Use combinations of d/h/m/s (e.g. 24h, 30m, 1d12h)."
        )
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise argparse.ArgumentTypeError("Duration must be > 0.")
    return total


# ---------------------------------------------------------------------------
# User identifier helpers (mirrored from fetch_wallet_trades.py)
# ---------------------------------------------------------------------------


def normalize_user_identifier(user: str) -> str:
    """Normalize wallet/handle/profile URL into a Data API user identifier."""
    raw = user.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        path = parsed.path.strip("/")
        if path.startswith("@"):
            return path
        if path:
            return path
    return raw


# ---------------------------------------------------------------------------
# S3 helpers (mirrored from fetch_wallet_trades.py)
# ---------------------------------------------------------------------------


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


def _require_aws_cli() -> None:
    if shutil.which("aws"):
        return
    raise RuntimeError("AWS CLI not found in PATH; required for S3 upload")


def _upload_with_retries(
    local_path: str, bucket: str, key: str, region: str, retries: int = 3
) -> str:
    cmd = [
        "aws", "s3", "cp", local_path,
        f"s3://{bucket}/{key}",
        "--region", region, "--only-show-errors",
    ]
    delay_s = 2.0
    for attempt in range(1, retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return f"s3://{bucket}/{key}"
        if attempt == retries:
            stderr = result.stderr.strip() or "unknown upload error"
            raise RuntimeError(f"S3 upload failed after {retries} attempts: {stderr}")
        time.sleep(delay_s)
        delay_s *= 2.0
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class TradeTracker:
    """Polls Polymarket Data API and accumulates deduplicated trades."""

    def __init__(
        self,
        user: str,
        duration_s: int,
        poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
        flush_interval_s: int = DEFAULT_FLUSH_INTERVAL_S,
        output_path: str = "",
        s3_bucket: str = "",
        s3_region: str = "",
        s3_prefix: str = DEFAULT_S3_PREFIX,
        no_upload: bool = False,
    ):
        self.user = normalize_user_identifier(user)
        self.duration_s = duration_s
        self.poll_interval_s = poll_interval_s
        self.flush_interval_s = flush_interval_s
        self.no_upload = no_upload

        self.s3_bucket = _resolve_bucket(s3_bucket)
        self.s3_region = _resolve_region(s3_region)
        self.s3_prefix = s3_prefix

        safe_user = self.user.lstrip("@").replace("/", "_")
        if not output_path:
            output_path = f"/tmp/tracker_{safe_user}_{int(time.time())}.csv"
        self.output_path = output_path

        self._seen_ids: set[str] = set()
        self._trades: list[dict[str, Any]] = []
        self._dupes_filtered: int = 0
        self._polls: int = 0
        self._errors: int = 0
        self._consecutive_errors: int = 0
        self._flush_count: int = 0
        self._trades_at_last_flush: int = 0
        self._shutdown_requested = False

    # -- signal handling -----------------------------------------------------

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received %s — flushing and shutting down", sig_name)
        self._shutdown_requested = True

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    # -- polling -------------------------------------------------------------

    def _poll_once(self, cursor_ts: int, now_ts: int) -> int:
        """Fetch trades for ``[cursor_ts, now_ts]``, deduplicate, return count of new trades."""
        try:
            trades = fetch_trades_for_wallet(
                wallet=self.user,
                start_ts=cursor_ts,
                end_ts=now_ts,
            )
        except Exception:
            self._errors += 1
            self._consecutive_errors += 1
            logger.exception(
                "API error (consecutive=%d)", self._consecutive_errors
            )
            return 0

        self._consecutive_errors = 0
        new_count = 0
        for t in trades:
            tid = str(t.get("id", ""))
            if not tid:
                continue
            if tid in self._seen_ids:
                self._dupes_filtered += 1
                continue
            self._seen_ids.add(tid)
            self._trades.append(t)
            new_count += 1
        return new_count

    # -- flush / upload ------------------------------------------------------

    def _flush(self) -> None:
        self._flush_count += 1
        new_since_flush = len(self._trades) - self._trades_at_last_flush
        self._trades_at_last_flush = len(self._trades)

        sorted_trades = sorted(self._trades, key=lambda t: int(t.get("timestamp", 0)))
        write_trades_csv(sorted_trades, self.output_path)
        logger.info(
            "Flush #%d: %d total trades (%d new since last flush), %d dupes filtered, "
            "csv=%s",
            self._flush_count,
            len(self._trades),
            new_since_flush,
            self._dupes_filtered,
            self.output_path,
        )

        if not self.no_upload and self.s3_bucket:
            try:
                safe_user = self.user.lstrip("@").replace("/", "_")
                s3_key = f"{self.s3_prefix.strip('/')}/{safe_user}/{Path(self.output_path).name}"
                uri = _upload_with_retries(
                    self.output_path, self.s3_bucket, s3_key, self.s3_region
                )
                logger.info("Uploaded %s", uri)
            except Exception:
                logger.exception("S3 upload failed (non-fatal, will retry next flush)")

    # -- main loop -----------------------------------------------------------

    def run(self) -> int:
        self._install_signal_handlers()

        if not self.no_upload:
            if not self.s3_bucket:
                logger.error(
                    "S3 upload enabled but no bucket configured. "
                    "Set --s3-bucket or env COLLECTOR_S3_BUCKET / LOG_SYNC_S3_BUCKET / S3_BUCKET, "
                    "or pass --no-upload."
                )
                return 1
            _require_aws_cli()

        start_ts = int(time.time())
        deadline = start_ts + self.duration_s
        cursor_ts = start_ts
        last_flush_ts = start_ts

        est = pytz_timezone("US/Eastern")
        start_dt = datetime.fromtimestamp(start_ts, tz=est)
        end_dt = datetime.fromtimestamp(deadline, tz=est)
        logger.info("--- Trade Tracker started ---")
        logger.info("User:          %s", self.user)
        logger.info("Duration:      %ds", self.duration_s)
        logger.info("Poll interval: %ds", self.poll_interval_s)
        logger.info("Flush interval:%ds", self.flush_interval_s)
        logger.info("Start (EST):   %s", start_dt.strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("End   (EST):   %s", end_dt.strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("Output CSV:    %s", self.output_path)
        logger.info(
            "S3 upload:     %s",
            "disabled" if self.no_upload else f"s3://{self.s3_bucket}/{self.s3_prefix}",
        )

        while not self._shutdown_requested:
            now_ts = int(time.time())
            if now_ts >= deadline:
                logger.info("Duration elapsed — finishing up")
                break

            self._polls += 1
            new = self._poll_once(cursor_ts, now_ts)
            remaining = deadline - now_ts
            logger.info(
                "Poll #%d: %d new trades, %d total, %s remaining",
                self._polls,
                new,
                len(self._trades),
                _fmt_seconds(remaining),
            )

            cursor_ts = now_ts - OVERLAP_BUFFER_S

            if now_ts - last_flush_ts >= self.flush_interval_s and self._trades:
                self._flush()
                last_flush_ts = now_ts

            if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.error(
                    "Aborting after %d consecutive API errors", MAX_CONSECUTIVE_ERRORS
                )
                break

            sleep_until = min(now_ts + self.poll_interval_s, deadline)
            sleep_s = max(0, sleep_until - int(time.time()))
            if sleep_s > 0 and not self._shutdown_requested:
                time.sleep(sleep_s)

        # Final flush
        if self._trades:
            logger.info("Final flush (%d trades)", len(self._trades))
            self._flush()

        logger.info(
            "--- Tracker finished --- polls=%d, trades=%d, dupes_filtered=%d, errors=%d",
            self._polls,
            len(self._trades),
            self._dupes_filtered,
            self._errors,
        )
        return 0


def _fmt_seconds(s: int) -> str:
    if s >= 86400:
        return f"{s // 86400}d {(s % 86400) // 3600}h {(s % 3600) // 60}m"
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60}m {s % 60}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Forward-track a Polymarket user's trades via REST polling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/track_user_trades.py --user @pbot-6 --duration 24h
  python scripts/track_user_trades.py --user @pbot-6 --duration 30m --poll-interval 30
  python scripts/track_user_trades.py --user @pbot-6 --duration 1d --no-upload
        """,
    )
    parser.add_argument(
        "--user", required=True,
        help="Polymarket user: wallet (0x...), handle (@name), or profile URL",
    )
    parser.add_argument(
        "--duration", required=True,
        help="How long to track (e.g. 30m, 2h, 1d, 24h, 1d12h)",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL_S,
        help=f"Seconds between polls (default: {DEFAULT_POLL_INTERVAL_S})",
    )
    parser.add_argument(
        "--flush-interval", type=int, default=DEFAULT_FLUSH_INTERVAL_S,
        help=f"Seconds between CSV flush + S3 upload (default: {DEFAULT_FLUSH_INTERVAL_S})",
    )
    parser.add_argument("--output", default="", help="Output CSV path (default: /tmp/tracker_<user>_<ts>.csv)")
    parser.add_argument("--s3-prefix", default=DEFAULT_S3_PREFIX, help="S3 key prefix")
    parser.add_argument("--s3-bucket", default="", help="S3 bucket override")
    parser.add_argument("--s3-region", default="", help="S3 region override")
    parser.add_argument("--no-upload", action="store_true", help="Disable S3 upload")

    args = parser.parse_args()
    setup_logging()

    duration_s = parse_duration(args.duration)

    tracker = TradeTracker(
        user=args.user,
        duration_s=duration_s,
        poll_interval_s=args.poll_interval,
        flush_interval_s=args.flush_interval,
        output_path=args.output,
        s3_bucket=args.s3_bucket,
        s3_region=args.s3_region,
        s3_prefix=args.s3_prefix,
        no_upload=args.no_upload,
    )
    return tracker.run()


if __name__ == "__main__":
    sys.exit(main())
