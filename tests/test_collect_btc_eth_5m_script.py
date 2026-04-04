"""Smoke checks for the BTC/ETH 5m collector script dry-run mode."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect_btc_eth_5m.py"


def _run_dry(n_events: int) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--dry-run",
            "--no-upload",
            "--n-events",
            str(n_events),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_dry_run_n1_emits_single_window() -> None:
    out = _run_dry(1)
    assert out["dry_run"] is True
    assert out["duration_minutes"] == 5
    assert len(out["windows"]) == 1
    slugs = out["windows"][0]["slugs"]
    assert set(slugs.keys()) == {"BTC", "ETH"}
    assert slugs["BTC"].startswith("btc-updown-5m-")
    assert slugs["ETH"].startswith("eth-updown-5m-")


def test_dry_run_n3_emits_rolling_windows() -> None:
    out = _run_dry(3)
    windows = out["windows"]
    assert len(windows) == 3
    timestamps = [w["timestamp"] for w in windows]
    deltas = [b - a for a, b in zip(timestamps, timestamps[1:])]
    assert deltas == [300, 300]
