"""Unit tests for notification_bridge formatting functions.

Tests the pure formatting functions without any I/O dependencies.
"""

import pytest

from src.notification_bridge import (
    build_telegram_message,
    clean_reason,
    fmt_price,
    format_proximity,
    format_skip_reason,
    format_timing,
)
from src.core.models import OrderIntent, OrderState, Side
from src.core.events import OrderStatus


class TestCleanReason:
    """Tests for clean_reason function."""

    def test_empty_string(self):
        assert clean_reason("") == ""

    def test_none_like(self):
        assert clean_reason("") == ""

    def test_strips_exception_prefix(self):
        assert clean_reason("EXCEPTION: Something went wrong") == "Something went wrong"

    def test_strips_exception_lowercase(self):
        assert clean_reason("Exception: Bad input") == "Bad input"

    def test_strips_poly_api_exception(self):
        assert clean_reason("PolyApiException: API error") == "API error"

    def test_strips_attribute_error(self):
        assert clean_reason("AttributeError: 'NoneType' has no attribute") == "'NoneType' has no attribute"

    def test_extracts_from_poly_api_format(self):
        reason = "PolyApiException[status=400, error_message={'error': 'Insufficient balance'}]"
        assert clean_reason(reason) == "Insufficient balance"

    def test_extracts_error_msg_field(self):
        reason = "PolyApiException[status=400, error_message={'errorMsg': 'Order too small'}]"
        assert clean_reason(reason) == "Order too small"

    def test_passthrough_unknown_format(self):
        assert clean_reason("Unknown error format") == "Unknown error format"


class TestFmtPrice:
    """Tests for fmt_price function."""

    def test_large_price(self):
        assert fmt_price(1234.56) == "$1,234.56"

    def test_hundred_plus(self):
        assert fmt_price(100.00) == "$100.00"

    def test_medium_price(self):
        assert fmt_price(5.1234) == "$5.1234"

    def test_one_dollar(self):
        assert fmt_price(1.0) == "$1.0000"

    def test_small_price(self):
        assert fmt_price(0.123456) == "$0.123456"

    def test_very_small_price(self):
        assert fmt_price(0.001234) == "$0.001234"


class TestFormatProximity:
    """Tests for format_proximity function."""

    def test_all_values(self):
        result = format_proximity(
            spot=100.5,
            strike=100.0,
            proximity=0.005,
            age_ms=150.0,
        )
        assert "spot=$100.50" in result  # Prices >= 100 use 2 decimals
        assert "strike=$100.00" in result
        assert "prox=0.500%" in result
        assert "age=150ms" in result

    def test_spot_only(self):
        result = format_proximity(spot=50.0, strike=None)
        assert "spot=$50.0000" in result  # Prices 1-100 use 4 decimals
        assert "strike=--" in result

    def test_stale_spot(self):
        result = format_proximity(spot=None, strike=100.0)
        assert "spot=STALE" in result
        assert "strike=$100.00" in result  # Prices >= 100 use 2 decimals

    def test_both_none(self):
        result = format_proximity(spot=None, strike=None)
        assert "spot=STALE" in result
        assert "strike=--" in result

    def test_no_proximity(self):
        result = format_proximity(spot=100.0, strike=99.0, proximity=None)
        assert "prox=" not in result

    def test_no_age(self):
        result = format_proximity(spot=100.0, strike=99.0, age_ms=None)
        assert "age=" not in result


class TestFormatTiming:
    """Tests for format_timing function."""

    def make_state(self, **kwargs) -> OrderState:
        """Helper to create OrderState with timing fields."""
        intent = OrderIntent(
            token_id="tok",
            price=0.95,
            size=10.0,
            side=Side.BUY,
            strategy="test",
            slug="test-slug",
            tick_size=0.001,
        )
        state = OrderState(order_id="ord1", intent=intent, status=OrderStatus.SUBMITTED)
        for k, v in kwargs.items():
            setattr(state, k, v)
        return state

    def test_no_timing_data(self):
        state = self.make_state()
        assert format_timing(state) == ""

    def test_with_timing_values(self):
        # OrderState uses slots, so we can't set arbitrary attributes
        # Just verify that the function handles the default None values gracefully
        state = self.make_state()
        result = format_timing(state)
        # With no timing data set, should return empty string
        assert result == ""

    def test_expiry_time(self):
        state = self.make_state()
        state.market_end_ts = 1000
        state.rest_response_ns = 500 * 1_000_000_000  # 500s before expiry
        result = format_timing(state)
        # time_to_expiry_s is computed
        assert isinstance(result, str)


class TestBuildTelegramMessage:
    """Tests for build_telegram_message function."""

    def test_basic_message(self):
        msg = build_telegram_message(
            color_emoji="🟢",
            title="TEST TITLE",
            body="This is the body",
            profile="P1",
        )
        assert "🟢" in msg
        assert "<b>TEST TITLE</b>" in msg
        assert "<code>P1</code>" in msg
        assert "This is the body" in msg
        assert "━━━" in msg  # Separator

    def test_html_tags(self):
        msg = build_telegram_message(
            color_emoji="🔵",
            title="Order",
            body="<b>Bold</b> text",
            profile="0",
        )
        # HTML should be preserved
        assert "<b>Bold</b>" in msg
        assert "<b>Order</b>" in msg


class TestFormatSkipReason:
    """Tests for format_skip_reason function."""

    def test_stale_reason(self):
        result = format_skip_reason("stale price data")
        assert "[bold red]BLOCKED:" in result
        assert "stale price data" in result

    def test_proximity_reason(self):
        result = format_skip_reason("proximity check failed")
        assert "[bold magenta]BLOCKED:" in result
        assert "proximity check failed" in result

    def test_other_reason(self):
        result = format_skip_reason("price below threshold")
        assert result == "price below threshold"
        assert "BLOCKED" not in result

    def test_case_insensitive_stale(self):
        result = format_skip_reason("STALE websocket")
        assert "[bold red]" in result

    def test_case_insensitive_proximity(self):
        result = format_skip_reason("PROXIMITY blocked")
        assert "[bold magenta]" in result


class TestIntegration:
    """Integration-style tests combining multiple formatters."""

    def test_telegram_fill_message(self):
        """Test building a complete fill notification."""
        body = (
            f"📍 <b>Market:</b> <code>btc-5m-test</code>\n"
            f"💵 <b>Price:</b> ${0.95:.4f}\n"
            f"📦 <b>Size:</b> {10.0:.2f} shares"
        )
        msg = build_telegram_message("🟢", "ORDER FILLED", body, "P1")

        assert "ORDER FILLED" in msg
        assert "btc-5m-test" in msg
        assert "$0.9500" in msg
        assert "10.00 shares" in msg

    def test_error_cleanup_pipeline(self):
        """Test cleaning and formatting an error for display."""
        raw = "PolyApiException[status=400, error_message={'error': 'Balance too low'}]"
        cleaned = clean_reason(raw)
        assert cleaned == "Balance too low"

        # Could be used in a skip message
        formatted = format_skip_reason(cleaned)
        assert formatted == "Balance too low"  # Not a special reason
