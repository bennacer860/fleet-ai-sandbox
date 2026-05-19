"""Tests for MarketWebSocketPool deduplication logic."""

import pytest
from src.gateway.market_ws_pool import LRUDedup, _event_dedup_key, PoolMetrics
from src.core.events import TickSizeChange, MarketResolved, BookUpdate


class TestLRUDedup:
    def test_first_seen_is_not_duplicate(self):
        dedup = LRUDedup(max_size=100, ttl_s=5.0)
        assert dedup.is_duplicate("key1") is False

    def test_second_seen_is_duplicate(self):
        dedup = LRUDedup(max_size=100, ttl_s=5.0)
        dedup.is_duplicate("key1")
        assert dedup.is_duplicate("key1") is True

    def test_different_keys_not_duplicate(self):
        dedup = LRUDedup(max_size=100, ttl_s=5.0)
        dedup.is_duplicate("key1")
        assert dedup.is_duplicate("key2") is False

    def test_cache_evicts_oldest(self):
        dedup = LRUDedup(max_size=3, ttl_s=5.0)
        dedup.is_duplicate("key1")
        dedup.is_duplicate("key2")
        dedup.is_duplicate("key3")
        dedup.is_duplicate("key4")  # evicts key1
        # key1 should no longer be in cache
        assert dedup.is_duplicate("key1") is False


class TestEventDedupKey:
    def test_tick_size_change_key(self):
        event = TickSizeChange(
            condition_id="cond1",
            slug="btc-updown-5m-123",
            token_id="token1",
            old_tick_size="0.01",
            new_tick_size="0.001",
        )
        key = _event_dedup_key(event)
        assert key == "tick:btc-updown-5m-123:0.001"

    def test_market_resolved_key(self):
        event = MarketResolved(
            slug="btc-updown-5m-123",
            condition_id="cond1",
            winning_token_id="winner123",
        )
        key = _event_dedup_key(event)
        assert key == "resolved:btc-updown-5m-123:winner123"

    def test_book_update_key(self):
        event = BookUpdate(
            token_id="token1",
            condition_id="cond1",
            slug="btc-updown-5m-123",
            bids=((0.5, 100.0),),
            asks=((0.51, 100.0),),
            best_bid=0.5,
            best_ask=0.51,
        )
        key = _event_dedup_key(event)
        assert key == "book:token1:0.5:0.51"

    def test_same_events_same_key(self):
        event1 = TickSizeChange(
            condition_id="cond1",
            slug="btc-updown-5m-123",
            token_id="token1",
            old_tick_size="0.01",
            new_tick_size="0.001",
        )
        event2 = TickSizeChange(
            condition_id="cond1",
            slug="btc-updown-5m-123",
            token_id="token2",  # different token, same slug
            old_tick_size="0.01",
            new_tick_size="0.001",
        )
        # Both should have same dedup key (keyed by slug, not token)
        assert _event_dedup_key(event1) == _event_dedup_key(event2)


class TestPoolMetrics:
    def test_record_first_seen(self):
        m = PoolMetrics()
        m.record_event(conn_idx=0, is_first=True)
        assert m.events_received[0] == 1
        assert m.first_seen_wins[0] == 1
        assert m.total_events == 1
        assert m.duplicates_dropped == 0

    def test_record_duplicate(self):
        m = PoolMetrics()
        m.record_event(conn_idx=0, is_first=True)
        m.record_event(conn_idx=1, is_first=False)  # duplicate from conn 1
        assert m.events_received[0] == 1
        assert m.events_received[1] == 1
        assert m.first_seen_wins[0] == 1
        assert m.first_seen_wins.get(1, 0) == 0
        assert m.total_events == 1
        assert m.duplicates_dropped == 1

    def test_multiple_connections_competing(self):
        m = PoolMetrics()
        # Simulate 5 connections each sending 10 events, conn 0 wins 6, others win rest
        for _ in range(6):
            m.record_event(0, is_first=True)
            for c in range(1, 5):
                m.record_event(c, is_first=False)
        for c in range(1, 5):
            m.record_event(c, is_first=True)
            for other in range(5):
                if other != c:
                    m.record_event(other, is_first=False)

        assert m.total_events == 10
        assert m.first_seen_wins[0] == 6
        # Total duplicates = (6 * 4) + (4 * 4) = 24 + 16 = 40
        assert m.duplicates_dropped == 40
