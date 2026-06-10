"""Tests for EffectiveStringSensor in sensor.py."""

from __future__ import annotations

from unittest.mock import MagicMock


from shady.coordinator import CoordinatorData


def _make_coordinator(effective_values: dict) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = CoordinatorData(
        effective_string_values=effective_values,
        fc_unit="W",
        fc_state_class="measurement",
    )
    return coordinator


def _make_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = {}
    entry.data = {}
    return entry


class TestEffectiveStringSensor:
    def _make_sensor(self, pv_entity_id: str, effective_values: dict):
        from shady.sensor import EffectiveStringSensor

        coordinator = _make_coordinator(effective_values)
        sensor = EffectiveStringSensor.__new__(EffectiveStringSensor)
        sensor._pv_entity_id = pv_entity_id
        sensor.coordinator = coordinator
        return sensor

    def test_native_value_returns_effective(self):
        sensor = self._make_sensor("sensor.pv1", {"sensor.pv1": 150.0, "sensor.pv2": 200.0})
        assert sensor.native_value == 150.0

    def test_native_value_returns_none_when_not_present(self):
        sensor = self._make_sensor("sensor.pv_missing", {})
        assert sensor.native_value is None

    def test_native_value_zero(self):
        sensor = self._make_sensor("sensor.pv1", {"sensor.pv1": 0.0})
        assert sensor.native_value == 0.0

    def test_extra_state_attributes_contains_pv_sensor(self):
        sensor = self._make_sensor("sensor.pv1", {})
        attrs = sensor.extra_state_attributes
        assert attrs.get("pv_sensor") == "sensor.pv1"
