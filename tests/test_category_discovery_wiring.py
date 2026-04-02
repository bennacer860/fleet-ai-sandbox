from __future__ import annotations

import asyncio
from argparse import Namespace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("aiohttp")

from main import cmd_run
from src.bot import Bot


def _base_run_args() -> Namespace:
    return Namespace(
        strategy="post_expiry",
        profile=None,
        markets=None,
        categories=["weather/temperature"],
        discovery_refresh_s=60.0,
        durations=[1440],
        price_threshold=0.99,
        early_tick_threshold=0.995,
        dry_run=True,
        dashboard=False,
        db_path=None,
        persist="false",
        claim=None,
        claim_interval=60.0,
        fill_mode="book",
        tag="",
    )


def test_cmd_run_category_only_uses_discovery():
    args = _base_run_args()

    with patch("src.markets.discovery.discover_slugs", return_value=["weather-temp-daily-slug"]) as discover_mock, patch(
        "src.bot.Bot"
    ) as bot_cls:
        bot = bot_cls.return_value
        bot.run_sync.return_value = None
        rc = cmd_run(args)

    assert rc == 0
    kwargs = bot_cls.call_args.kwargs
    assert kwargs["slugs"] == ["weather-temp-daily-slug"]
    assert kwargs["market_selections"] == []
    assert kwargs["category_paths"] == ["weather/temperature"]
    discover_mock.assert_called_once()
    assert discover_mock.call_args.kwargs["lead_time_seconds"] == 30 * 60


def test_bot_refresh_discovery_adds_and_removes():
    bot = Bot(
        slugs=["btc-updown-15m-1773541800"],
        strategy_name="post_expiry",
        dry_run=True,
        persist=False,
        market_selections=["BTC"],
        durations=[15, 1440],
        category_paths=["weather/temperature"],
        discovery_refresh_s=10.0,
    )
    bot.market_ws.add_markets = AsyncMock()
    bot.market_ws.remove_markets = AsyncMock()
    bot._last_discovery_refresh_mono = 0.0
    bot._eval_cache["weather-old"] = {"x": 1}
    bot._discovered_category_slugs = {"weather-old"}

    async def _run() -> None:
        with patch("src.bot.discover_slugs", return_value=["weather-new"]) as discover_mock:
            await bot._refresh_discovered_categories()
            discover_mock.assert_called_once()
            assert discover_mock.call_args.kwargs["lead_time_seconds"] == 30 * 60

    asyncio.run(_run())

    bot.market_ws.add_markets.assert_awaited_once_with(["weather-new"])
    bot.market_ws.remove_markets.assert_awaited_once_with(["weather-old"])
    assert "weather-old" not in bot._eval_cache
