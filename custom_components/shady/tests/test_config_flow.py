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

    def test_no_default_when_no_value(self):
        """Field must never have a default — stored value goes to suggested_value."""
        key = _optional_entity(CONF_GRID_IMPORT, None)
        assert isinstance(key, vol.Optional)
        assert key.default is vol.UNDEFINED
        assert key.description is None

    def test_suggested_value_when_value_present(self):
        """Stored entity ID appears as suggested_value, not as default."""
        entity_id = "sensor.grid_import"
        key = _optional_entity(CONF_GRID_IMPORT, entity_id)
        assert isinstance(key, vol.Optional)
        assert key.default is vol.UNDEFINED
        assert key.description == {"suggested_value": entity_id}

    def test_no_suggestion_when_empty_string(self):
        """Empty string is treated the same as None — no suggestion."""
        key = _optional_entity(CONF_GRID_IMPORT, "")
        assert key.default is vol.UNDEFINED
        assert key.description is None

    def test_all_system_sensor_keys_never_have_default(self):
        """All four system sensor keys never carry a default."""
        for key in [CONF_GRID_IMPORT, CONF_GRID_EXPORT, CONF_BATTERY_IMPORT, CONF_BATTERY_EXPORT]:
            assert _optional_entity(key, "sensor.x").default is vol.UNDEFINED


class TestMergeOptions:
    """_merge_options correctly resolves optional sensor keys."""

    def test_none_in_user_input_means_cleared(self):
        """Key present as None → user clicked ✕ → stored as None."""
        stored = {CONF_GRID_IMPORT: "sensor.gi"}
        result = _merge_options({CONF_GRID_IMPORT: None}, stored)
        assert result[CONF_GRID_IMPORT] is None

    def test_absent_key_falls_back_to_stored(self):
        """Key absent from user_input → user did not touch field → keep stored value."""
        stored = {CONF_GRID_IMPORT: "sensor.gi", CONF_GRID_EXPORT: "sensor.ge"}
        user_input = {CONF_GRID_IMPORT: "sensor.gi"}  # grid_export absent
        result = _merge_options(user_input, stored)
        assert result[CONF_GRID_EXPORT] == "sensor.ge"

    def test_absent_key_with_nothing_stored_becomes_none(self):
        """Key absent from user_input and not in stored → None (never configured)."""
        result = _merge_options({}, {})
        for key in SYSTEM_SENSOR_KEYS:
            assert result[key] is None

    def test_present_value_is_preserved(self):
        """Key present in user_input with a value → kept as-is."""
        user_input = {CONF_GRID_IMPORT: "sensor.new"}
        result = _merge_options(user_input, {CONF_GRID_IMPORT: "sensor.old"})
        assert result[CONF_GRID_IMPORT] == "sensor.new"

    def test_all_system_keys_covered(self):
        """Every SYSTEM_SENSOR_KEYS entry appears in the result."""
        result = _merge_options({}, {})
        for key in SYSTEM_SENSOR_KEYS:
            assert key in result

    def test_non_sensor_keys_passed_through(self):
        """Unrelated keys in user_input are not modified."""
        user_input = {"fc_sensor": "sensor.fc", "history_days": 30}
        result = _merge_options(user_input, {})
        assert result["fc_sensor"] == "sensor.fc"
        assert result["history_days"] == 30

    def test_all_sensors_cleared_explicitly(self):
        """When user sends None for all sensors, all four keys become None."""
        stored = {k: "sensor.x" for k in SYSTEM_SENSOR_KEYS}
        user_input = {key: None for key in SYSTEM_SENSOR_KEYS}
        result = _merge_options(user_input, stored)
        for key in SYSTEM_SENSOR_KEYS:
            assert result[key] is None
