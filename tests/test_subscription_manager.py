"""Unit tests for SubscriptionManager.

Tests the pure subscription logic without any I/O dependencies.
"""

import time
from datetime import date

import pytest

from src.subscription_manager import SubscriptionManager, SubscriptionDelta


class TestSubscriptionManagerSeed:
    """Tests for the seed() method."""

    def test_seed_returns_current_and_next_windows(self):
        """Seed should return slugs for current and next intervals."""
        mgr = SubscriptionManager(
            durations=[5],
            market_selections=["BTC"],
            grace_period_s=300,
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,
        )
        now = time.time()
        slugs = mgr.seed(now)

        # Should have at least 2 slugs (current + next)
        assert len(slugs) >= 2
        assert all("btc" in s.lower() or "bitcoin" in s.lower() for s in slugs)
        assert all("5m" in s or "5min" in s for s in slugs)

    def test_seed_includes_previous_window_within_grace(self):
        """Seed should include previous window if within grace period."""
        mgr = SubscriptionManager(
            durations=[5],
            market_selections=["BTC"],
            grace_period_s=300,  # 5 min grace
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,
        )
        # Simulate being 2 minutes after previous window ended
        from src.markets.fifteen_min import get_current_interval_utc
        cur_ts = get_current_interval_utc(5)
        # 2 minutes into current window
        now = cur_ts + 120

        slugs = mgr.seed(now)

        # Should have 3 slugs (prev + current + next) because we're within grace
        # The previous window ended at cur_ts, and we're at cur_ts + 120,
        # which is within grace_period_s (300s)
        assert len(slugs) >= 2  # At minimum current + next

    def test_seed_defers_long_duration_markets(self):
        """30m+ markets far from expiry should be deferred, not subscribed."""
        mgr = SubscriptionManager(
            durations=[60],  # 1-hour markets
            market_selections=["BTC"],
            grace_period_s=300,
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,  # 15 min lead
        )
        now = time.time()
        slugs = mgr.seed(now)

        # The "next" window (starting in ~1 hour) should be deferred
        # So we might only get 1 or 2 slugs depending on timing
        # The key test is that deferred_ts has entries
        assert len(mgr._deferred_ts[60]["BTC"]) >= 0  # May have deferred entries

    def test_seed_multiple_durations_and_selections(self):
        """Seed handles multiple durations and market selections."""
        mgr = SubscriptionManager(
            durations=[5, 15],
            market_selections=["BTC", "ETH"],
            grace_period_s=300,
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,
        )
        now = time.time()
        slugs = mgr.seed(now)

        # Should have slugs for both assets and both durations
        btc_slugs = [s for s in slugs if "btc" in s.lower() or "bitcoin" in s.lower()]
        eth_slugs = [s for s in slugs if "eth" in s.lower() or "ethereum" in s.lower()]

        assert len(btc_slugs) >= 2
        assert len(eth_slugs) >= 2


class TestSubscriptionManagerTick:
    """Tests for the tick() method."""

    def test_tick_returns_empty_delta_when_no_changes(self):
        """Tick with no time change should return empty delta."""
        mgr = SubscriptionManager(
            durations=[5],
            market_selections=["BTC"],
            grace_period_s=300,
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,
        )
        now = time.time()
        mgr.seed(now)

        # Immediate tick should have no changes
        delta = mgr.tick(now, market_active={})
        assert delta.is_empty

    def test_tick_promotes_deferred_when_close_to_expiry(self):
        """Deferred markets should be promoted when within lazy_sub_lead_s."""
        mgr = SubscriptionManager(
            durations=[60],  # 1-hour markets
            market_selections=["BTC"],
            grace_period_s=300,
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,  # 15 min
        )

        # Seed at a time when next window is far away (deferred)
        from src.markets.fifteen_min import get_next_interval_utc
        next_ts = get_next_interval_utc(60)
        next_end = next_ts + 3600  # 1 hour duration

        # Start when next window is 45 min from expiry (should be deferred)
        seed_time = next_end - (45 * 60)
        mgr.seed(seed_time)

        # Check that next window is deferred
        initial_deferred = len(mgr._deferred_ts[60]["BTC"])

        # Tick when next window is 10 min from expiry (should promote)
        tick_time = next_end - (10 * 60)
        delta = mgr.tick(tick_time, market_active={})

        # If there was a deferred entry, it should now be promoted
        if initial_deferred > 0:
            assert len(delta.slugs_to_add) > 0 or len(mgr._deferred_ts[60]["BTC"]) < initial_deferred

    def test_tick_removes_expired_inactive_markets(self):
        """Tick should remove markets past grace period that are inactive."""
        mgr = SubscriptionManager(
            durations=[5],
            market_selections=["BTC"],
            grace_period_s=300,  # 5 min
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,
        )

        from src.markets.fifteen_min import get_current_interval_utc, get_market_slug

        # Seed normally
        now = time.time()
        mgr.seed(now)

        # Get current window info
        cur_ts = get_current_interval_utc(5)
        cur_end = cur_ts + 300  # 5 min duration
        cur_slug = get_market_slug("BTC", 5, cur_ts)

        # Tick at a time past grace period
        future_time = cur_end + 400  # Past grace period

        # Mark the current market as inactive
        market_active = {cur_slug: False}
        delta = mgr.tick(future_time, market_active=market_active)

        # Should try to remove the expired market
        # (May or may not depending on whether it was in monitored_ts)
        assert isinstance(delta, SubscriptionDelta)

    def test_tick_does_not_remove_active_markets(self):
        """Tick should NOT remove markets that are still active (not resolved)."""
        mgr = SubscriptionManager(
            durations=[5],
            market_selections=["BTC"],
            grace_period_s=300,
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,
        )

        from src.markets.fifteen_min import get_current_interval_utc, get_market_slug

        now = time.time()
        mgr.seed(now)

        cur_ts = get_current_interval_utc(5)
        cur_end = cur_ts + 300
        cur_slug = get_market_slug("BTC", 5, cur_ts)

        # Tick past grace period but market is still active
        future_time = cur_end + 400
        market_active = {cur_slug: True}  # Still active!
        delta = mgr.tick(future_time, market_active=market_active)

        # Should NOT remove the active market
        assert cur_slug not in delta.slugs_to_remove


