"""Tests for config_flow optional-sensor handling (issue #34).

Verifies:
- _optional_entity_validator converts empty string / None to None without
  raising, and passes valid entity IDs through EntitySelector validation.
- _optional_entity attaches suggested_value when a stored value exists.
- _merge_options distinguishes "X clicked" (key present, None) from
  "field untouched" (key absent) and falls back to stored for the latter.
"""

from __future__ import annotations

import voluptuous as vol

from shady.config_flow import _merge_options, _optional_entity, _optional_entity_validator
from shady.const import (
    CONF_GRID_EXPORT,
    CONF_GRID_IMPORT,
    SYSTEM_SENSOR_KEYS,
)


class TestOptionalEntityValidator:
    """_optional_entity_validator converts HA frontend output correctly."""

    def test_empty_string_becomes_none(self):
        """HA sends '' when user clicks ✕ — must not raise, must return None."""
        assert _optional_entity_validator("") is None

    def test_none_becomes_none(self):
        """None input is treated as cleared."""
        assert _optional_entity_validator(None) is None

    def test_non_empty_value_delegates_to_entity_sel(self):
        """A non-empty value is passed to _ENTITY_SEL for validation (mocked in tests)."""
        # In production _ENTITY_SEL validates the entity ID; here we only verify
        # that a truthy value is not swallowed and the validator returns something.
        result = _optional_entity_validator("sensor.grid_import")
        assert result is not None


class TestOptionalEntity:
    """_optional_entity schema-key helper."""

    def test_suggested_value_when_stored(self):
        """Stored entity ID appears as suggested_value placeholder."""
        key = _optional_entity(CONF_GRID_IMPORT, "sensor.gi")
        assert key.description == {"suggested_value": "sensor.gi"}

    def test_no_description_when_none(self):
        """No suggested_value when nothing is stored."""
        key = _optional_entity(CONF_GRID_IMPORT, None)
        assert key.description is None

    def test_no_description_when_empty_string(self):
        """Empty string treated as not stored."""
        key = _optional_entity(CONF_GRID_IMPORT, "")
        assert key.description is None

    def test_no_default(self):
        """No default set — key absent from user_input when field untouched."""
        key = _optional_entity(CONF_GRID_IMPORT, "sensor.gi")
        assert key.default is vol.UNDEFINED

    def test_returns_vol_optional(self):
        assert isinstance(_optional_entity(CONF_GRID_IMPORT, None), vol.Optional)


class TestMergeOptions:
    """_merge_options correctly resolves optional sensor keys."""

    def test_none_means_cleared(self):
        """Key present as None (X clicked) → stored as None."""
        stored = {CONF_GRID_IMPORT: "sensor.gi"}
        result = _merge_options({CONF_GRID_IMPORT: None}, stored)
        assert result[CONF_GRID_IMPORT] is None

    def test_absent_key_falls_back_to_stored(self):
        """Key absent (field untouched) → stored value preserved."""
        stored = {CONF_GRID_IMPORT: "sensor.gi", CONF_GRID_EXPORT: "sensor.ge"}
        result = _merge_options({CONF_GRID_IMPORT: "sensor.gi"}, stored)
        assert result[CONF_GRID_EXPORT] == "sensor.ge"

    def test_absent_key_with_nothing_stored_is_none(self):
        """Key absent and nothing stored → None."""
        result = _merge_options({}, {})
        for key in SYSTEM_SENSOR_KEYS:
            assert result[key] is None

    def test_new_value_preserved(self):
        """Key present with a new entity ID → kept."""
        result = _merge_options({CONF_GRID_IMPORT: "sensor.new"}, {CONF_GRID_IMPORT: "sensor.old"})
        assert result[CONF_GRID_IMPORT] == "sensor.new"

    def test_all_system_keys_in_result(self):
        """Every SYSTEM_SENSOR_KEY appears in the result."""
        result = _merge_options({}, {})
        for key in SYSTEM_SENSOR_KEYS:
            assert key in result

    def test_non_sensor_keys_passed_through(self):
        """Unrelated keys are not modified."""
        result = _merge_options({"fc_sensor": "sensor.fc", "history_days": 30}, {})
        assert result["fc_sensor"] == "sensor.fc"
        assert result["history_days"] == 30

    def test_all_cleared_explicitly(self):
        """All four sensors cleared by X → all None."""
        stored = {k: "sensor.x" for k in SYSTEM_SENSOR_KEYS}
        user_input = {key: None for key in SYSTEM_SENSOR_KEYS}
        result = _merge_options(user_input, stored)
        for key in SYSTEM_SENSOR_KEYS:
            assert result[key] is None
