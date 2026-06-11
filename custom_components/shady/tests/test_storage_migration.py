"""Tests for storage version-migration fix and config-hash cache invalidation.

Verifies that:
- compute_config_hash produces stable, discriminating digests.
- EffectiveHistoryStore correctly stores, compares and acts on the config hash.

Note: _DiscardOnMigrationStore._async_migrate_func and coordinator.async_setup
cannot be unit-tested here because conftest stubs Store and DataUpdateCoordinator
as MagicMock objects. The integration-level behaviour is covered by the
EffectiveHistoryStore tests below, which mock the store at the boundary.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# compute_config_hash
# ---------------------------------------------------------------------------


class TestComputeConfigHash:
    def _hash(self, pv_sensors, system_sensor_cfg):
        from shady.effective_history import compute_config_hash

        return compute_config_hash(pv_sensors, system_sensor_cfg)

    def test_returns_16_hex_chars(self):
        h = self._hash(["sensor.pv1"], {"grid_import": "sensor.gi"})
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_inputs_same_hash(self):
        cfg = {"grid_import": "sensor.gi", "grid_export": None}
        assert self._hash(["sensor.pv1"], cfg) == self._hash(["sensor.pv1"], cfg)

    def test_different_pv_sensors_different_hash(self):
        cfg = {"grid_import": "sensor.gi"}
        assert self._hash(["sensor.pv1"], cfg) != self._hash(["sensor.pv2"], cfg)

    def test_pv_sensor_order_matters(self):
        """Order matters because compute_effective_strings is index-based."""
        cfg = {"grid_import": "sensor.gi"}
        assert self._hash(["sensor.pv1", "sensor.pv2"], cfg) != self._hash(
            ["sensor.pv2", "sensor.pv1"], cfg
        )

    def test_different_system_cfg_different_hash(self):
        pv = ["sensor.pv1"]
        assert self._hash(pv, {"grid_import": "sensor.a"}) != self._hash(
            pv, {"grid_import": "sensor.b"}
        )

    def test_none_system_entity_does_not_raise(self):
        pv = ["sensor.pv1"]
        cfg = {"grid_import": None, "grid_export": None, "battery_import": None}
        assert len(self._hash(pv, cfg)) == 16

    def test_shadylib_version_in_hash(self):
        """Changing the shadylib version must change the hash."""
        from shady.effective_history import compute_config_hash

        pv = ["sensor.pv1"]
        cfg: dict = {}
        with patch("shady.effective_history._pkg_version", return_value="1.0.0"):
            h1 = compute_config_hash(pv, cfg)
        with patch("shady.effective_history._pkg_version", return_value="2.0.0"):
            h2 = compute_config_hash(pv, cfg)
        assert h1 != h2


# ---------------------------------------------------------------------------
# EffectiveHistoryStore – config hash lifecycle
# ---------------------------------------------------------------------------


class TestEffectiveHistoryHashInvalidation:
    def _make_eff_store(self, hass, stored_data):
        from shady.effective_history import EffectiveHistoryStore

        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=stored_data)
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = EffectiveHistoryStore(hass)
        return s, store_mock

    @pytest.mark.asyncio
    async def test_load_stores_config_hash(self):
        hass = MagicMock()
        s, _ = self._make_eff_store(
            hass,
            {
                "version": 2,
                "config_hash": "abcd1234abcd1234",
                "cached_until": "2025-06-01T10:00:00+00:00",
                "strings": {},
            },
        )
        await s.async_load()
        assert s._config_hash == "abcd1234abcd1234"

    @pytest.mark.asyncio
    async def test_load_missing_hash_stores_none(self):
        hass = MagicMock()
        s, _ = self._make_eff_store(
            hass,
            {
                "version": 2,
                "cached_until": "2025-06-01T10:00:00+00:00",
                "strings": {"sensor.pv1": {"2025-06-01T10:00:00+00:00": 42.0}},
            },
        )
        await s.async_load()
        assert s._config_hash is None
        # Cache data still loaded – invalidation only happens in backfill
        assert "sensor.pv1" in s._cache

    @pytest.mark.asyncio
    async def test_backfill_invalidates_cache_on_hash_mismatch(self):
        """Stale hash → cache wiped before backfill."""
        from shady.effective_history import EffectiveHistoryStore

        hass = MagicMock()
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(
            return_value={
                "version": 2,
                "config_hash": "000000000000dead",
                "cached_until": "2025-06-01T10:00:00+00:00",
                "strings": {"sensor.pv1": {"2025-06-01T10:00:00+00:00": 99.0}},
            }
        )
        store_mock.async_save = AsyncMock()
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = EffectiveHistoryStore(hass)

        await s.async_load()
        assert s._cache  # populated from disk

        pv = ["sensor.pv1"]
        sys_cfg: dict[str, str | None] = {
            "grid_import": "sensor.gi",
            "grid_export": None,
            "battery_import": None,
            "battery_export": None,
        }
        with (
            patch("shady.effective_history.fetch_statistics", new=AsyncMock(return_value={})),
            patch(
                "shady.effective_history.detect_unit",
                new=AsyncMock(return_value=("W", "measurement")),
            ),
        ):
            await s.async_backfill_if_needed(pv, sys_cfg, history_days=30)

        assert s._cache == {}
        assert s._cached_until is None

    @pytest.mark.asyncio
    async def test_backfill_no_invalidation_on_hash_match(self):
        """Matching hash → cache preserved."""
        from shady.effective_history import EffectiveHistoryStore, compute_config_hash

        hass = MagicMock()
        pv = ["sensor.pv1"]
        sys_cfg: dict[str, str | None] = {
            "grid_import": "sensor.gi",
            "grid_export": None,
            "battery_import": None,
            "battery_export": None,
        }
        current_hash = compute_config_hash(pv, sys_cfg)

        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(
            return_value={
                "version": 2,
                "config_hash": current_hash,
                "cached_until": "2025-06-01T10:00:00+00:00",
                "strings": {"sensor.pv1": {"2025-06-01T10:00:00+00:00": 77.0}},
            }
        )
        store_mock.async_save = AsyncMock()
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = EffectiveHistoryStore(hass)

        await s.async_load()
        original_cache = dict(s._cache)

        with (
            patch("shady.effective_history.fetch_statistics", new=AsyncMock(return_value={})),
            patch(
                "shady.effective_history.detect_unit",
                new=AsyncMock(return_value=("W", "measurement")),
            ),
        ):
            await s.async_backfill_if_needed(pv, sys_cfg, history_days=30)

        assert s._cache == original_cache

    @pytest.mark.asyncio
    async def test_backfill_no_invalidation_when_no_stored_hash(self):
        """No stored hash (old cache format) → cache kept, hash set for future runs."""
        from shady.effective_history import EffectiveHistoryStore, compute_config_hash

        hass = MagicMock()
        pv = ["sensor.pv1"]
        sys_cfg: dict[str, str | None] = {"grid_import": "sensor.gi"}

        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(
            return_value={
                "version": 2,
                "cached_until": "2025-06-01T10:00:00+00:00",
                "strings": {"sensor.pv1": {"2025-06-01T10:00:00+00:00": 55.0}},
            }
        )
        store_mock.async_save = AsyncMock()
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = EffectiveHistoryStore(hass)

        await s.async_load()
        assert s._config_hash is None

        with (
            patch("shady.effective_history.fetch_statistics", new=AsyncMock(return_value={})),
            patch(
                "shady.effective_history.detect_unit",
                new=AsyncMock(return_value=("W", "measurement")),
            ),
        ):
            await s.async_backfill_if_needed(pv, sys_cfg, history_days=30)

        assert "sensor.pv1" in s._cache  # NOT wiped
        assert s._config_hash == compute_config_hash(pv, sys_cfg)

    @pytest.mark.asyncio
    async def test_save_persists_config_hash(self):
        """async_save must include config_hash in the saved payload."""
        from shady.effective_history import EffectiveHistoryStore

        hass = MagicMock()
        store_mock = MagicMock()
        store_mock.async_load = AsyncMock(return_value=None)
        store_mock.async_save = AsyncMock()
        with patch("shady.effective_history._DiscardOnMigrationStore", return_value=store_mock):
            s = EffectiveHistoryStore(hass)

        s._config_hash = "cafebabe12345678"
        await s.async_save()

        saved = store_mock.async_save.call_args[0][0]
        assert saved["config_hash"] == "cafebabe12345678"
