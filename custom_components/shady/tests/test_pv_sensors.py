"""Tests for the dynamic pv_sensors list feature.

Covers:
- _entity_id_to_slug (sensor.py)
- SolarForecastStringCurrentSensor unique_id and name (sensor.py)
- _migrate_legacy_pv_sensors (__init__): all migration scenarios
- coordinator._active_pv_sensors reads CONF_PV_SENSORS list
"""

from __future__ import annotations

from unittest.mock import MagicMock


from shady.const import (
    CONF_PV_SENSORS,
    LEGACY_PV_SENSOR_KEYS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(options=None, data=None):
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = options if options is not None else {}
    entry.data = data if data is not None else {}
    return entry


def _make_hass():
    hass = MagicMock()
    hass.config_entries = MagicMock()
    return hass


# ---------------------------------------------------------------------------
# _entity_id_to_slug
# ---------------------------------------------------------------------------


class TestEntityIdToSlug:
    def _slug(self, entity_id: str) -> str:
        from shady.sensor import _entity_id_to_slug

        return _entity_id_to_slug(entity_id)

    def test_strips_domain_prefix(self):
        assert self._slug("sensor.pv_string_dach_ost") == "pv_string_dach_ost"

    def test_no_domain_prefix(self):
        assert self._slug("pv_string_dach_ost") == "pv_string_dach_ost"

    def test_lowercased(self):
        assert self._slug("sensor.PV_String_DACH") == "pv_string_dach"

    def test_special_chars_replaced_by_underscore(self):
        assert self._slug("sensor.pv-string dach.ost") == "pv_string_dach_ost"

    def test_leading_trailing_underscores_stripped(self):
        result = self._slug("sensor.___pv_string___")
        assert not result.startswith("_")
        assert not result.endswith("_")

    def test_numeric_chars_kept(self):
        assert self._slug("sensor.pv123") == "pv123"

    def test_domain_prefix_stripped_leaving_local_part(self):
        """sensor.pv_string_dach_ost → pv_string_dach_ost (domain stripped)"""
        assert self._slug("sensor.pv_string_dach_ost") == "pv_string_dach_ost"


# ---------------------------------------------------------------------------
# SolarForecastStringCurrentSensor – unique_id and name
# ---------------------------------------------------------------------------


class TestStringCurrentSensorMetadata:
    def _make_sensor(self, entity_id: str):
        from shady.sensor import SolarForecastStringCurrentSensor

        coordinator = MagicMock()
        coordinator.data = None
        entry = _make_entry(options={CONF_PV_SENSORS: [entity_id]})
        return SolarForecastStringCurrentSensor(coordinator, entry, entity_id)

    def test_unique_id_format(self):
        """unique_id = '{entry_id}_forecast_{slug}'"""
        sensor = self._make_sensor("sensor.pv_string_dach_ost")
        assert sensor._attr_unique_id == "test_entry_id_pv_string_dach_ost_fc"

    def test_unique_id_no_legacy_suffix(self):
        """unique_id must not contain old _pv_sensor_N suffix."""
        sensor = self._make_sensor("sensor.pv_string_dach_ost")
        for k in LEGACY_PV_SENSOR_KEYS:
            assert k not in sensor._attr_unique_id

    def test_unique_id_contains_forecast(self):
        sensor = self._make_sensor("sensor.pv_string_dach_ost")
        assert sensor._attr_unique_id.endswith("_fc")

    def test_attr_name_format(self):
        """_attr_name = 'Shady Forecast {slug}'"""
        sensor = self._make_sensor("sensor.pv_string_dach_ost")
        assert sensor._attr_name == "Solar String pv_string_dach_ost Forecast"

    def test_attr_name_starts_with_shady(self):
        sensor = self._make_sensor("sensor.my_pv")
        assert sensor._attr_name.startswith("Solar String")

    def test_attr_name_no_hourly(self):
        """'Hourly' must not appear in the new name."""
        sensor = self._make_sensor("sensor.pv_string_dach_ost")
        assert "Hourly" not in sensor._attr_name

    def test_two_different_strings_have_distinct_unique_ids(self):
        s1 = self._make_sensor("sensor.pv_string_dach_ost")
        s2 = self._make_sensor("sensor.pv_string_dach_west")
        assert s1._attr_unique_id != s2._attr_unique_id

    def test_two_different_strings_have_distinct_names(self):
        s1 = self._make_sensor("sensor.pv_string_dach_ost")
        s2 = self._make_sensor("sensor.pv_string_dach_west")
        assert s1._attr_name != s2._attr_name

    def test_pv_entity_id_stored(self):
        sensor = self._make_sensor("sensor.my_pv_string")
        assert sensor._pv_entity_id == "sensor.my_pv_string"


# ---------------------------------------------------------------------------
# _migrate_legacy_pv_sensors
# ---------------------------------------------------------------------------


class TestMigrateLegacyPvSensors:
    def _migrate(self, options=None, data=None):
        from shady import _migrate_legacy_pv_sensors

        hass = _make_hass()
        entry = _make_entry(options=options, data=data)
        _migrate_legacy_pv_sensors(hass, entry)
        return hass, entry

    def _migrated_options(self, options=None, data=None):
        hass, _ = self._migrate(options=options, data=data)
        hass.config_entries.async_update_entry.assert_called_once()
        _, kwargs = hass.config_entries.async_update_entry.call_args
        return kwargs["options"]

    # --- no-op cases ---

    def test_no_legacy_keys_no_migration(self):
        hass, _ = self._migrate(options={})
        hass.config_entries.async_update_entry.assert_not_called()

    def test_already_migrated_skips(self):
        """pv_sensors already present → no update_entry call."""
        hass, _ = self._migrate(options={CONF_PV_SENSORS: ["sensor.pv1"]})
        hass.config_entries.async_update_entry.assert_not_called()

    def test_all_empty_strings_no_migration(self):
        hass, _ = self._migrate(
            options={"pv_sensor_1": "", "pv_sensor_2": "", "pv_sensor_3": "", "pv_sensor_4": ""}
        )
        hass.config_entries.async_update_entry.assert_not_called()

    # --- migration content ---

    def test_four_sensors_migrated_in_order(self):
        opts = self._migrated_options(
            options={
                "pv_sensor_1": "sensor.s1",
                "pv_sensor_2": "sensor.s2",
                "pv_sensor_3": "sensor.s3",
                "pv_sensor_4": "sensor.s4",
            }
        )
        assert opts[CONF_PV_SENSORS] == ["sensor.s1", "sensor.s2", "sensor.s3", "sensor.s4"]

    def test_single_sensor_migrated(self):
        opts = self._migrated_options(options={"pv_sensor_1": "sensor.only_one"})
        assert opts[CONF_PV_SENSORS] == ["sensor.only_one"]

    def test_empty_strings_dropped(self):
        opts = self._migrated_options(
            options={
                "pv_sensor_1": "sensor.s1",
                "pv_sensor_2": "",
                "pv_sensor_3": "sensor.s3",
                "pv_sensor_4": "",
            }
        )
        assert opts[CONF_PV_SENSORS] == ["sensor.s1", "sensor.s3"]

    def test_order_preserved_with_gaps(self):
        """Order must follow pv_sensor_1, 2, 3, 4 — not insertion order of dict."""
        opts = self._migrated_options(
            options={
                "pv_sensor_4": "sensor.s4",
                "pv_sensor_2": "sensor.s2",
                "pv_sensor_1": "sensor.s1",
                "pv_sensor_3": "sensor.s3",
            }
        )
        assert opts[CONF_PV_SENSORS] == ["sensor.s1", "sensor.s2", "sensor.s3", "sensor.s4"]

    # --- cleanup ---

    def test_legacy_keys_removed_from_options(self):
        opts = self._migrated_options(
            options={"pv_sensor_1": "sensor.s1", "pv_sensor_2": "sensor.s2"}
        )
        for k in LEGACY_PV_SENSOR_KEYS:
            assert k not in opts

    def test_other_options_preserved(self):
        opts = self._migrated_options(
            options={
                "pv_sensor_1": "sensor.s1",
                "fc_sensor": "sensor.forecast",
                "history_days": 28,
                "algorithm": "linear",
            }
        )
        assert opts["fc_sensor"] == "sensor.forecast"
        assert opts["history_days"] == 28
        assert opts["algorithm"] == "linear"

    # --- data fallback ---

    def test_reads_from_data_when_options_is_none(self):
        """entry.options = None → falls back to entry.data."""
        opts = self._migrated_options(
            options=None,
            data={"pv_sensor_1": "sensor.from_data"},
        )
        assert opts[CONF_PV_SENSORS] == ["sensor.from_data"]

    def test_result_written_to_options(self):
        """Migration result is always written to entry.options, not entry.data."""
        from shady import _migrate_legacy_pv_sensors

        hass = _make_hass()
        entry = _make_entry(options={"pv_sensor_1": "sensor.s1"})
        _migrate_legacy_pv_sensors(hass, entry)
        _, kwargs = hass.config_entries.async_update_entry.call_args
        assert "options" in kwargs

    def test_entry_passed_to_update(self):
        from shady import _migrate_legacy_pv_sensors

        hass = _make_hass()
        entry = _make_entry(options={"pv_sensor_1": "sensor.s1"})
        _migrate_legacy_pv_sensors(hass, entry)
        args, _ = hass.config_entries.async_update_entry.call_args
        assert args[0] is entry


# ---------------------------------------------------------------------------
# coordinator._active_pv_sensors reads CONF_PV_SENSORS list
# ---------------------------------------------------------------------------


class TestCoordinatorActivePvSensors:
    def _make_coordinator(self, options: dict):
        from shady.coordinator import ShadyCoordinator

        entry = _make_entry(options=options)
        coord = ShadyCoordinator.__new__(ShadyCoordinator)
        coord._entry = entry
        return coord

    def test_returns_all_sensors(self):
        coord = self._make_coordinator({CONF_PV_SENSORS: ["sensor.s1", "sensor.s2", "sensor.s3"]})
        assert coord._active_pv_sensors() == ["sensor.s1", "sensor.s2", "sensor.s3"]

    def test_empty_list_returns_empty(self):
        coord = self._make_coordinator({CONF_PV_SENSORS: []})
        assert coord._active_pv_sensors() == []

    def test_missing_key_returns_empty(self):
        coord = self._make_coordinator({})
        assert coord._active_pv_sensors() == []

    def test_filters_empty_strings(self):
        coord = self._make_coordinator({CONF_PV_SENSORS: ["sensor.s1", "", "sensor.s3"]})
        result = coord._active_pv_sensors()
        assert "" not in result
        assert result == ["sensor.s1", "sensor.s3"]

    def test_more_than_four_sensors(self):
        """No upper limit – must return all entries."""
        sensors = [f"sensor.pv_string_{i}" for i in range(10)]
        coord = self._make_coordinator({CONF_PV_SENSORS: sensors})
        assert coord._active_pv_sensors() == sensors

    def test_single_sensor(self):
        coord = self._make_coordinator({CONF_PV_SENSORS: ["sensor.only"]})
        assert coord._active_pv_sensors() == ["sensor.only"]
