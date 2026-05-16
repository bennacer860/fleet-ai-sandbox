from __future__ import annotations

from datetime import datetime, timezone

from src.markets import temperature


def test_parse_city_temp_slug() -> None:
    parsed = temperature.parse_city_temp_slug("highest-temperature-in-nyc-on-may-16-2026")
    assert parsed == ("highest", "nyc")
    assert temperature.parse_city_temp_slug("may-2026-temperature-increase-c") is None


def test_compute_safe_expiry_ts_uses_local_day_boundary() -> None:
    # NYC local midnight start for May 16, 2026 -> 04:00 UTC.
    game_start_ts = int(datetime(2026, 5, 16, 4, 0, tzinfo=timezone.utc).timestamp())
    safe_ts = temperature.compute_safe_expiry_ts(
        game_start_ts=game_start_ts,
        city_timezone="America/New_York",
        buffer_hours=0.0,
    )
    assert safe_ts == int(datetime(2026, 5, 17, 4, 0, tzinfo=timezone.utc).timestamp())


def test_discovery_filters_daily_city_events(monkeypatch) -> None:
    now_ts = int(datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc).timestamp())
    fake_events = [
        {
            "slug": "highest-temperature-in-nyc-on-may-16-2026",
            "endDate": "2026-05-16T12:00:00Z",
            "resolutionSource": "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA",
            "markets": [
                {"gameStartTime": "2026-05-16 04:00:00+00", "resolutionSource": None}
            ],
        },
        {
            "slug": "may-2026-temperature-increase-c",
            "endDate": "2026-06-10T00:00:00Z",
            "markets": [{"gameStartTime": "2026-05-01 00:00:00+00"}],
        },
    ]

    monkeypatch.setattr(temperature, "fetch_events", lambda **_: fake_events)

    discovered = temperature.discover_daily_city_temperature_events(
        cities={"nyc"},
        temperature_kind="both",
        horizon_hours=48,
        now_ts=now_ts,
    )
    assert len(discovered) == 1
    item = discovered[0]
    assert item.slug == "highest-temperature-in-nyc-on-may-16-2026"
    assert item.city == "nyc"
    assert item.kind == "highest"
    assert item.weather_station == "KLGA"
    assert item.city_timezone == "America/New_York"
    # Safe expiry should be local day end in NYC, not the gamma endDate.
    assert item.safe_expiry_ts == int(datetime(2026, 5, 17, 4, 0, tzinfo=timezone.utc).timestamp())
