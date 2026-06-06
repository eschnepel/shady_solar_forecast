"""Tests for normalise_to_5min_day in math_utils.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from shady.math_utils import normalise_to_5min_day as _normalise_to_5min_day

UTC = timezone.utc


def day(y=2025, m=6, d=2) -> datetime:
    return datetime(y, m, d, 0, 0, 0, tzinfo=UTC)


class TestNormaliseTo5MinDay:
    def test_always_returns_288_slots(self):
        result = _normalise_to_5min_day({}, day())
        assert len(result) == 288

    def test_empty_input_all_zeros(self):
        result = _normalise_to_5min_day({}, day())
        assert all(v == 0.0 for v in result.values())

    def test_slots_span_full_24h(self):
        result = _normalise_to_5min_day({}, day())
        keys = sorted(result.keys())
        assert keys[0] == "2025-06-02T00:00:00+00:00"
        assert keys[-1] == "2025-06-02T23:55:00+00:00"

    def test_exact_5min_timestamp_preserved(self):
        slots = {"2025-06-02T10:15:00+00:00": 42.0}
        result = _normalise_to_5min_day(slots, day())
        assert result["2025-06-02T10:15:00+00:00"] == 42.0

    def test_sub_5min_timestamp_snapped(self):
        """21:12:46 must snap to 21:10:00."""
        slots = {"2025-06-02T21:12:46+00:00": 30.0}
        result = _normalise_to_5min_day(slots, day())
        assert result["2025-06-02T21:10:00+00:00"] == 30.0
        assert result.get("2025-06-02T21:12:46+00:00") is None

    def test_sub_5min_accumulation(self):
        """Two timestamps snapping to the same bucket are summed."""
        slots = {
            "2025-06-02T10:01:00+00:00": 10.0,
            "2025-06-02T10:03:00+00:00": 5.0,
        }
        result = _normalise_to_5min_day(slots, day())
        assert abs(result["2025-06-02T10:00:00+00:00"] - 15.0) < 0.01

    def test_out_of_day_slots_ignored(self):
        slots = {
            "2025-06-01T23:55:00+00:00": 99.0,  # yesterday
            "2025-06-03T00:00:00+00:00": 99.0,  # day after tomorrow
            "2025-06-02T12:00:00+00:00": 50.0,  # today
        }
        result = _normalise_to_5min_day(slots, day())
        assert result["2025-06-02T12:00:00+00:00"] == 50.0
        total = sum(result.values())
        assert abs(total - 50.0) < 0.01

    def test_night_slots_are_zero(self):
        """Hours with no solar data must be present but zero."""
        slots = {"2025-06-02T12:00:00+00:00": 100.0}
        result = _normalise_to_5min_day(slots, day())
        assert result["2025-06-02T00:00:00+00:00"] == 0.0
        assert result["2025-06-02T23:55:00+00:00"] == 0.0

    def test_hourly_slot_placed_at_hour_boundary(self):
        """A raw hourly slot (minute=0) lands in the correct 5-min bucket."""
        slots = {"2025-06-02T14:00:00+00:00": 200.0}
        result = _normalise_to_5min_day(slots, day())
        assert result["2025-06-02T14:00:00+00:00"] == 200.0

    def test_keys_are_sorted(self):
        result = _normalise_to_5min_day({}, day())
        keys = list(result.keys())
        assert keys == sorted(keys)

    def test_consecutive_slots_differ_by_5min(self):
        result = _normalise_to_5min_day({}, day())
        keys = list(result.keys())
        for a, b in zip(keys, keys[1:]):
            delta = datetime.fromisoformat(b) - datetime.fromisoformat(a)
            assert delta == timedelta(minutes=5)
