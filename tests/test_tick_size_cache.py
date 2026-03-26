"""Tests for tick-size cache timestamp fix in place_limit_order.

Verifies that setting __tick_size_timestamps alongside __tick_sizes
prevents get_tick_size() from making a blocking HTTP call on every order.
"""

import time
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from py_clob_client.client import ClobClient


@pytest.fixture
def mock_client():
    """Create a ClobClient with mocked credentials (no real connection)."""
    with patch.object(ClobClient, "__init__", lambda self, *a, **kw: None):
        client = ClobClient.__new__(ClobClient)
        # Initialise the private caches that the real __init__ sets up
        client.host = "https://clob.example.com"
        client._ClobClient__tick_sizes = {}
        client._ClobClient__tick_size_timestamps = {}
        client._ClobClient__tick_size_ttl = 300.0
        client._ClobClient__neg_risk = {"tok_abc": True}
        client._ClobClient__fee_rates = {"tok_abc": 0}
        client._ClobClient__api_creds = MagicMock()
        return client


class TestTickSizeCacheTimestamp:
    """Verify that place_limit_order populates the TTL timestamp."""

    def test_timestamp_set_when_tick_size_provided(self, mock_client):
        """After place_limit_order writes __tick_sizes, the TTL timestamp
        must also be set so get_tick_size() returns the cached value."""
        token_id = "tok_abc"

        # Simulate what place_limit_order does after the fix
        from src.clob_client import _TICK_SIZE_MAP
        tick_size = 0.001
        ts_str = _TICK_SIZE_MAP[tick_size]

        mock_client._ClobClient__tick_sizes[token_id] = ts_str
        mock_client._ClobClient__tick_size_timestamps[token_id] = time.monotonic()

        # get_tick_size should return from cache without HTTP
        with patch("py_clob_client.client.get") as mock_get:
            result = mock_client.get_tick_size(token_id)

        assert result == ts_str
        mock_get.assert_not_called()

    def test_missing_timestamp_causes_http_call(self, mock_client):
        """Without the timestamp, get_tick_size falls through to HTTP
        even when __tick_sizes has the value — this is the bug we fixed."""
        token_id = "tok_abc"
        ts_str = "0.001"

        # Only set the value, NOT the timestamp (the old broken behaviour)
        mock_client._ClobClient__tick_sizes[token_id] = ts_str

        with patch("py_clob_client.client.get") as mock_get:
            mock_get.return_value = {"minimum_tick_size": "0.001"}
            mock_client.get_tick_size(token_id)

        # Without a timestamp the cache misses and HTTP is called
        mock_get.assert_called_once()

    def test_expired_timestamp_triggers_refresh(self, mock_client):
        """When the timestamp is older than the TTL, an HTTP call is made."""
        token_id = "tok_abc"
        ts_str = "0.001"

        mock_client._ClobClient__tick_sizes[token_id] = ts_str
        # Set timestamp 400s in the past (TTL is 300s)
        mock_client._ClobClient__tick_size_timestamps[token_id] = (
            time.monotonic() - 400
        )

        with patch("py_clob_client.client.get") as mock_get:
            mock_get.return_value = {"minimum_tick_size": "0.001"}
            mock_client.get_tick_size(token_id)

        mock_get.assert_called_once()

    def test_fresh_timestamp_skips_http(self, mock_client):
        """A recent timestamp within TTL must skip the HTTP call."""
        token_id = "tok_abc"
        ts_str = "0.01"

        mock_client._ClobClient__tick_sizes[token_id] = ts_str
        mock_client._ClobClient__tick_size_timestamps[token_id] = time.monotonic()

        with patch("py_clob_client.client.get") as mock_get:
            result = mock_client.get_tick_size(token_id)

        assert result == ts_str
        mock_get.assert_not_called()


class TestPlaceLimitOrderCacheIntegration:
    """End-to-end test that place_limit_order sets both cache fields."""

    def test_place_limit_order_sets_timestamp(self, mock_client):
        """place_limit_order must populate __tick_size_timestamps so
        subsequent calls to create_order don't trigger HTTP lookups."""
        from src.clob_client import place_limit_order

        token_id = "tok_abc"

        with (
            patch("src.clob_client.create_clob_client", return_value=mock_client),
            patch.object(mock_client, "create_order") as mock_create,
            patch.object(mock_client, "post_order") as mock_post,
        ):
            mock_create.return_value = MagicMock()
            mock_post.return_value = {
                "success": True,
                "orderId": "order_123",
                "status": "live",
            }

            place_limit_order(
                token_id=token_id,
                price=0.999,
                size=40.0,
                side="BUY",
                tick_size=0.001,
            )

        # The fix: both cache fields should be populated
        assert token_id in mock_client._ClobClient__tick_sizes
        assert mock_client._ClobClient__tick_sizes[token_id] == "0.001"

        assert token_id in mock_client._ClobClient__tick_size_timestamps
        ts = mock_client._ClobClient__tick_size_timestamps[token_id]
        assert time.monotonic() - ts < 2.0, "Timestamp should be recent"

    def test_place_limit_order_without_tick_size_skips_cache_write(self, mock_client):
        """When tick_size is None, place_limit_order should not write to
        the cache (the library will resolve it via its own mechanism)."""
        from src.clob_client import place_limit_order

        token_id = "tok_abc"

        with (
            patch("src.clob_client.create_clob_client", return_value=mock_client),
            patch.object(mock_client, "create_order") as mock_create,
            patch.object(mock_client, "post_order") as mock_post,
        ):
            mock_create.return_value = MagicMock()
            mock_post.return_value = {
                "success": True,
                "orderId": "order_456",
                "status": "live",
            }

            place_limit_order(
                token_id=token_id,
                price=0.50,
                size=10.0,
                side="BUY",
                tick_size=None,
            )

        assert token_id not in mock_client._ClobClient__tick_sizes
        assert token_id not in mock_client._ClobClient__tick_size_timestamps
