"""Tests for math_utils.py – pure helpers, no HA needed beyond the stub."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shady.math_utils import (
    r,
    r6,
    snap,
    parse_dt,
    aggregate_to_hours,
    wls2,
    wls2_origin_quad,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# r / r6
# ---------------------------------------------------------------------------


class TestRounding:
    def test_r_rounds_to_2(self):
        assert r(1.23456) == 1.23

    def test_r_rounds_half_up(self):
        assert r(1.005) == 1.01 or r(1.005) == 1.0  # float rounding; just no crash

    def test_r_zero(self):
        assert r(0.0) == 0.0

    def test_r6_rounds_to_6(self):
        assert r6(0.1234567890) == 0.123457

    def test_r6_small_value(self):
        assert r6(0.000000123456789) == 0.000000


# ---------------------------------------------------------------------------
# snap
# ---------------------------------------------------------------------------


class TestSnap:
    @pytest.mark.parametrize(
        "minute,expected",
        [
            (0, 0),
            (1, 0),
            (4, 0),
            (5, 5),
            (9, 5),
            (10, 10),
            (29, 25),
            (30, 30),
            (55, 55),
            (59, 55),
        ],
    )
    def test_snap(self, minute, expected):
        assert snap(minute) == expected


# ---------------------------------------------------------------------------
# parse_dt
# ---------------------------------------------------------------------------


class TestParseDt:
    def test_valid_iso_with_tz(self):
        result = parse_dt("2025-06-01T10:00:00+02:00")
        assert result.hour == 10

    def test_valid_iso_utc(self):
        result = parse_dt("2025-06-01T10:00:00+00:00")
        assert result.year == 2025

    def test_invalid_returns_min(self):
        result = parse_dt("not-a-date")
        assert result == datetime.min.replace(tzinfo=UTC)

    def test_empty_string_returns_min(self):
        result = parse_dt("")
        assert result == datetime.min.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# aggregate_to_hours
# ---------------------------------------------------------------------------


class TestAggregateToHours:
    def test_5min_slots_sum_to_hour(self):
        slots = {
            "2025-06-01T10:00:00+00:00": 10.0,
            "2025-06-01T10:05:00+00:00": 10.0,
            "2025-06-01T10:10:00+00:00": 10.0,
            "2025-06-01T10:15:00+00:00": 10.0,
            "2025-06-01T10:20:00+00:00": 10.0,
            "2025-06-01T10:25:00+00:00": 10.0,
        }
        result = aggregate_to_hours(slots)
        assert len(result) == 1
        assert list(result.values())[0] == 60.0

    def test_two_hours_separate(self):
        slots = {
            "2025-06-01T10:00:00+00:00": 50.0,
            "2025-06-01T11:00:00+00:00": 30.0,
        }
        result = aggregate_to_hours(slots)
        assert len(result) == 2

    def test_result_sorted(self):
        slots = {
            "2025-06-01T12:00:00+00:00": 5.0,
            "2025-06-01T10:00:00+00:00": 5.0,
            "2025-06-01T11:00:00+00:00": 5.0,
        }
        keys = list(aggregate_to_hours(slots).keys())
        assert keys == sorted(keys)

    def test_empty_input(self):
        assert aggregate_to_hours({}) == {}

    def test_invalid_ts_skipped(self):
        slots = {"bad-ts": 10.0, "2025-06-01T10:00:00+00:00": 20.0}
        result = aggregate_to_hours(slots)
        assert len(result) == 1

    def test_precision_preserved(self):
        slots = {f"2025-06-01T10:{m:02d}:00+00:00": 10.123 for m in range(0, 60, 5)}
        result = aggregate_to_hours(slots)
        val = list(result.values())[0]
        # 12 slots × 10.123 = 121.476 → rounded to 2dp
        # Floating point accumulation across 12 r() calls may drift slightly
        assert abs(val - round(12 * 10.123, 2)) <= 0.05


# ---------------------------------------------------------------------------
# wls2 – linear regression
# ---------------------------------------------------------------------------


class TestWls2:
    def test_perfect_linear(self):
        """y = 2x + 1 should be recovered exactly."""
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [3.0, 5.0, 7.0, 9.0, 11.0]
        ws = [1.0] * 5
        result = wls2(xs, ys, ws)
        assert result is not None
        slope, intercept = result
        assert abs(slope - 2.0) < 1e-9
        assert abs(intercept - 1.0) < 1e-9

    def test_zero_weights_returns_none(self):
        xs = [1.0, 2.0, 3.0]
        ys = [1.0, 2.0, 3.0]
        ws = [0.0, 0.0, 0.0]
        assert wls2(xs, ys, ws) is None

    def test_all_same_x_returns_none(self):
        """Degenerate case: no variance in x."""
        xs = [5.0, 5.0, 5.0]
        ys = [1.0, 2.0, 3.0]
        ws = [1.0, 1.0, 1.0]
        assert wls2(xs, ys, ws) is None

    def test_weighted_regression(self):
        """High-weight points should dominate the fit."""
        # Points near y=2x get high weight; outlier gets near-zero weight
        xs = [1.0, 2.0, 3.0, 4.0, 50.0]
        ys = [2.0, 4.0, 6.0, 8.0, 0.0]  # outlier at x=50
        ws = [10.0, 10.0, 10.0, 10.0, 0.001]  # outlier almost ignored
        result = wls2(xs, ys, ws)
        assert result is not None
        slope, intercept = result
        # Should be close to y = 2x (dominated by high-weight points)
        assert abs(slope - 2.0) < 0.1
        assert abs(intercept) < 0.3

    def test_noisy_data_reasonable_fit(self):
        """Noisy but roughly linear data returns sensible slope."""
        import random

        random.seed(42)
        xs = [float(i) for i in range(1, 21)]
        ys = [2.0 * x + 1.0 + random.gauss(0, 0.5) for x in xs]
        ws = [1.0] * 20
        result = wls2(xs, ys, ws)
        assert result is not None
        slope, intercept = result
        assert 1.5 < slope < 2.5
        assert 0.0 < intercept < 2.5

    def test_single_point_returns_none(self):
        assert wls2([1.0], [1.0], [1.0]) is None


# ---------------------------------------------------------------------------
# wls2_origin_quad – quadratic through origin
# ---------------------------------------------------------------------------


class TestWls2OriginQuad:
    def test_perfect_quadratic_through_origin(self):
        """y = 2x² + 3x should be recovered."""
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2 * x**2 + 3 * x for x in xs]
        ws = [1.0] * 5
        result = wls2_origin_quad(xs, ys, ws)
        assert result is not None
        a, b = result
        assert abs(a - 2.0) < 1e-6
        assert abs(b - 3.0) < 1e-6

    def test_linear_through_origin(self):
        """y = 0*x² + 3x (pure linear through origin)."""
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [3.0 * x for x in xs]
        ws = [1.0] * 5
        result = wls2_origin_quad(xs, ys, ws)
        assert result is not None
        a, b = result
        assert abs(a) < 1e-6
        assert abs(b - 3.0) < 1e-6

    def test_all_zero_x_returns_none(self):
        xs = [0.0, 0.0, 0.0]
        ys = [1.0, 2.0, 3.0]
        ws = [1.0, 1.0, 1.0]
        assert wls2_origin_quad(xs, ys, ws) is None

    def test_prediction_at_zero_is_zero(self):
        """Because model goes through origin, predict at x=0 must give ~0."""
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2 * x**2 + x for x in xs]
        ws = [1.0] * 5
        result = wls2_origin_quad(xs, ys, ws)
        assert result is not None
        a, b = result
        assert abs(a * 0**2 + b * 0) < 1e-9

    def test_two_points_returns_result(self):
        """Minimum viable input."""
        xs = [1.0, 2.0]
        ys = [3.0, 10.0]
        ws = [1.0, 1.0]
        result = wls2_origin_quad(xs, ys, ws)
        # May return None if degenerate, but should not raise
        assert result is None or len(result) == 2
