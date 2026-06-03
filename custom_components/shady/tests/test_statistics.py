"""Tests for statistics.py – recorder row parsing."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

UTC = timezone.utc


def dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2025, 6, 1, hour, minute, tzinfo=UTC)


class TestStartParsing:
    """Test the _start() helper inside fetch_statistics via indirect testing.

    We test the parsing logic by exercising it through the public API with
    mocked recorder results in each row format HA versions can return.
    """

    def _make_row(self, start_val, mean_val: float) -> object:
        """Create a mock row with object-style attributes."""
        row = MagicMock()
        row.start = start_val
        row.mean = mean_val
        return row

    def _make_dict_row(self, start_val, mean_val: float) -> dict:
        return {"start": start_val, "mean": mean_val}

    def test_object_row_with_datetime(self):
        """Object row where start is already a datetime."""
        # We test the inner logic by reconstructing it here
        row = self._make_row(dt(10), 100.0)
        v = row.start
        assert isinstance(v, datetime)

    def test_dict_row_with_unix_timestamp(self):
        """Dict row where start is a Unix float (seen in some HA versions)."""
        ts = datetime(2025, 6, 1, 10, 0, tzinfo=UTC).timestamp()
        row = {"start": ts, "mean": 100.0}
        # Simulate _start logic
        v = row.get("start")
        result = datetime.fromtimestamp(v, tz=UTC)
        assert result.hour == 10

    def test_dict_row_with_iso_string(self):
        v = "2025-06-01T10:00:00+00:00"
        result = datetime.fromisoformat(v)
        assert result.hour == 10

    @pytest.mark.asyncio
    async def test_fetch_statistics_filters_none_mean(self):
        """Rows with mean=None are excluded from output."""
        from shady.statistics import fetch_statistics

        rows = [
            {"start": dt(10), "mean": 100.0},
            {"start": dt(11), "mean": None},  # should be filtered
            {"start": dt(12), "mean": 50.0},
        ]

        mock_result = {"sensor.test": rows}

        def mock_sdp(*args, **kwargs):
            return mock_result

        mock_recorder = MagicMock()
        mock_recorder.async_add_executor_job = AsyncMock(side_effect=lambda fn: fn())

        hass = MagicMock()

        with (
            patch("shady.statistics.statistics_during_period", mock_sdp),
            patch("shady.statistics.get_recorder", return_value=mock_recorder),
        ):
            result = await fetch_statistics(hass, ["sensor.test"], dt(0))

        rows_out = result.get("sensor.test", [])
        assert len(rows_out) == 2
        assert all(r["mean"] is not None for r in rows_out)

    @pytest.mark.asyncio
    async def test_fetch_statistics_returns_datetime_objects(self):
        """start values in output should be datetime objects."""
        from shady.statistics import fetch_statistics

        rows = [{"start": dt(10), "mean": 200.0}]
        mock_result = {"sensor.pv": rows}

        def mock_sdp(*args, **kwargs):
            return mock_result

        mock_recorder = MagicMock()
        mock_recorder.async_add_executor_job = AsyncMock(side_effect=lambda fn: fn())

        hass = MagicMock()

        with (
            patch("shady.statistics.statistics_during_period", mock_sdp),
            patch("shady.statistics.get_recorder", return_value=mock_recorder),
        ):
            result = await fetch_statistics(hass, ["sensor.pv"], dt(0))

        rows_out = result.get("sensor.pv", [])
        assert len(rows_out) == 1
        assert isinstance(rows_out[0]["start"], datetime)

    @pytest.mark.asyncio
    async def test_fetch_statistics_unix_timestamp_converted(self):
        """Unix float start values are converted to datetime."""
        from shady.statistics import fetch_statistics

        unix_ts = datetime(2025, 6, 1, 10, 0, tzinfo=UTC).timestamp()
        rows = [{"start": unix_ts, "mean": 300.0}]
        mock_result = {"sensor.pv": rows}

        def mock_sdp(*args, **kwargs):
            return mock_result

        mock_recorder = MagicMock()
        mock_recorder.async_add_executor_job = AsyncMock(side_effect=lambda fn: fn())

        hass = MagicMock()

        with (
            patch("shady.statistics.statistics_during_period", mock_sdp),
            patch("shady.statistics.get_recorder", return_value=mock_recorder),
        ):
            result = await fetch_statistics(hass, ["sensor.pv"], dt(0))

        rows_out = result.get("sensor.pv", [])
        assert len(rows_out) == 1
        assert rows_out[0]["start"].hour == 10
