"""math_utils.py – Pure-Python helpers. No external dependencies.

Contains:
  - Output precision helpers (r, r6)
  - Datetime parsing (parse_dt)
  - 5-min bucket snapping (snap)
  - Hourly aggregation (aggregate_to_hours)
  - WLS solvers (wls2, wls2_origin_quad)
"""
from __future__ import annotations

from datetime import datetime, timezone

PRECISION  = 2   # decimal places for all Wh output values
BUCKET_MIN = 5   # minutes per bucket

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Precision
# ---------------------------------------------------------------------------

def r(v: float) -> float:
    """Round to standard output precision (2 decimal places)."""
    return round(v, PRECISION)


def r6(v: float) -> float:
    """Round model coefficients to 6 decimal places."""
    return round(v, 6)


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------

def parse_dt(iso_str: str) -> datetime:
    """Parse an ISO-8601 string to datetime. Returns datetime.min on failure."""
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return datetime.min.replace(tzinfo=_UTC)


def snap(minute: int) -> int:
    """Round a minute value down to the nearest 5-minute boundary."""
    return (minute // BUCKET_MIN) * BUCKET_MIN


def aggregate_to_hours(slots: dict[str, float]) -> dict[str, float]:
    """Sum sub-hourly slots into full-hour buckets.

    Key is the ISO string of the hour's start (minute=0, second=0).
    Invalid timestamps are silently skipped.
    """
    hourly: dict[str, float] = {}
    for ts, wh in slots.items():
        try:
            dt  = datetime.fromisoformat(ts)
            key = dt.replace(minute=0, second=0, microsecond=0).isoformat()
        except ValueError:
            continue
        hourly[key] = r(hourly.get(key, 0.0) + wh)
    return dict(sorted(hourly.items()))


# ---------------------------------------------------------------------------
# WLS solvers
# ---------------------------------------------------------------------------

def wls2(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple[float, float] | None:
    """Weighted least squares linear regression: y ~ slope*x + intercept.

    Returns (slope, intercept) or None if the system is degenerate
    (e.g. zero total weight, all-same x values).
    """
    sw   = sum(ws)
    if sw == 0:
        return None
    swx  = sum(w * x     for w, x    in zip(ws, xs))
    swy  = sum(w * y     for w, y    in zip(ws, ys))
    swxx = sum(w * x * x for w, x    in zip(ws, xs))
    swxy = sum(w * x * y for w, x, y in zip(ws, xs, ys))
    denom = sw * swxx - swx ** 2
    if abs(denom) < 1e-12:
        return None
    slope     = (sw * swxy - swx * swy) / denom
    intercept = (swy - slope * swx) / sw
    return slope, intercept


def wls2_origin_quad(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple[float, float] | None:
    """WLS quadratic through the origin: y ~ a*x² + b*x  (no free intercept).

    Fixing the intercept to zero is physically correct for solar correction
    (fc=0 → pv=0) and prevents the model from memorising the training mean
    as a constant offset.

    Solves the 2×2 normal equations:
      [Σw·x⁴  Σw·x³] [a]   [Σw·x²·y]
      [Σw·x³  Σw·x²] [b] = [Σw·x·y  ]

    Returns (a, b) or None if the system is degenerate.
    """
    swx2  = sum(w * x**2     for w, x    in zip(ws, xs))
    swx3  = sum(w * x**3     for w, x    in zip(ws, xs))
    swx4  = sum(w * x**4     for w, x    in zip(ws, xs))
    swxy  = sum(w * x * y    for w, x, y in zip(ws, xs, ys))
    swx2y = sum(w * x**2 * y for w, x, y in zip(ws, xs, ys))

    det = swx4 * swx2 - swx3 ** 2
    if abs(det) < 1e-12:
        return None

    a = (swx2y * swx2 - swxy  * swx3) / det
    b = (swxy  * swx4 - swx2y * swx3) / det
    return a, b
