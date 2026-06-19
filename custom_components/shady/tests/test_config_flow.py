"""Tests for config_flow optional-sensor deletion fix (issue #34).

Verifies that:
- _optional_entity returns a vol.Optional *without* a default when the key is
  absent or None in the stored config, so the HA frontend renders the field
  empty instead of re-injecting the old entity ID.
- _optional_entity returns a vol.Optional *with* a default when a value is
  stored, so existing configs are pre-populated correctly.
- _merge_options writes None for every SYSTEM_SENSOR_KEYS entry that the user
  left empty (not present in user_input), preventing _cfg from falling back to
  entry.data on the next options-flow open.
"""

from __future__ import annotations

import voluptuous as vol

from shady.config_flow import _merge_options, _optional_entity
from shady.const import (
    CONF_BATTERY_EXPORT,
    CONF_BATTERY_IMPORT,
    CONF_GRID_EXPORT,
    CONF_GRID_IMPORT,
    SYSTEM_SENSOR_KEYS,
)


class TestOptionalEntity:
    """_optional_entity schema-key helper."""

    def test_no_default_when_key_absent(self):
        """Field should render empty when no value is stored."""
        key = _optional_entity(CONF_GRID_IMPORT, {})
        assert isinstance(key, vol.Optional)
        assert key.default is vol.UNDEFINED

    def test_no_default_when_value_is_none(self):
        """Field should render empty when stored value is None (explicitly cleared)."""
        key = _optional_entity(CONF_GRID_IMPORT, {CONF_GRID_IMPORT: None})
        assert isinstance(key, vol.Optional)
        assert key.default is vol.UNDEFINED

    def test_no_default_when_value_is_empty_string(self):
        """Field should render empty when stored value is an empty string."""
        key = _optional_entity(CONF_GRID_IMPORT, {CONF_GRID_IMPORT: ""})
        assert isinstance(key, vol.Optional)
        assert key.default is vol.UNDEFINED

    def test_default_set_when_value_present(self):
        """Field should be pre-populated when a real entity ID is stored."""
        entity_id = "sensor.grid_import"
        key = _optional_entity(CONF_GRID_IMPORT, {CONF_GRID_IMPORT: entity_id})
        assert isinstance(key, vol.Optional)
        assert key.default() == entity_id

    def test_all_system_sensor_keys(self):
        """All four system sensor keys behave correctly."""
        stored = {
            CONF_GRID_IMPORT: "sensor.gi",
            CONF_GRID_EXPORT: None,
            CONF_BATTERY_IMPORT: "",
            # CONF_BATTERY_EXPORT absent
        }
        gi = _optional_entity(CONF_GRID_IMPORT, stored)
        ge = _optional_entity(CONF_GRID_EXPORT, stored)
        bi = _optional_entity(CONF_BATTERY_IMPORT, stored)
        be = _optional_entity(CONF_BATTERY_EXPORT, stored)

        assert gi.default() == "sensor.gi"
        assert ge.default is vol.UNDEFINED
        assert bi.default is vol.UNDEFINED
        assert be.default is vol.UNDEFINED


class TestMergeOptions:
    """_merge_options ensures cleared sensors are stored as None."""

    def test_absent_keys_become_none(self):
        """System sensor keys missing from user_input must be set to None."""
        user_input = {
            "fc_sensor": "sensor.fc",
            "pv_sensors": ["sensor.pv"],
            CONF_GRID_IMPORT: "sensor.gi",
            # grid_export, battery_import, battery_export intentionally absent
        }
        result = _merge_options(user_input)
        assert result[CONF_GRID_EXPORT] is None
        assert result[CONF_BATTERY_IMPORT] is None
        assert result[CONF_BATTERY_EXPORT] is None

    def test_present_keys_are_preserved(self):
        """System sensor keys that the user filled must not be overwritten."""
        user_input = {CONF_GRID_IMPORT: "sensor.gi"}
        result = _merge_options(user_input)
        assert result[CONF_GRID_IMPORT] == "sensor.gi"

    def test_all_system_keys_covered(self):
        """Every SYSTEM_SENSOR_KEYS entry appears in the result."""
        result = _merge_options({})
        for key in SYSTEM_SENSOR_KEYS:
            assert key in result

    def test_non_sensor_keys_passed_through(self):
        """Unrelated keys in user_input are not modified."""
        user_input = {"fc_sensor": "sensor.fc", "history_days": 30}
        result = _merge_options(user_input)
        assert result["fc_sensor"] == "sensor.fc"
        assert result["history_days"] == 30

    def test_all_sensors_cleared(self):
        """When user clears all sensors, all four keys become None."""
        result = _merge_options({"fc_sensor": "sensor.fc"})
        for key in SYSTEM_SENSOR_KEYS:
            assert result[key] is None
