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
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
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
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = self._make_store(hass)
            await s.async_load()
        assert "sensor.pv1" in s._cache
        assert s._cached_until == datetime.fromisoformat(cached_until)

    @pytest.mark.asyncio
    async def test_get_slots_returns_copy(self):
        hass = MagicMock()
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=None)
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
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

        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
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

        # One slot: PV1=200W, PV2=200W, grid_export=300W
        # pv_usable = 300W, total_loss = 400W - 300W = 100W → even split → 150W each
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


# ---------------------------------------------------------------------------
# BMS pv_usable formula
# ---------------------------------------------------------------------------


class TestPvUsableFormula:
    """Unit-level tests for the pv_usable computation embedded in backfill.

    pv_usable = max(0, (grid_export + battery_import) - (grid_import + battery_export))
    total_loss = pv_sum - pv_usable
    """

    def _make_store_with_patches(self, hass, cached_until):
        from shady.effective_history import EffectiveHistoryStore
        from unittest.mock import AsyncMock, MagicMock, patch

        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=None)
        store_mock.async_save = AsyncMock()

        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = EffectiveHistoryStore(hass)
        return s, store_mock

    async def _run_backfill(self, pv_mean, system_cfg, system_means):
        from datetime import datetime
        from unittest.mock import AsyncMock, MagicMock, patch

        hass = MagicMock()
        s, _ = self._make_store_with_patches(hass, None)
        slot = "2025-06-01T10:00:00+00:00"

        stats = {"sensor.pv1": [{"start": datetime.fromisoformat(slot), "mean": pv_mean}]}
        for sensor_id, mean in system_means.items():
            stats[sensor_id] = [{"start": datetime.fromisoformat(slot), "mean": mean}]

        with (
            patch("shady.effective_history.fetch_statistics", AsyncMock(return_value=stats)),
            patch(
                "shady.effective_history.detect_unit",
                AsyncMock(return_value=("W", "measurement")),
            ),
        ):
            await s.async_backfill_if_needed(["sensor.pv1"], system_cfg, 7)
        return s.get_slots("sensor.pv1").get(slot)

    @pytest.mark.asyncio
    async def test_only_grid_export_reduces_pv(self):
        # PV=300W, grid_export=200W -> pv_usable=200 -> loss=100 -> effective < raw
        val = await self._run_backfill(
            pv_mean=300.0,
            system_cfg={"grid_export": "sensor.grid_export"},
            system_means={"sensor.grid_export": 200.0},
        )
        assert val is not None
        assert val < 300.0 * (5 / 60)

    @pytest.mark.asyncio
    async def test_battery_discharge_reduces_pv_usable(self):
        # battery_export offsets grid_export: pv_usable = 200-100 = 100 -> more loss
        val_without = await self._run_backfill(
            pv_mean=300.0,
            system_cfg={"grid_export": "sensor.grid_export"},
            system_means={"sensor.grid_export": 200.0},
        )
        val_with_discharge = await self._run_backfill(
            pv_mean=300.0,
            system_cfg={
                "grid_export": "sensor.grid_export",
                "battery_export": "sensor.battery_export",
            },
            system_means={"sensor.grid_export": 200.0, "sensor.battery_export": 100.0},
        )
        assert val_with_discharge is not None
        assert val_without is not None
        assert val_with_discharge <= val_without

    @pytest.mark.asyncio
    async def test_grid_import_reduces_pv_usable(self):
        # grid_import reduces pv_usable -> more loss -> lower effective
        val_without_import = await self._run_backfill(
            pv_mean=300.0,
            system_cfg={"grid_export": "sensor.grid_export"},
            system_means={"sensor.grid_export": 200.0},
        )
        val_with_import = await self._run_backfill(
            pv_mean=300.0,
            system_cfg={
                "grid_export": "sensor.grid_export",
                "grid_import": "sensor.grid_import",
            },
            system_means={"sensor.grid_export": 200.0, "sensor.grid_import": 50.0},
        )
        assert val_with_import is not None
        assert val_with_import <= val_without_import

    @pytest.mark.asyncio
    async def test_pv_usable_clamped_to_zero_when_sources_exceed_output(self):
        # Only grid_import, no grid_export: pv_usable = max(0, 0-200) = 0
        # -> total_loss = pv_sum -> all effective = 0
        val = await self._run_backfill(
            pv_mean=200.0,
            system_cfg={"grid_import": "sensor.grid_import"},
            system_means={"sensor.grid_import": 200.0},
        )
        assert val is not None
        assert val == 0.0


