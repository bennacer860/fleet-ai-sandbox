from src.markets.discovery import discover_slugs


def test_discover_slugs_filters_binary_and_dedups(monkeypatch):
    rows = [
        {
            "slug": "weather-nyc-temp-april-1-2026",
            "clobTokenIds": '["yes_token","no_token"]',
            "outcomes": '["Yes","No"]',
        },
        {
            # duplicate slug should be deduped
            "slug": "weather-nyc-temp-april-1-2026",
            "clobTokenIds": '["yes_token","no_token"]',
            "outcomes": '["Yes","No"]',
        },
        {
            # non-binary market should be filtered out
            "slug": "weather-three-way-example",
            "clobTokenIds": '["a","b","c"]',
            "outcomes": '["A","B","C"]',
        },
    ]

    monkeypatch.setattr(
        "src.markets.discovery.discover_markets_by_category",
        lambda *args, **kwargs: rows,
    )

    slugs = discover_slugs("weather/temperature")

    assert slugs == ["weather-nyc-temp-april-1-2026"]


def test_discover_slugs_applies_lead_time_window(monkeypatch):
    rows = [
        {
            "slug": "weather-within-30m",
            "clobTokenIds": '["yes","no"]',
            "outcomes": '["Yes","No"]',
            "endDate": "2026-04-01T10:25:00Z",
        },
        {
            "slug": "weather-too-early",
            "clobTokenIds": '["yes","no"]',
            "outcomes": '["Yes","No"]',
            "endDate": "2026-04-01T12:00:00Z",
        },
        {
            "slug": "weather-no-end-date",
            "clobTokenIds": '["yes","no"]',
            "outcomes": '["Yes","No"]',
        },
    ]
    monkeypatch.setattr(
        "src.markets.discovery.discover_markets_by_category",
        lambda *args, **kwargs: rows,
    )
    # 2026-04-01T10:00:00Z
    monkeypatch.setattr("src.markets.discovery.time.time", lambda: 1_775_037_600.0)

    slugs = discover_slugs("weather/temperature", lead_time_seconds=30 * 60)

    assert slugs == ["weather-within-30m"]


def test_discover_slugs_falls_back_to_events_when_markets_empty(monkeypatch):
    event_rows = [
        {
            "slug": "highest-temperature-in-seoul-on-april-2-2026",
            "ticker": "highest-temperature-in-seoul-on-april-2-2026-market",
            "endDate": "2026-04-02T12:00:00Z",
            "markets": [
                {
                    "clobTokenIds": '["yes","no"]',
                    "outcomes": '["Yes","No"]',
                }
            ],
        }
    ]
    monkeypatch.setattr(
        "src.markets.discovery.discover_markets_by_category",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "src.markets.discovery.discover_events_by_category",
        lambda *args, **kwargs: event_rows,
    )

    slugs = discover_slugs("weather/temperature", lead_time_seconds=None)

    assert slugs == ["highest-temperature-in-seoul-on-april-2-2026"]


def test_discover_slugs_prefers_event_slug_over_market_slug(monkeypatch):
    rows = [
        {
            "slug": "market-level-slug",
            "event": {"slug": "event-level-slug"},
            "clobTokenIds": '["yes","no"]',
            "outcomes": '["Yes","No"]',
        }
    ]
    monkeypatch.setattr(
        "src.markets.discovery.discover_markets_by_category",
        lambda *args, **kwargs: rows,
    )

    slugs = discover_slugs("weather/temperature", lead_time_seconds=None)

    assert slugs == ["event-level-slug"]
