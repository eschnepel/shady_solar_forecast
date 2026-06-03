"""math_utils.py – Re-exports shadylib helpers + HA-specific parse_dt wrapper.

Everything pure-Python is delegated to shadylib.
parse_dt is overridden here to use HA's UTC constant for consistency.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util

# Re-export everything from shadylib
from shadylib.math_utils import (
    r,
    r6,
    snap,
    aggregate_to_hours,
    wls2,
    wls2_origin_quad,
    BUCKET_MIN,
    PRECISION,
)

__all__ = [
    "r",
    "r6",
    "snap",
    "parse_dt",
    "aggregate_to_hours",
    "wls2",
    "wls2_origin_quad",
    "BUCKET_MIN",
    "PRECISION",
]


def parse_dt(iso_str: str) -> datetime:
    """Parse an ISO-8601 string. Uses HA's UTC for the fallback sentinel."""
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return datetime.min.replace(tzinfo=dt_util.UTC)
