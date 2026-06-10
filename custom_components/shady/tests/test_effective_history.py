"""Tests for effective_history.py – backfill and HA Storage cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

UTC = timezone.utc


def _dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2025, 6, day, hour, minute, tzinfo=UTC)


def _make_rows(
    start: datetime,
    count: int,
    mean: float,
) -> list[dict]:
    return [{"start": start + timedelta(minutes=5 * i), "mean": mean} for i in range(count)]


# ---------------------------------------------------------------------------
# EffectiveHistoryStore – load / save
# ---------------------------------------------------------------------------


class TestEffectiveHistoryStoreLoadSave:
    def _make_store(self, hass: MagicMock):
        from shady.effective_history import EffectiveHistoryStore

        return EffectiveHistoryStore(hass)

    @pytest.mark.asyncio
    async def test_load_empty_store(self):
        hass = MagicMock()
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=None)
        with patch("shady.effective_history.Store", return_value=store_mock):
            s = self._make_store(hass)
            await s.async_load()
        assert s._cache == {}
        assert s._cached_until is None

    @pytest.mark.asyncio
    async def test_load_valid_cache(self):
        hass = MagicMock()
        cached_until = "2025-06-01T10:00:00+00:00"
        stored = {
            "version": 1,
            "cached_until": cached_until,
            "strings": {
                "sensor.pv1": {"2025-06-01T10:00:00+00:00": 100.0},
            },
        }
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=stored)
        with patch("shady.effective_history.Store", return_value=store_mock):
            s = self._make_store(hass)
            await s.async_load()
        assert "sensor.pv1" in s._cache
        assert s._cached_until == datetime.fromisoformat(cached_until)

    @pytest.mark.asyncio
    async def test_get_slots_returns_copy(self):
        hass = MagicMock()
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=None)
        with patch("shady.effective_history.Store", return_value=store_mock):
            s = self._make_store(hass)
            await s.async_load()
        s._cache["sensor.pv1"] = {"slot1": 42.0}
        result = s.get_slots("sensor.pv1")
        result["slot1"] = 999.0  # modify copy
        assert s._cache["sensor.pv1"]["slot1"] == 42.0  # original unchanged


# ---------------------------------------------------------------------------
# EffectiveHistoryStore – backfill
# ---------------------------------------------------------------------------


class TestEffectiveHistoryBackfill:
    def _make_store_with_patches(self, hass, stats_return, store_data=None):
        from shady.effective_history import EffectiveHistoryStore

        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=store_data)
        store_mock.async_save = AsyncMock()

        with patch("shady.effective_history.Store", return_value=store_mock):
            s = EffectiveHistoryStore(hass)

        return s, store_mock

    @pytest.mark.asyncio
    async def test_no_pv_sensors_skips_backfill(self):
        hass = MagicMock()
        s, store_mock = self._make_store_with_patches(hass, {})
        await s.async_backfill_if_needed([], {}, 7)
        store_mock.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_backfill_computes_effective_slots(self):
        """Backfill with known PV + export data produces correct effective values."""
        hass = MagicMock()
        s, store_mock = self._make_store_with_patches(hass, None)

        # One slot: PV1=200W, PV2=200W, grid_export=300W → loss=100 → each -50 → 150W
        slot = "2025-06-01T10:00:00+00:00"
        stats = {
            "sensor.pv1": [{"start": datetime.fromisoformat(slot), "mean": 200.0}],
            "sensor.pv2": [{"start": datetime.fromisoformat(slot), "mean": 200.0}],
            "sensor.grid_export": [{"start": datetime.fromisoformat(slot), "mean": 300.0}],
        }

        with (
            patch(
                "shady.effective_history.fetch_statistics",
                AsyncMock(return_value=stats),
            ),
            patch(
                "shady.effective_history.detect_unit",
                AsyncMock(return_value=("W", "measurement")),
            ),
        ):
            await s.async_backfill_if_needed(
                ["sensor.pv1", "sensor.pv2"],
                {"grid_export": "sensor.grid_export"},
                7,
            )

        # After W→Wh conversion (×5/60) then back to W for effective:
        # The backfill stores Wh/slot values; we just check they are equal for both strings
        pv1_slots = s.get_slots("sensor.pv1")
        pv2_slots = s.get_slots("sensor.pv2")
        assert slot in pv1_slots
        assert slot in pv2_slots
        assert abs(pv1_slots[slot] - pv2_slots[slot]) < 1e-6

    @pytest.mark.asyncio
    async def test_backfill_skips_already_cached_slots(self):
        hass = MagicMock()
        cached_slot = "2025-06-01T10:00:00+00:00"
        cached_until = datetime.fromisoformat(cached_slot)

        s, store_mock = self._make_store_with_patches(hass, None)
        s._cached_until = cached_until
        s._cache["sensor.pv1"] = {cached_slot: 99.0}

        newer_slot = "2025-06-01T10:05:00+00:00"
        stats = {
            "sensor.pv1": [{"start": datetime.fromisoformat(newer_slot), "mean": 150.0}],
        }

        with (
            patch(
                "shady.effective_history.fetch_statistics",
                AsyncMock(return_value=stats),
            ),
            patch(
                "shady.effective_history.detect_unit",
                AsyncMock(return_value=("W", "measurement")),
            ),
        ):
            await s.async_backfill_if_needed(["sensor.pv1"], {}, 7)

        # Old slot must be untouched
        assert s._cache["sensor.pv1"][cached_slot] == 99.0

    @pytest.mark.asyncio
    async def test_backfill_handles_fetch_error_gracefully(self):
        hass = MagicMock()
        s, store_mock = self._make_store_with_patches(hass, None)

        with patch(
            "shady.effective_history.fetch_statistics",
            AsyncMock(side_effect=RuntimeError("recorder down")),
        ):
            # Should not raise
            await s.async_backfill_if_needed(["sensor.pv1"], {}, 7)

        store_mock.async_save.assert_not_called()
        assert s._cache == {}
