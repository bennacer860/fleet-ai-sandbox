"""Generic parsing helpers used across the codebase.

Centralises the JSON-list/pipe-separated/comma-separated parsing that
appears in ``gamma_client``, ``resolve_trades``, and other modules.
"""

import json
from typing import Any


def parse_json_list(raw: Any) -> list[str]:
    """Parse a value that can be a JSON string, Python list, or delimited string.

    Handles the three common shapes returned by the Polymarket Gamma API::

        '["a","b"]'      -> ["a", "b"]   # JSON string
        ["a", "b"]       -> ["a", "b"]   # already a list
        "a|b"            -> ["a", "b"]   # pipe-separated
        "a, b"           -> ["a", "b"]   # comma-separated

    Args:
        raw: The raw value to parse.

    Returns:
        A list of strings (empty if *raw* is ``None``).
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (json.JSONDecodeError, ValueError):
            pass
        # Fallback: pipe or comma separated
        sep = "|" if "|" in raw else ","
        return [x.strip() for x in raw.split(sep) if x.strip()]
    return []


def parse_float_list(raw: Any) -> list[float]:
    """Like ``parse_json_list`` but converts every element to *float*.

    Args:
        raw: A JSON string, list, or comma-separated string of numbers.

    Returns:
        A list of floats (empty on failure).
    """
    str_items = parse_json_list(raw)
    try:
        return [float(x) for x in str_items]
    except (ValueError, TypeError):
        return []
