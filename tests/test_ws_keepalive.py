"""Tests for MarketWebSocket subscription keepalive and heartbeat watchdog.

Exercises the keepalive loop, reactive heartbeat resubscribe, and their
interaction using a fake WebSocket that records sent messages.
"""

import asyncio
import time
from unittest.mock import patch

import orjson
import pytest

from src.core.event_bus import EventBus
from src.gateway.market_ws import MarketWebSocket


# ── Helpers ────────────────────────────────────────────────────────────────


class FakeWebSocket:
    """Minimal stand-in for websockets.WebSocketClientProtocol."""

    def __init__(self):
        self.sent: list[str] = []
        self.closed = False

    async def send(self, msg: str) -> None:
        if self.closed:
            raise Exception("connection closed")
        self.sent.append(msg)

    async def close(self) -> None:
        self.closed = True

    def subscribe_messages(self) -> list[dict]:
        """Return only subscribe-type JSON payloads sent on this socket."""
        results = []
        for raw in self.sent:
            try:
                data = orjson.loads(raw)
                if isinstance(data, dict) and data.get("type") == "subscribe":
                    results.append(data)
            except Exception:
                pass
        return results


def _setup_ws(ws: MarketWebSocket, fake_ws: FakeWebSocket) -> None:
    """Wire a MarketWebSocket to use a FakeWebSocket as its live connection."""
    ws._websocket = fake_ws
    ws._running = True
    ws._connected_since = time.monotonic()
    ws._last_data_message_time = 0.0
    ws._stale_resubscribe_attempts = 0
    ws._last_stale_resubscribe_ts = 0.0


def _make_market_ws(**kwargs) -> MarketWebSocket:
    """Create a MarketWebSocket with pre-loaded token state (no HTTP calls)."""
    bus = EventBus()
    mws = MarketWebSocket(event_bus=bus, **kwargs)
    mws.token_ids = {
        "btc-5m-market-A": ["tok_a1", "tok_a2"],
        "eth-5m-market-B": ["tok_b1", "tok_b2"],
    }
    mws.slug_by_token = {
        "tok_a1": "btc-5m-market-A",
        "tok_a2": "btc-5m-market-A",
        "tok_b1": "eth-5m-market-B",
        "tok_b2": "eth-5m-market-B",
    }
    mws.market_active = {
        "btc-5m-market-A": True,
        "eth-5m-market-B": True,
    }
    return mws


# ── Keepalive tests ───────────────────────────────────────────────────────


