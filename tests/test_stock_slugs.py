"""Tests for stock/ETF daily slug generation and parsing."""

import pytest
from datetime import date

from src.markets.stocks import (
    generate_stock_slugs_for_date,
    is_stock_slug,
    parse_stock_slug_end_ts,
    extract_ticker_from_stock_slug,
)
from src.markets.fifteen_min import extract_market_end_ts, detect_duration_from_slug


class TestGenerateStockSlugs:
    def test_generates_two_slugs(self):
        slugs = generate_stock_slugs_for_date("SPX", date(2026, 4, 8))
        assert len(slugs) == 2
        assert slugs[0] == "spx-opens-up-or-down-on-april-8-2026"
        assert slugs[1] == "spx-up-or-down-on-april-8-2026"

    def test_ticker_lowercased(self):
        slugs = generate_stock_slugs_for_date("TSLA", date(2026, 4, 8))
        assert slugs[0].startswith("tsla-")
        assert slugs[1].startswith("tsla-")

    def test_various_tickers(self):
        for ticker in ("AAPL", "MSFT", "NVDA", "NFLX", "QQQ"):
            slugs = generate_stock_slugs_for_date(ticker, date(2026, 4, 8))
            assert len(slugs) == 2
            assert ticker.lower() in slugs[0]

    def test_skips_weekends(self):
        saturday = date(2026, 4, 11)
        sunday = date(2026, 4, 12)
        assert saturday.weekday() == 5
        assert sunday.weekday() == 6
        assert generate_stock_slugs_for_date("SPX", saturday) == []
        assert generate_stock_slugs_for_date("SPX", sunday) == []

    def test_weekday_returns_slugs(self):
        monday = date(2026, 4, 13)
        assert monday.weekday() == 0
        assert len(generate_stock_slugs_for_date("SPX", monday)) == 2


class TestIsStockSlug:
    def test_stock_opens_slug(self):
        assert is_stock_slug("spx-opens-up-or-down-on-april-8-2026") is True

    def test_stock_close_slug(self):
        assert is_stock_slug("tsla-up-or-down-on-april-8-2026") is True

    def test_crypto_daily_slug_not_stock(self):
        assert is_stock_slug("bitcoin-up-or-down-on-march31-2026") is False

    def test_crypto_hourly_slug_not_stock(self):
        assert is_stock_slug("bitcoin-up-or-down-march-9-2026-10pm-et") is False

    def test_crypto_short_slug_not_stock(self):
        assert is_stock_slug("btc-updown-5m-1707523200") is False


class TestParseStockSlugEndTs:
    def test_opens_slug_returns_930am_est(self):
        ts = parse_stock_slug_end_ts("spx-opens-up-or-down-on-april-8-2026")
        assert ts is not None
        import pytz
        from datetime import datetime
        dt = datetime.fromtimestamp(ts, tz=pytz.timezone("US/Eastern"))
        assert dt.hour == 9
        assert dt.minute == 30
        assert dt.day == 8
        assert dt.month == 4
        assert dt.year == 2026

    def test_close_slug_returns_4pm_est(self):
        ts = parse_stock_slug_end_ts("tsla-up-or-down-on-april-8-2026")
        assert ts is not None
        import pytz
        from datetime import datetime
        dt = datetime.fromtimestamp(ts, tz=pytz.timezone("US/Eastern"))
        assert dt.hour == 16
        assert dt.minute == 0

    def test_returns_none_for_garbage(self):
        assert parse_stock_slug_end_ts("garbage-slug") is None

    def test_returns_none_for_crypto_slug(self):
        assert parse_stock_slug_end_ts("bitcoin-up-or-down-on-march31-2026") is None


class TestExtractTicker:
    def test_opens_slug(self):
        assert extract_ticker_from_stock_slug("spx-opens-up-or-down-on-april-8-2026") == "SPX"

    def test_close_slug(self):
        assert extract_ticker_from_stock_slug("nvda-up-or-down-on-april-8-2026") == "NVDA"

    def test_returns_none_for_non_stock(self):
        assert extract_ticker_from_stock_slug("btc-updown-5m-123") is None


class TestIntegrationWithFifteenMin:
    """Verify stock slugs flow through extract_market_end_ts correctly."""

    def test_extract_end_ts_opens(self):
        ts = extract_market_end_ts("spx-opens-up-or-down-on-april-8-2026")
        assert ts is not None
        import pytz
        from datetime import datetime
        dt = datetime.fromtimestamp(ts, tz=pytz.timezone("US/Eastern"))
        assert dt.hour == 9 and dt.minute == 30

    def test_extract_end_ts_close(self):
        ts = extract_market_end_ts("aapl-up-or-down-on-april-8-2026")
        assert ts is not None
        import pytz
        from datetime import datetime
        dt = datetime.fromtimestamp(ts, tz=pytz.timezone("US/Eastern"))
        assert dt.hour == 16 and dt.minute == 0

    def test_detect_duration_stock_opens(self):
        assert detect_duration_from_slug("spx-opens-up-or-down-on-april-8-2026") == 1440

    def test_detect_duration_stock_close(self):
        assert detect_duration_from_slug("tsla-up-or-down-on-april-8-2026") == 1440

    def test_crypto_daily_still_works(self):
        ts = extract_market_end_ts("bitcoin-up-or-down-on-march31-2026")
        assert ts is not None
