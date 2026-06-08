"""Tests for forecast.py – raw forecast fetching."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestFetchRawForecast:
    def _make_hass(self, energy_data: dict, platforms: dict, config_entries: dict) -> tuple:
        hass = MagicMock()

        manager = MagicMock()
        manager.data = energy_data

        async def mock_get_manager(h):
            return manager

        async def mock_get_platforms(h):
            return platforms

        entry_registry = MagicMock()
        entry_registry.async_get_entry = lambda eid: config_entries.get(eid)
        hass.config_entries = entry_registry

        return hass, mock_get_manager, mock_get_platforms

    @pytest.mark.asyncio
    async def test_no_energy_data_returns_empty(self):
        from custom_components.shady.forecast import fetch_raw_forecast

        hass = MagicMock()
        manager = MagicMock()
        manager.data = None

        with patch(
            "custom_components.shady.forecast.async_get_energy_manager",
            AsyncMock(return_value=manager),
        ):
            result = await fetch_raw_forecast(hass)
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_solar_sources_returns_empty(self):
        from custom_components.shady.forecast import fetch_raw_forecast

        hass = MagicMock()
        manager = MagicMock()
        manager.data = {"energy_sources": [{"type": "grid"}]}

        with patch(
            "custom_components.shady.forecast.async_get_energy_manager",
            AsyncMock(return_value=manager),
        ):
            result = await fetch_raw_forecast(hass)
        assert result == {}

    @pytest.mark.asyncio
    async def test_solar_source_no_forecast_returns_empty(self):
        from custom_components.shady.forecast import fetch_raw_forecast

        hass = MagicMock()
        manager = MagicMock()
        manager.data = {"energy_sources": [{"type": "solar", "config_entry_solar_forecast": []}]}

        with patch(
            "custom_components.shady.forecast.async_get_energy_manager",
            AsyncMock(return_value=manager),
        ):
            result = await fetch_raw_forecast(hass)
        assert result == {}

    @pytest.mark.asyncio
    async def test_forecast_data_aggregated(self):
        from custom_components.shady.forecast import fetch_raw_forecast

        entry_id = "abc123"
        hass = MagicMock()

        manager = MagicMock()
        manager.data = {
            "energy_sources": [
                {
                    "type": "solar",
                    "config_entry_solar_forecast": [entry_id],
                }
            ]
        }

        ce = MagicMock()
        ce.domain = "forecast_solar"
        hass.config_entries.async_get_entry = lambda eid: ce if eid == entry_id else None

        forecast_data = {
            entry_id: {
                "2025-06-01T10:00:00+00:00": 400.0,
                "2025-06-01T11:00:00+00:00": 500.0,
            }
        }

        async def mock_platform_fn(h, eid):
            return forecast_data

        with (
            patch(
                "custom_components.shady.forecast.async_get_energy_manager",
                AsyncMock(return_value=manager),
            ),
            patch(
                "custom_components.shady.forecast.async_get_energy_platforms",
                AsyncMock(return_value={"forecast_solar": mock_platform_fn}),
            ),
        ):
            result = await fetch_raw_forecast(hass)

        assert result["2025-06-01T10:00:00+00:00"] == 400.0
        assert result["2025-06-01T11:00:00+00:00"] == 500.0

    @pytest.mark.asyncio
    async def test_two_sources_aggregated(self):
        """Two solar sources with same timestamp → values summed."""
        from custom_components.shady.forecast import fetch_raw_forecast

        hass = MagicMock()
        manager = MagicMock()
        manager.data = {
            "energy_sources": [
                {
                    "type": "solar",
                    "config_entry_solar_forecast": ["e1", "e2"],
                }
            ]
        }

        def get_entry(eid):
            ce = MagicMock()
            ce.domain = "forecast_solar"
            return ce

        hass.config_entries.async_get_entry = get_entry

        async def mock_fn(h, eid):
            return {eid: {"2025-06-01T10:00:00+00:00": 200.0}}

        with (
            patch(
                "custom_components.shady.forecast.async_get_energy_manager",
                AsyncMock(return_value=manager),
            ),
            patch(
                "custom_components.shady.forecast.async_get_energy_platforms",
                AsyncMock(return_value={"forecast_solar": mock_fn}),
            ),
        ):
            result = await fetch_raw_forecast(hass)

        assert result["2025-06-01T10:00:00+00:00"] == 400.0

    @pytest.mark.asyncio
    async def test_platform_exception_skipped(self):
        """If one platform raises, others still processed."""
        from custom_components.shady.forecast import fetch_raw_forecast

        hass = MagicMock()
        manager = MagicMock()
        manager.data = {
            "energy_sources": [
                {
                    "type": "solar",
                    "config_entry_solar_forecast": ["e1"],
                }
            ]
        }

        ce = MagicMock()
        ce.domain = "bad_provider"
        hass.config_entries.async_get_entry = lambda eid: ce

        async def bad_fn(h, eid):
            raise RuntimeError("API error")

        with (
            patch(
                "custom_components.shady.forecast.async_get_energy_manager",
                AsyncMock(return_value=manager),
            ),
            patch(
                "custom_components.shady.forecast.async_get_energy_platforms",
                AsyncMock(return_value={"bad_provider": bad_fn}),
            ),
        ):
            result = await fetch_raw_forecast(hass)

        assert result == {}

    @pytest.mark.asyncio
    async def test_result_is_sorted(self):
        from custom_components.shady.forecast import fetch_raw_forecast

        hass = MagicMock()
        manager = MagicMock()
        manager.data = {
            "energy_sources": [
                {
                    "type": "solar",
                    "config_entry_solar_forecast": ["e1"],
                }
            ]
        }

        ce = MagicMock()
        ce.domain = "forecast_solar"
        hass.config_entries.async_get_entry = lambda eid: ce

        async def mock_fn(h, eid):
            return {
                eid: {
                    "2025-06-01T12:00:00+00:00": 300.0,
                    "2025-06-01T10:00:00+00:00": 400.0,
                    "2025-06-01T11:00:00+00:00": 500.0,
                }
            }

        with (
            patch(
                "custom_components.shady.forecast.async_get_energy_manager",
                AsyncMock(return_value=manager),
            ),
            patch(
                "custom_components.shady.forecast.async_get_energy_platforms",
                AsyncMock(return_value={"forecast_solar": mock_fn}),
            ),
        ):
            result = await fetch_raw_forecast(hass)

        keys = list(result.keys())
        assert keys == sorted(keys)
