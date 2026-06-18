"""Tests for the BucketModelSensor diagnostic sensor in sensor.py."""

from __future__ import annotations

from datetime import timezone
from unittest.mock import MagicMock

import pytest


UTC = timezone.utc

_EXAMPLE_MODELS: dict = {
    (8, 0): (0.912,),
    (12, 0): (0.743, 12.5),
    (12, 5): (0.031, 0.761, 0.0),
}
_EXAMPLE_TS = "2025-06-01T06:00:00+00:00"


def _make_coordinator_data(
    *,
    string_bucket_models: dict | None = None,
    bucket_models_timestamp: str | None = None,
) -> MagicMock:
    from shady.coordinator import CoordinatorData

    data = CoordinatorData()
    data.string_bucket_models = string_bucket_models or {}
    data.bucket_models_timestamp = bucket_models_timestamp
    return data


def _make_coordinator(data=None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data or _make_coordinator_data()
    return coordinator


def _make_entry(entry_id: str = "test_entry") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _make_sensor(pv_entity_id: str = "sensor.pv1", *, data=None):
    from shady.sensor import BucketModelSensor

    entry = _make_entry()
    coordinator = _make_coordinator(data=data)
    sensor = BucketModelSensor.__new__(BucketModelSensor)
    sensor.coordinator = coordinator
    sensor._entry = entry
    sensor._pv_entity_id = pv_entity_id

    slug = pv_entity_id.split(".", 1)[-1]
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
    sensor._attr_unique_id = f"{entry.entry_id}_{slug}_bucket_models"
    sensor._attr_name = f"Solar String {slug} Bucket Models"
    return sensor


class TestBucketModelSensorNativeValue:
    def test_returns_none_when_no_models(self):
        sensor = _make_sensor()
        assert sensor.native_value is None

    def test_returns_none_when_models_dict_empty(self):
        data = _make_coordinator_data(
            string_bucket_models={"sensor.pv1": {}},
            bucket_models_timestamp=_EXAMPLE_TS,
        )
        sensor = _make_sensor(data=data)
        # empty models dict → falsy → None
        assert sensor.native_value is None

    def test_returns_timestamp_when_models_present(self):
        data = _make_coordinator_data(
            string_bucket_models={"sensor.pv1": _EXAMPLE_MODELS},
            bucket_models_timestamp=_EXAMPLE_TS,
        )
        sensor = _make_sensor(data=data)
        assert sensor.native_value == _EXAMPLE_TS

    def test_returns_none_for_different_string(self):
        """Sensor for pv2 when only pv1 has models → None."""
        data = _make_coordinator_data(
            string_bucket_models={"sensor.pv1": _EXAMPLE_MODELS},
            bucket_models_timestamp=_EXAMPLE_TS,
        )
        sensor = _make_sensor("sensor.pv2", data=data)
        assert sensor.native_value is None

    def test_timestamp_is_valid_iso(self):
        from datetime import datetime

        data = _make_coordinator_data(
            string_bucket_models={"sensor.pv1": _EXAMPLE_MODELS},
            bucket_models_timestamp=_EXAMPLE_TS,
        )
        sensor = _make_sensor(data=data)
        ts = sensor.native_value
        assert ts is not None
        # Must parse without error
        datetime.fromisoformat(ts)


class TestBucketModelSensorAttributes:
    def test_attributes_empty_when_no_models(self):
        sensor = _make_sensor()
        assert sensor.extra_state_attributes == {}

    def test_attributes_match_models(self):
        data = _make_coordinator_data(
            string_bucket_models={"sensor.pv1": _EXAMPLE_MODELS},
            bucket_models_timestamp=_EXAMPLE_TS,
        )
        sensor = _make_sensor(data=data)
        attrs = sensor.extra_state_attributes

        assert "08:00" in attrs
        assert "12:00" in attrs
        assert "12:05" in attrs
        assert attrs["08:00"] == [0.912]
        assert attrs["12:00"] == [0.743, 12.5]
        assert attrs["12:05"] == [0.031, 0.761, 0.0]

    def test_attributes_sorted_by_bucket(self):
        data = _make_coordinator_data(
            string_bucket_models={"sensor.pv1": _EXAMPLE_MODELS},
            bucket_models_timestamp=_EXAMPLE_TS,
        )
        sensor = _make_sensor(data=data)
        keys = list(sensor.extra_state_attributes.keys())
        assert keys == sorted(keys)

    def test_attribute_values_are_lists(self):
        data = _make_coordinator_data(
            string_bucket_models={"sensor.pv1": _EXAMPLE_MODELS},
            bucket_models_timestamp=_EXAMPLE_TS,
        )
        sensor = _make_sensor(data=data)
        for val in sensor.extra_state_attributes.values():
            assert isinstance(val, list)


class TestBucketModelSensorMetadata:
    def test_unique_id_suffix(self):
        sensor = _make_sensor("sensor.my_pv_west")
        assert "my_pv_west_bucket_models" in sensor._attr_unique_id

    def test_name_contains_slug(self):
        sensor = _make_sensor("sensor.my_pv_west")
        assert "my_pv_west" in sensor._attr_name.lower()

    def test_entity_category_is_diagnostic(self):
        from shady.sensor import BucketModelSensor

        assert BucketModelSensor._attr_entity_category is not None

    def test_icon(self):
        from shady.sensor import BucketModelSensor

        assert BucketModelSensor._attr_icon == "mdi:chart-scatter-plot"

    def test_no_unit_or_device_class(self):
        from shady.sensor import BucketModelSensor

        assert BucketModelSensor._attr_native_unit_of_measurement is None
        assert BucketModelSensor._attr_device_class is None
        assert BucketModelSensor._attr_state_class is None


class TestBucketModelSensorAbsentWithNoPVStrings:
    @pytest.mark.asyncio
    async def test_sensor_not_created_without_pv_sensors(self):
        """async_setup_entry must not create BucketModelSensor when pv_sensors is empty."""
        from unittest.mock import patch
        from shady.sensor import BucketModelSensor, async_setup_entry

        # Simulate entry with no pv sensors
        entry = MagicMock()
        entry.entry_id = "no_pv"
        entry.options = {"pv_sensors": []}

        coordinator = _make_coordinator()
        hass = MagicMock()
        hass.data = {"shady": {entry.entry_id: coordinator}}

        added: list = []

        def _add(entities):
            added.extend(entities)

        with patch("shady.sensor.DOMAIN", "shady"):
            await async_setup_entry(hass, entry, _add)

        bucket_sensors = [e for e in added if isinstance(e, BucketModelSensor)]
        assert bucket_sensors == []
