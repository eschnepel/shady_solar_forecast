"""math_utils.py – Re-exports shadylib helpers + HA-specific parse_dt wrapper.

Everything pure-Python is delegated to shadylib.
parse_dt is overridden here to use HA's UTC constant for consistency.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    "normalise_to_5min_day",
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


def normalise_to_5min_day(
    slots: dict[str, float],
    day_start: datetime,
) -> dict[str, float]:
    """Return a complete 288-slot dict for *day_start*'s calendar day.

    All timestamps in *slots* that fall on that day are snapped to the
    nearest 5-minute boundary (floor) and accumulated.  Every slot for
    the full 24 hours is present in the output; slots with no data are
    set to 0.0.

    This normalises away sub-5-min timestamps (e.g. 21:12:46) that some
    providers emit, and fills night-time gaps so consumers always receive
    a complete, uniform series.
    """
    day_end = day_start + timedelta(days=1)
    tz = day_start.tzinfo or timezone.utc

    # Build a zero-filled skeleton for the entire day.
    result: dict[str, float] = {}
    t = day_start
    while t < day_end:
        result[t.isoformat()] = 0.0
        t += timedelta(minutes=BUCKET_MIN)

    # Accumulate incoming values into the correct 5-min bucket.
    for ts, wh in slots.items():
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        # Normalise timezone for comparison.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(tz)

        if not (day_start <= dt < day_end):
            continue

        snapped = dt.replace(
            minute=(dt.minute // BUCKET_MIN) * BUCKET_MIN,
            second=0,
            microsecond=0,
        )
        key = snapped.isoformat()
        if key in result:
            result[key] = round(result[key] + wh, 2)

    return result
