"""Lightweight metrics collection — counters, gauges, and histograms.

Thread-safe singleton that can be read from the dashboard / health
endpoint at any time without locks (reads are atomic on CPython for
simple types).
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class Metrics:
    """In-process metrics collector."""

    _instance: Metrics | None = None

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._start_time = time.monotonic()
        self._max_histogram_size = 1000

    @classmethod
    def get(cls) -> Metrics:
        if cls._instance is None:
            cls._instance = Metrics()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    # ── Write API ─────────────────────────────────────────────────────────

    def inc(self, name: str, value: int = 1) -> None:
        self._counters[name] += value

    def set(self, name: str, value: float) -> None:
        self._gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        hist = self._histograms[name]
        hist.append(value)
        if len(hist) > self._max_histogram_size:
            self._histograms[name] = hist[-500:]

    # ── Read API ──────────────────────────────────────────────────────────

    def get_counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    def get_gauge(self, name: str) -> float:
        return self._gauges.get(name, 0.0)

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self._start_time

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "uptime_s": round(self.uptime_s, 1),
            "counters": dict(self._counters),
            "gauges": {k: round(v, 4) for k, v in self._gauges.items()},
        }
        histograms: dict[str, dict[str, Any]] = {}
        for name, values in self._histograms.items():
            if not values:
                continue
            s = sorted(values)
            n = len(s)
            histograms[name] = {
                "count": n,
                "p50": round(s[n // 2], 2),
                "p95": round(s[int(n * 0.95)], 2),
                "p99": round(s[min(int(n * 0.99), n - 1)], 2),
                "min": round(s[0], 2),
                "max": round(s[-1], 2),
            }
        if histograms:
            result["histograms"] = histograms
        return result