class TestSubscriptionKeepalive:

    def test_keepalive_sends_subscribe_on_interval(self):
        """Keepalive should send a subscribe message after each interval."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)

            with patch("src.gateway.market_ws.SUBSCRIPTION_KEEPALIVE_S", 0.05):
                task = asyncio.create_task(mws._subscription_keepalive(fake))
                await asyncio.sleep(0.18)
                mws._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            subs = fake.subscribe_messages()
            assert len(subs) >= 2, f"Expected >= 2 keepalive subscribes, got {len(subs)}"
            for sub in subs:
                assert set(sub["assets_ids"]) == {"tok_a1", "tok_a2", "tok_b1", "tok_b2"}
                assert "book" in sub["channels"]

        asyncio.run(_run())

    def test_keepalive_increments_counter(self):
        """Each keepalive cycle should bump _keepalive_count."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)
            assert mws.keepalive_count == 0

            with patch("src.gateway.market_ws.SUBSCRIPTION_KEEPALIVE_S", 0.05):
                task = asyncio.create_task(mws._subscription_keepalive(fake))
                await asyncio.sleep(0.18)
                mws._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            assert mws.keepalive_count >= 2

        asyncio.run(_run())

    def test_keepalive_stops_when_ws_changes(self):
        """Keepalive must exit when a different WS replaces the original."""
        async def _run():
            mws = _make_market_ws()
            fake_old = FakeWebSocket()
            fake_new = FakeWebSocket()
            _setup_ws(mws, fake_old)

            with patch("src.gateway.market_ws.SUBSCRIPTION_KEEPALIVE_S", 0.05):
                task = asyncio.create_task(mws._subscription_keepalive(fake_old))
                await asyncio.sleep(0.03)
                mws._websocket = fake_new  # simulate reconnect
                await asyncio.sleep(0.15)
                mws._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            assert len(fake_new.subscribe_messages()) == 0

        asyncio.run(_run())

    def test_keepalive_stops_when_running_false(self):
        """Keepalive should exit promptly when _running is cleared."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)

            with patch("src.gateway.market_ws.SUBSCRIPTION_KEEPALIVE_S", 0.05):
                task = asyncio.create_task(mws._subscription_keepalive(fake))
                await asyncio.sleep(0.02)
                mws._running = False
                await asyncio.sleep(0.12)
                assert task.done() or task.cancelled()

        asyncio.run(_run())

    def test_keepalive_skips_when_no_tokens(self):
        """If all markets are removed, keepalive should not send subscribes."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)
            mws.token_ids.clear()

            with patch("src.gateway.market_ws.SUBSCRIPTION_KEEPALIVE_S", 0.05):
                task = asyncio.create_task(mws._subscription_keepalive(fake))
                await asyncio.sleep(0.18)
                mws._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            assert len(fake.subscribe_messages()) == 0

        asyncio.run(_run())

    def test_keepalive_exits_on_send_error(self):
        """If the WS send raises, the keepalive loop should break cleanly."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)

            with patch("src.gateway.market_ws.SUBSCRIPTION_KEEPALIVE_S", 0.05):
                task = asyncio.create_task(mws._subscription_keepalive(fake))
                await asyncio.sleep(0.03)
                fake.closed = True  # next send will raise
                await asyncio.sleep(0.15)
                assert task.done()

        asyncio.run(_run())


# ── Heartbeat watchdog tests ──────────────────────────────────────────────


class TestHeartbeatWatchdog:

    def test_reactive_resub_fires_on_data_gap(self):
        """Watchdog should fire a reactive resub when data goes silent."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)
            mws._connected_since = time.monotonic() - 10

            with (
                patch("src.gateway.market_ws.HEARTBEAT_CHECK_INTERVAL_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_AFTER_S", 0.1),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_EVERY_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_MAX_RESUBSCRIBE_ATTEMPTS", 3),
                patch("src.gateway.market_ws.HEARTBEAT_RECONNECT_AFTER_S", 999),
            ):
                task = asyncio.create_task(mws._heartbeat_watchdog())
                await asyncio.sleep(0.35)
                mws._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            assert mws.resubscribe_count >= 1, "Expected at least 1 reactive resub"
            subs = fake.subscribe_messages()
            assert len(subs) >= 1

        asyncio.run(_run())

    def test_reactive_resub_resets_on_data(self):
        """After data arrives, the stale attempt counter should reset."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)
            mws._connected_since = time.monotonic() - 10

            with (
                patch("src.gateway.market_ws.HEARTBEAT_CHECK_INTERVAL_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_AFTER_S", 0.1),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_EVERY_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_MAX_RESUBSCRIBE_ATTEMPTS", 3),
                patch("src.gateway.market_ws.HEARTBEAT_RECONNECT_AFTER_S", 999),
            ):
                task = asyncio.create_task(mws._heartbeat_watchdog())
                await asyncio.sleep(0.2)
                resubs_before = mws.resubscribe_count
                assert resubs_before >= 1

                mws._mark_data_message("book")
                assert mws._stale_resubscribe_attempts == 0

                mws._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_run())

    def test_watchdog_caps_at_max_attempts(self):
        """Reactive resubs should not exceed HEARTBEAT_MAX_RESUBSCRIBE_ATTEMPTS."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)
            mws._connected_since = time.monotonic() - 10

            max_attempts = 2
            with (
                patch("src.gateway.market_ws.HEARTBEAT_CHECK_INTERVAL_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_AFTER_S", 0.1),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_EVERY_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_MAX_RESUBSCRIBE_ATTEMPTS", max_attempts),
                patch("src.gateway.market_ws.HEARTBEAT_RECONNECT_AFTER_S", 999),
            ):
                task = asyncio.create_task(mws._heartbeat_watchdog())
                await asyncio.sleep(0.5)
                mws._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            assert mws.resubscribe_count == max_attempts

        asyncio.run(_run())

    def test_watchdog_forces_reconnect_on_prolonged_silence(self):
        """After HEARTBEAT_RECONNECT_AFTER_S the watchdog should close the WS."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)
            mws._connected_since = time.monotonic() - 10

            with (
                patch("src.gateway.market_ws.HEARTBEAT_CHECK_INTERVAL_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_AFTER_S", 0.1),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_EVERY_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_MAX_RESUBSCRIBE_ATTEMPTS", 1),
                patch("src.gateway.market_ws.HEARTBEAT_RECONNECT_AFTER_S", 0.2),
            ):
                task = asyncio.create_task(mws._heartbeat_watchdog())
                await asyncio.sleep(0.35)
                mws._running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            assert fake.closed, "Watchdog should have closed the WS after prolonged silence"

        asyncio.run(_run())


# ── Keepalive + watchdog interaction ──────────────────────────────────────


class TestKeepaliveWatchdogInteraction:

    def test_keepalive_prevents_reactive_resubs(self):
        """When keepalive keeps data flowing, reactive resubs should not fire."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)

            async def simulate_data_flow():
                """Pretend each keepalive subscribe causes data to resume."""
                while mws._running:
                    await asyncio.sleep(0.03)
                    mws._mark_data_message("book")

            with (
                patch("src.gateway.market_ws.SUBSCRIPTION_KEEPALIVE_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_CHECK_INTERVAL_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_AFTER_S", 0.2),
                patch("src.gateway.market_ws.HEARTBEAT_RECONNECT_AFTER_S", 999),
            ):
                keepalive = asyncio.create_task(mws._subscription_keepalive(fake))
                watchdog = asyncio.create_task(mws._heartbeat_watchdog())
                data_sim = asyncio.create_task(simulate_data_flow())

                await asyncio.sleep(0.4)
                mws._running = False
                for t in (keepalive, watchdog, data_sim):
                    t.cancel()
                await asyncio.gather(keepalive, watchdog, data_sim, return_exceptions=True)

            assert mws.keepalive_count >= 2, "Keepalive should have fired"
            assert mws.resubscribe_count == 0, (
                f"With steady data, reactive resubs should be 0 but got {mws.resubscribe_count}"
            )

        asyncio.run(_run())

    def test_reactive_resub_still_fires_despite_keepalive_if_data_stops(self):
        """If keepalive is running but data stops, the watchdog still reacts."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)
            mws._connected_since = time.monotonic() - 10

            with (
                patch("src.gateway.market_ws.SUBSCRIPTION_KEEPALIVE_S", 0.3),
                patch("src.gateway.market_ws.HEARTBEAT_CHECK_INTERVAL_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_AFTER_S", 0.1),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_EVERY_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_MAX_RESUBSCRIBE_ATTEMPTS", 3),
                patch("src.gateway.market_ws.HEARTBEAT_RECONNECT_AFTER_S", 999),
            ):
                keepalive = asyncio.create_task(mws._subscription_keepalive(fake))
                watchdog = asyncio.create_task(mws._heartbeat_watchdog())

                await asyncio.sleep(0.4)
                mws._running = False
                for t in (keepalive, watchdog):
                    t.cancel()
                await asyncio.gather(keepalive, watchdog, return_exceptions=True)

            assert mws.resubscribe_count >= 1, (
                "Watchdog should still react when data is absent, even with keepalive running"
            )

        asyncio.run(_run())


# ── Counter independence ─────────────────────────────────────────────────


class TestCounterSeparation:

    def test_keepalive_and_resub_counts_are_independent(self):
        """keepalive_count and resubscribe_count should track separately."""
        async def _run():
            mws = _make_market_ws()
            fake = FakeWebSocket()
            _setup_ws(mws, fake)
            mws._connected_since = time.monotonic() - 10

            with (
                patch("src.gateway.market_ws.SUBSCRIPTION_KEEPALIVE_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_CHECK_INTERVAL_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_AFTER_S", 0.1),
                patch("src.gateway.market_ws.HEARTBEAT_RESUBSCRIBE_EVERY_S", 0.05),
                patch("src.gateway.market_ws.HEARTBEAT_MAX_RESUBSCRIBE_ATTEMPTS", 2),
                patch("src.gateway.market_ws.HEARTBEAT_RECONNECT_AFTER_S", 999),
            ):
                keepalive = asyncio.create_task(mws._subscription_keepalive(fake))
                watchdog = asyncio.create_task(mws._heartbeat_watchdog())

                await asyncio.sleep(0.4)
                mws._running = False
                for t in (keepalive, watchdog):
                    t.cancel()
                await asyncio.gather(keepalive, watchdog, return_exceptions=True)

            assert mws.keepalive_count >= 1, "Keepalive should have incremented"
            assert mws.resubscribe_count >= 1, "Reactive resub should have incremented"

        asyncio.run(_run())

    def test_initial_counts_are_zero(self):
        """Both counters should start at zero."""
        mws = _make_market_ws()
        assert mws.keepalive_count == 0
        assert mws.resubscribe_count == 0
        assert mws.reconnect_count == 0


# ── Subscribe message correctness ────────────────────────────────────────


class TestSubscribeMessageFormat:

    def test_subscribe_message_contains_all_tokens(self):
        mws = _make_market_ws()
        all_tids = mws._all_token_ids()
        msg = mws._build_subscribe_message(all_tids)
        data = orjson.loads(msg)

        assert data["type"] == "subscribe"
        assert set(data["assets_ids"]) == {"tok_a1", "tok_a2", "tok_b1", "tok_b2"}
        assert data["channels"] == ["book", "price_change", "tick_size_change"]

    def test_subscribe_message_after_market_removal(self):
        mws = _make_market_ws()
        del mws.token_ids["btc-5m-market-A"]
        all_tids = mws._all_token_ids()
        msg = mws._build_subscribe_message(all_tids)
        data = orjson.loads(msg)

        assert set(data["assets_ids"]) == {"tok_b1", "tok_b2"}


# ── mark_data_message behaviour ──────────────────────────────────────────


class TestMarkDataMessage:

    def test_resets_stale_counters(self):
        mws = _make_market_ws()
        mws._stale_resubscribe_attempts = 3
        mws._last_stale_resubscribe_ts = 12345.0

        mws._mark_data_message("book")

        assert mws._stale_resubscribe_attempts == 0
        assert mws._last_stale_resubscribe_ts == 0.0
        assert mws._last_data_message_time > 0

    def test_updates_channel_time(self):
        mws = _make_market_ws()
        before = time.monotonic()
        mws._mark_data_message("price_change")
        after = time.monotonic()

        assert before <= mws._last_channel_message_time["price_change"] <= after

    def test_ignores_unknown_channel(self):
        mws = _make_market_ws()
        mws._mark_data_message("unknown_channel")
        assert mws._last_channel_message_time.get("unknown_channel") is None