# ---------------------------------------------------------------------------
# EffectiveHistoryStore.invalidate – used by async_rebuild_history
# ---------------------------------------------------------------------------


class TestEffectiveHistoryStoreInvalidate:
    def _make_store(self, hass):
        from shady.effective_history import EffectiveHistoryStore

        return EffectiveHistoryStore(hass)

    def test_invalidate_clears_cache_and_cached_until(self):
        from datetime import datetime, timezone
        from unittest.mock import MagicMock, patch

        hass = MagicMock()
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=None)
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = self._make_store(hass)
            # Pre-populate with some data
            s._cache = {"sensor.pv1": {"2025-06-01T08:00:00+00:00": 25.0}}
            s._cached_until = datetime(2025, 6, 14, 23, 55, tzinfo=timezone.utc)

            s.invalidate()

            assert s._cache == {}
            assert s._cached_until is None

    def test_invalidate_idempotent_on_empty_cache(self):
        from unittest.mock import MagicMock, patch

        hass = MagicMock()
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=None)
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = self._make_store(hass)
            # Already empty
            s._cache = {}
            s._cached_until = None

            s.invalidate()  # Must not raise

            assert s._cache == {}
            assert s._cached_until is None

    def test_get_slots_returns_empty_after_invalidate(self):
        from unittest.mock import MagicMock, patch

        hass = MagicMock()
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=None)
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = self._make_store(hass)
            s._cache = {"sensor.pv1": {"2025-06-01T08:00:00+00:00": 25.0}}
            s._cached_until = None

            s.invalidate()

            assert s.get_slots("sensor.pv1") == {}

    @pytest.mark.asyncio
    async def test_backfill_after_invalidate_rebuilds_from_scratch(self):
        """After invalidate(), the next backfill must treat all slots as missing."""
        from datetime import datetime, timezone, timedelta
        from unittest.mock import MagicMock, AsyncMock, patch

        UTC = timezone.utc
        now = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
        day_start = datetime(2025, 6, 14, 8, 0, tzinfo=UTC)

        hass = MagicMock()
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=None)
        store_mock.async_save = AsyncMock()

        rows = [{"start": day_start + timedelta(minutes=5 * i), "mean": 50.0} for i in range(10)]

        with (
            patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock),
            patch(
                "shady.effective_history.fetch_statistics",
                new=AsyncMock(return_value={"sensor.pv1": rows}),
            ),
            patch("shady.effective_history.dt_util") as mock_dt,
            patch(
                "shady.effective_history.detect_unit",
                new=AsyncMock(return_value=("W", "measurement")),
            ),
        ):
            mock_dt.now.return_value = now
            mock_dt.UTC = UTC

            from shady.effective_history import EffectiveHistoryStore

            s = EffectiveHistoryStore(hass)
            s._cache = {"sensor.pv1": {"2025-06-01T08:00:00+00:00": 25.0}}
            s._cached_until = datetime(2025, 6, 13, 23, 55, tzinfo=UTC)

            s.invalidate()
            assert s._cache == {}
            assert s._cached_until is None

            # After invalidate, backfill should run and populate cache
            await s.async_backfill_if_needed(
                ["sensor.pv1"],
                {},
                history_days=2,
            )
            # Cache must now be populated
            assert "sensor.pv1" in s._cache or store_mock.async_save.called
