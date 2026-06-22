"""Tests for config_flow with two multi-select sensor lists (issue #34).

Verifies that:
- CONF_IMPORT_SENSORS and CONF_EXPORT_SENSORS replace the old four single-entity keys.
- _schema uses vol.Optional with default=[] for both lists.
- Stored values are pre-filled as defaults.
"""

from __future__ import annotations

import voluptuous as vol

from shady.const import (
    CONF_EXPORT_SENSORS,
    CONF_IMPORT_SENSORS,
    SYSTEM_SENSOR_KEYS,
)


class TestSystemSensorKeys:
    """SYSTEM_SENSOR_KEYS contains exactly the two new list keys."""

    def test_contains_import_and_export(self):
        assert CONF_IMPORT_SENSORS in SYSTEM_SENSOR_KEYS
        assert CONF_EXPORT_SENSORS in SYSTEM_SENSOR_KEYS

    def test_old_keys_gone(self):
        for old_key in ("grid_import", "grid_export", "battery_import", "battery_export"):
            assert old_key not in SYSTEM_SENSOR_KEYS

    def test_length(self):
        assert len(SYSTEM_SENSOR_KEYS) == 2


class TestSchemaKeys:
    """_schema vol.Schema contains correct vol.Optional keys for import/export."""

    def _get_key(self, schema: vol.Schema, name: str) -> vol.Optional | None:
        for key in schema.schema:
            if hasattr(key, "schema") and key.schema == name:
                return key
        return None

    def test_import_sensors_key_is_optional_with_empty_default(self):
        from shady.config_flow import _schema

        schema = _schema({})
        key = self._get_key(schema, CONF_IMPORT_SENSORS)
        assert key is not None
        assert isinstance(key, vol.Optional)
        assert key.default() == []

    def test_export_sensors_key_is_optional_with_empty_default(self):
        from shady.config_flow import _schema

        schema = _schema({})
        key = self._get_key(schema, CONF_EXPORT_SENSORS)
        assert key is not None
        assert isinstance(key, vol.Optional)
        assert key.default() == []

    def test_stored_import_sensors_used_as_default(self):
        from shady.config_flow import _schema

        stored = {CONF_IMPORT_SENSORS: ["sensor.gi", "sensor.bi"]}
        schema = _schema(stored)
        key = self._get_key(schema, CONF_IMPORT_SENSORS)
        assert key is not None
        assert key.default() == ["sensor.gi", "sensor.bi"]

    def test_stored_export_sensors_used_as_default(self):
        from shady.config_flow import _schema

        stored = {CONF_EXPORT_SENSORS: ["sensor.ge"]}
        schema = _schema(stored)
        key = self._get_key(schema, CONF_EXPORT_SENSORS)
        assert key is not None
        assert key.default() == ["sensor.ge"]
