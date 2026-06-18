"""Tests for coordinator.py – bucket model caching and yesterday-cutoff logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

UTC = timezone.utc


def _dt(hour: int, minute: int = 0, day: int = 15, month: int = 6) -> datetime:
    return datetime(2025, month, day, hour, minute, tzinfo=UTC)


def _make_rows(start: datetime, count: int, mean: float = 100.0) -> list[dict]:
    return [{"start": start + timedelta(minutes=5 * i), "mean": mean} for i in range(count)]


class TestApplyCorrectionsYesterdayCutoff:
    """Verify that _apply_corrections passes only pre-cutoff rows to model fitting."""

    def _make_coordinator(self, cfg: dict | None = None):
        from shady.coordinator import ShadyCoordinator, CoordinatorData
        from unittest.mock import MagicMock

        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test"
        d = {
            "fc_sensor": "sensor.fc",
            "pv_sensors": ["sensor.pv1"],
            "history_days": 7,
            "algorithm": "factor",
            "filter_recorder_gaps": False,
            "use_effective_sensors": False,
        }
        if cfg:
            d.update(cfg)
        entry.options = d
        entry.data = {}

        coord = ShadyCoordinator.__new__(ShadyCoordinator)
        coord.hass = hass
        coord._entry = entry
        coord._unit_cache = {}
        coord._rebuild_lock = __import__("asyncio").Lock()
        coord._cached_bucket_models = {}
        coord._bucket_models_timestamp = None
        coord._unsub_midnight = None
        coord._unsub_listener = None

        data = CoordinatorData()
        coord.data = data

        eff_store = MagicMock()
        eff_store.get_slots = MagicMock(return_value={})
        coord._effective_store = eff_store

        return coord

    @pytest.mark.asyncio
    async def test_today_rows_excluded_from_model_training(self):
        """Rows with start >= today_start must not reach build_bucket_models."""
        coord = self._make_coordinator()

        today_start = datetime(2025, 6, 15, 0, 0, tzinfo=UTC)
        # Rows: 48 from yesterday, 12 from today
        yesterday_rows = _make_rows(_dt(8, 0, day=14), 48, mean=50.0)
        today_rows = _make_rows(_dt(6, 0, day=15), 12, mean=80.0)
        all_rows = yesterday_rows + today_rows

        captured_fc_for_model: list = []
        captured_pv_for_model: list = []

        def _fake_apply(raw_norm, fc_rows_m, pv_rows_m, algorithm):
            captured_fc_for_model.extend(fc_rows_m)
            for rows in pv_rows_m.values():
                captured_pv_for_model.extend(rows)
            return {}, {}, {}

        with (
            patch(
                "shady.coordinator.fetch_statistics",
                new=AsyncMock(
                    return_value={
                        "sensor.fc": all_rows,
                        "sensor.pv1": all_rows,
                    }
                ),
            ),
            patch("shady.coordinator._shadylib_apply_corrections", side_effect=_fake_apply),
            patch("shady.coordinator.dt_util") as mock_dt,
            patch("shady.coordinator.normalise_em_to_5min", return_value={}),
        ):
            mock_dt.now.return_value = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
            mock_dt.utcnow.return_value = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)

            raw: dict[str, float] = {}
            pv_units = {"sensor.pv1": "Wh"}

            with patch("shady.coordinator.to_wh_per_slot", side_effect=lambda rows, unit: rows):
                await coord._apply_corrections(
                    raw,
                    ["sensor.pv1"],
                    "Wh",
                    pv_units,
                    use_effective=False,
                )

        # All rows passed to model must be strictly before today_start
        for row in captured_fc_for_model:
            assert row["start"] < today_start, f"Today row leaked to model: {row['start']}"
        for row in captured_pv_for_model:
            assert row["start"] < today_start, f"Today row leaked to model: {row['start']}"

    @pytest.mark.asyncio
    async def test_bucket_models_cached_after_first_fit(self):
        """After the first _apply_corrections call, models must be stored in cache."""
        coord = self._make_coordinator()

        example_models = {(8, 0): (0.9,), (12, 0): (1.1,)}

        with (
            patch(
                "shady.coordinator.fetch_statistics",
                new=AsyncMock(return_value={"sensor.fc": [], "sensor.pv1": []}),
            ),
            patch(
                "shady.coordinator._shadylib_apply_corrections",
                return_value=({}, {}, example_models),
            ),
            patch("shady.coordinator.dt_util") as mock_dt,
            patch("shady.coordinator.normalise_em_to_5min", return_value={}),
            patch("shady.coordinator.to_wh_per_slot", return_value=[]),
        ):
            mock_dt.now.return_value = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)
            mock_dt.utcnow.return_value = datetime(2025, 6, 15, 10, 0, tzinfo=UTC)

            await coord._apply_corrections({}, ["sensor.pv1"], "Wh", {"sensor.pv1": "Wh"})

        assert coord._cached_bucket_models == example_models
        assert coord._bucket_models_timestamp is not None

    @pytest.mark.asyncio
    async def test_intraday_refresh_reuses_cached_models(self):
        """When _cached_bucket_models is non-empty, build_bucket_models is NOT called again."""
        coord = self._make_coordinator()
        coord._cached_bucket_models = {(8, 0): (0.9,)}  # pre-populate cache
        coord._bucket_models_timestamp = "2025-06-15T00:00:00+00:00"

        call_count = [0]

        def _fake_apply(raw_norm, fc_rows_m, pv_rows_m, algorithm):
            call_count[0] += 1
            return {}, {}, {}

        with (
            patch(
                "shady.coordinator.fetch_statistics",
                new=AsyncMock(return_value={"sensor.fc": [], "sensor.pv1": []}),
            ),
            patch("shady.coordinator._shadylib_apply_corrections", side_effect=_fake_apply),
            patch("shady.coordinator.dt_util") as mock_dt,
            patch("shady.coordinator.normalise_em_to_5min", return_value={}),
            patch("shady.coordinator.to_wh_per_slot", return_value=[]),
        ):
            mock_dt.now.return_value = datetime(2025, 6, 15, 14, 0, tzinfo=UTC)
            mock_dt.utcnow.return_value = datetime(2025, 6, 15, 14, 0, tzinfo=UTC)

            await coord._apply_corrections({}, ["sensor.pv1"], "Wh", {"sensor.pv1": "Wh"})

        # apply_corrections in shadylib is called once (for the intra-day reuse path)
        assert call_count[0] == 1
        # Timestamp must NOT be updated on intra-day refresh
        assert coord._bucket_models_timestamp == "2025-06-15T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_day_start_invalidates_cache(self):
        """_on_day_start must clear _cached_bucket_models and _bucket_models_timestamp."""
        coord = self._make_coordinator()
        coord._cached_bucket_models = {(8, 0): (0.9,)}
        coord._bucket_models_timestamp = "2025-06-14T00:00:00+00:00"
        coord.async_refresh = AsyncMock()

        await coord._on_day_start(now=None)

        assert coord._cached_bucket_models == {}
        assert coord._bucket_models_timestamp is None
        coord.async_refresh.assert_awaited_once()


class TestAsyncRebuildHistory:
    def _make_coordinator(self):
        from shady.coordinator import ShadyCoordinator

        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test"
        entry.options = {
            "pv_sensors": ["sensor.pv1"],
            "history_days": 7,
            "filter_recorder_gaps": False,
        }
        entry.data = {}

        coord = ShadyCoordinator.__new__(ShadyCoordinator)
        coord.hass = hass
        coord._entry = entry
        coord._unit_cache = {}
        coord._rebuild_lock = __import__("asyncio").Lock()
        coord._cached_bucket_models = {(8, 0): (0.9,)}
        coord._bucket_models_timestamp = "2025-06-14T00:00:00+00:00"
        coord._unsub_midnight = None
        coord._unsub_listener = None

        eff_store = MagicMock()
        eff_store.invalidate = MagicMock()
        eff_store.get_slots = MagicMock(return_value={})
        coord._effective_store = eff_store

        coord.async_refresh = AsyncMock()

        return coord

    @pytest.mark.asyncio
    async def test_clears_effective_cache(self):
        coord = self._make_coordinator()
        with patch.object(coord, "_async_backfill_effective", new=AsyncMock()):
            await coord.async_rebuild_history()
        coord._effective_store.invalidate.assert_called_once()

    @pytest.mark.asyncio
    async def test_clears_bucket_model_cache(self):
        coord = self._make_coordinator()
        with patch.object(coord, "_async_backfill_effective", new=AsyncMock()):
            await coord.async_rebuild_history()
        assert coord._cached_bucket_models == {}
        assert coord._bucket_models_timestamp is None

    @pytest.mark.asyncio
    async def test_calls_async_refresh(self):
        coord = self._make_coordinator()
        with patch.object(coord, "_async_backfill_effective", new=AsyncMock()):
            await coord.async_rebuild_history()
        coord.async_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_backfill_called_when_pv_sensors_present(self):
        coord = self._make_coordinator()
        backfill_mock = AsyncMock()
        with patch.object(coord, "_async_backfill_effective", backfill_mock):
            await coord.async_rebuild_history()
        backfill_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_run_guard(self):
        """A second concurrent call must not start a second rebuild."""
        import asyncio

        coord = self._make_coordinator()
        started = []
        finished = []

        async def _slow_backfill():
            started.append(1)
            await asyncio.sleep(0.05)
            finished.append(1)

        with patch.object(coord, "_async_backfill_effective", _slow_backfill):
            await asyncio.gather(
                coord.async_rebuild_history(),
                coord.async_rebuild_history(),
            )

        # Only one backfill should have started (the second call is skipped)
        assert len(started) == 1
