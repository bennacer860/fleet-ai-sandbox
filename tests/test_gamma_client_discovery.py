from src.gamma_client import discover_markets_by_category


def test_discover_markets_falls_back_when_server_filter_is_ignored(monkeypatch):
    related = {
        "slug": "weather-temp-nyc-daily",
        "category": "weather/temperature",
        "active": True,
    }
    unrelated = {
        "slug": "gta-vi-before-july",
        "category": "entertainment/gaming",
        "active": True,
    }

    def fake_fetch(params, timeout=30.0):
        if params.get("offset", 0) > 0:
            return []
        if any(k in params for k in ("tag_slug", "category_slug", "category", "tag")):
            # Simulate Gamma ignoring filter params and returning unrelated rows.
            return [unrelated]
        return [unrelated, related]

    monkeypatch.setattr("src.gamma_client.fetch_markets_page", fake_fetch)

    rows = discover_markets_by_category("weather/temperature", max_pages=1)
    assert [r.get("slug") for r in rows] == ["weather-temp-nyc-daily"]


def test_discover_markets_requires_full_category_path_not_partial(monkeypatch):
    rows = [
        {"slug": "generic-temp-market", "category": "temperature", "active": True},
        {"slug": "weather-temp-market", "category": "weather/temperature", "active": True},
    ]
    monkeypatch.setattr("src.gamma_client.fetch_markets_page", lambda params, timeout=30.0: rows)

    found = discover_markets_by_category("weather/temperature", max_pages=1)
    assert [r.get("slug") for r in found] == ["weather-temp-market"]
