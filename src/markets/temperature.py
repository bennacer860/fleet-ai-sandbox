"""Discovery and time helpers for daily city temperature markets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from ..gamma_client import fetch_events
from ..logging_config import get_logger

logger = get_logger(__name__)

_CITY_TEMP_SLUG_RE = re.compile(
    r"^(highest|lowest)-temperature-in-([a-z0-9-]+)-on-([a-z]+-\d{1,2}-\d{4})$"
)

_CITY_TIMEZONE_MAP: dict[str, str] = {
    "nyc": "America/New_York",
    "new-york-city": "America/New_York",
    "miami": "America/New_York",
    "los-angeles": "America/Los_Angeles",
    "san-francisco": "America/Los_Angeles",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "madrid": "Europe/Madrid",
    "milan": "Europe/Rome",
    "amsterdam": "Europe/Amsterdam",
    "warsaw": "Europe/Warsaw",
    "tokyo": "Asia/Tokyo",
    "seoul": "Asia/Seoul",
    "shanghai": "Asia/Shanghai",
    "hong-kong": "Asia/Hong_Kong",
    "lagos": "Africa/Lagos",
    "cape-town": "Africa/Johannesburg",
}


@dataclass(frozen=True, slots=True)
class CityTemperatureEvent:
    """Minimal metadata for a discoverable city temperature event."""

    slug: str
    city: str
    kind: str
    gamma_end_ts: int
    game_start_ts: int | None
    city_timezone: str
    safe_expiry_ts: int | None
    resolution_source: str | None
    weather_station: str | None


def parse_iso_ts(raw: str | None) -> int | None:
    """Parse common Gamma ISO timestamps into UTC epoch seconds."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp())


def parse_game_start_ts(raw: str | None) -> int | None:
    """Parse ``gameStartTime`` values like ``2026-05-16 04:00:00+00``."""
    if not raw:
        return None
    normalized = raw.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return int(dt.timestamp())


def parse_city_temp_slug(slug: str) -> tuple[str, str] | None:
    """Extract ``(kind, city)`` from daily city temperature slugs."""
    m = _CITY_TEMP_SLUG_RE.match(slug)
    if not m:
        return None
    kind, city, _ = m.groups()
    return kind, city


def infer_city_timezone(city_slug: str) -> str:
    """Return timezone for known city slugs, defaulting to UTC."""
    return _CITY_TIMEZONE_MAP.get(city_slug, "UTC")


def extract_weather_station(resolution_source: str | None) -> str | None:
    """Extract station code from Weather Underground style URLs."""
    if not resolution_source:
        return None
    # Most WU urls end in station code, e.g. /KLGA or /LFPB.
    tail = resolution_source.rstrip("/").split("/")[-1]
    if not tail:
        return None
    if re.fullmatch(r"[A-Z0-9]{3,5}", tail):
        return tail
    return None


def compute_safe_expiry_ts(
    *,
    game_start_ts: int | None,
    city_timezone: str,
    buffer_hours: float = 0.0,
) -> int | None:
    """Compute conservative expiry: local day end + buffer."""
    if game_start_ts is None:
        return None
    tz = ZoneInfo(city_timezone)
    start_local = datetime.fromtimestamp(game_start_ts, tz=timezone.utc).astimezone(tz)
    day_end_local = (start_local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    safe_local = day_end_local + timedelta(hours=buffer_hours)
    return int(safe_local.timestamp())


def _is_daily_city_temperature_slug(slug: str) -> bool:
    return _CITY_TEMP_SLUG_RE.match(slug) is not None


def discover_daily_city_temperature_events(
    *,
    cities: set[str] | None = None,
    temperature_kind: str = "both",
    horizon_hours: int = 48,
    now_ts: int | None = None,
    safe_expiry_buffer_hours: float = 0.0,
) -> list[CityTemperatureEvent]:
    """Discover active daily city temperature events from Gamma."""
    if temperature_kind not in {"highest", "lowest", "both"}:
        raise ValueError("temperature_kind must be one of: highest, lowest, both")

    now = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    horizon_ts = now + int(horizon_hours * 3600)
    wanted_cities = {c.lower().replace("_", "-") for c in (cities or set())}

    discovered: list[CityTemperatureEvent] = []
    seen: set[str] = set()

    for event in fetch_events(active=True, closed=False, limit=100):
        slug = str(event.get("slug") or "")
        parsed = parse_city_temp_slug(slug)
        if parsed is None:
            continue
        if slug in seen:
            continue

        kind, city = parsed
        if wanted_cities and city not in wanted_cities:
            continue
        if temperature_kind != "both" and kind != temperature_kind:
            continue

        gamma_end_ts = parse_iso_ts(event.get("endDate"))
        if gamma_end_ts is None:
            continue
        if gamma_end_ts < now or gamma_end_ts > horizon_ts:
            continue

        markets = event.get("markets") or []
        market = markets[0] if markets else {}
        game_start_ts = parse_game_start_ts(market.get("gameStartTime"))
        city_timezone = infer_city_timezone(city)
        safe_expiry_ts = compute_safe_expiry_ts(
            game_start_ts=game_start_ts,
            city_timezone=city_timezone,
            buffer_hours=safe_expiry_buffer_hours,
        )
        resolution_source = (
            market.get("resolutionSource")
            or event.get("resolutionSource")
            or None
        )
        discovered.append(
            CityTemperatureEvent(
                slug=slug,
                city=city,
                kind=kind,
                gamma_end_ts=gamma_end_ts,
                game_start_ts=game_start_ts,
                city_timezone=city_timezone,
                safe_expiry_ts=safe_expiry_ts,
                resolution_source=resolution_source,
                weather_station=extract_weather_station(resolution_source),
            )
        )
        seen.add(slug)

    logger.info(
        "[TEMP_DISCOVERY] cities=%s kind=%s horizon=%dh -> %d events",
        sorted(wanted_cities) if wanted_cities else "ALL",
        temperature_kind,
        horizon_hours,
        len(discovered),
    )
    return discovered