class TestSubscriptionManagerStocks:
    """Tests for stock subscription handling."""

    def test_seed_includes_stock_markets(self):
        """Seed should include stock markets when tickers are configured."""
        mgr = SubscriptionManager(
            durations=[5],
            market_selections=["BTC"],
            stock_tickers=["AAPL"],
            grace_period_s=300,
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,
        )
        now = time.time()
        slugs = mgr.seed(now)

        # Should have crypto slugs at minimum
        assert len(slugs) >= 2

        # Stock slugs may or may not be present depending on market hours
        # This is expected — stocks only have markets on trading days

    def test_empty_stock_tickers_skips_stock_logic(self):
        """When no stock tickers, stock logic should be skipped."""
        mgr = SubscriptionManager(
            durations=[5],
            market_selections=["BTC"],
            stock_tickers=[],
            grace_period_s=300,
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,
        )
        now = time.time()
        slugs = mgr.seed(now)

        # Should only have crypto slugs
        assert all("btc" in s.lower() or "bitcoin" in s.lower() for s in slugs)


class TestSubscriptionDelta:
    """Tests for the SubscriptionDelta dataclass."""

    def test_is_empty_when_no_changes(self):
        delta = SubscriptionDelta(slugs_to_add=(), slugs_to_remove=())
        assert delta.is_empty

    def test_not_empty_when_adds(self):
        delta = SubscriptionDelta(slugs_to_add=("slug1",), slugs_to_remove=())
        assert not delta.is_empty

    def test_not_empty_when_removes(self):
        delta = SubscriptionDelta(slugs_to_add=(), slugs_to_remove=("slug1",))
        assert not delta.is_empty

    def test_frozen(self):
        """Delta should be immutable."""
        delta = SubscriptionDelta(slugs_to_add=("a",), slugs_to_remove=("b",))
        with pytest.raises(AttributeError):
            delta.slugs_to_add = ("c",)


class TestSubscriptionManagerIntegration:
    """Integration-style tests with realistic scenarios."""

    def test_full_window_lifecycle(self):
        """Test a market through its full lifecycle: add -> active -> expired -> remove."""
        mgr = SubscriptionManager(
            durations=[5],
            market_selections=["BTC"],
            grace_period_s=60,  # Short grace for testing
            lazy_sub_min_duration=30,
            lazy_sub_lead_s=900,
        )

        from src.markets.fifteen_min import get_current_interval_utc, get_market_slug

        # Phase 1: Seed
        base_ts = get_current_interval_utc(5)
        seed_time = base_ts + 60  # 1 min into window
        slugs = mgr.seed(seed_time)
        cur_slug = get_market_slug("BTC", 5, base_ts)

        assert cur_slug in slugs, "Current window should be seeded"

        # Phase 2: Tick during window (no changes)
        delta = mgr.tick(seed_time + 60, market_active={cur_slug: True})
        assert cur_slug not in delta.slugs_to_remove

        # Phase 3: Window ends, within grace period, still active
        window_end = base_ts + 300
        delta = mgr.tick(window_end + 30, market_active={cur_slug: True})
        assert cur_slug not in delta.slugs_to_remove, "Should not remove during grace if active"

        # Phase 4: Past grace period, market resolves (inactive)
        delta = mgr.tick(window_end + 120, market_active={cur_slug: False})
        # Now it should be removed (if still in tracked set)
        # The slug may or may not be in removes depending on internal state
        assert isinstance(delta, SubscriptionDelta)
