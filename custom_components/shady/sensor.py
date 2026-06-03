"""Sensors for Shady.

Published sensors (entity IDs are prefixed with the device name "shady_" by HA):

  Aggregate (always present):
    solar_forecast_hourly            – corrected Wh for the current 5-min slot
                                       attr 'forecast': today's {ts: Wh} at native resolution
                                       attr 'forecast_tomorrow': tomorrow's {hour-ts: Wh} hourly
    solar_forecast_today             – total corrected Wh for today
    solar_forecast_remaining         – corrected Wh remaining today (from now)
    solar_forecast_hourly_raw        – raw (uncorrected) Wh for the current slot
                                       attr 'forecast': full raw {ts: Wh} dict

  Per configured PV string (one sensor per non-empty pv_sensor_N config key):
    solar_forecast_hourly_<slug>     – corrected Wh current slot for that string
                                       attr 'forecast': per-string today {ts: Wh}
                                       attr 'pv_sensor': source entity_id

  Note: slots whose 5-min bucket has no fitted model default to 0.0.
"""

from __future__ import annotations

import logging
import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from datetime import timedelta

from homeassistant.util import dt as dt_util

from .const import DOMAIN, PV_SENSOR_KEYS
from .coordinator import CoordinatorData, ShadyCoordinator
from .math_utils import parse_dt as _parse_dt

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ShadyCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        SolarForecastCurrentSensor(coordinator, entry),
        SolarForecastTodaySensor(coordinator, entry),
        SolarForecastRemainingSensor(coordinator, entry),
        SolarForecastCurrentRawSensor(coordinator, entry),
    ]

    # One hourly sensor per configured PV string
    d = entry.options or entry.data
    for key in PV_SENSOR_KEYS:
        entity_id = d.get(key, "")
        if entity_id:
            entities.append(
                SolarForecastStringCurrentSensor(coordinator, entry, key, entity_id)
            )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _Base(CoordinatorEntity[ShadyCoordinator], SensorEntity):  # type: ignore[misc]
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR

    def __init__(
        self,
        coordinator: ShadyCoordinator,
        entry: ConfigEntry,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Shady",
            manufacturer="Enrico Schnepel",
            entry_type="service",
        )

    @property
    def _data(self) -> CoordinatorData:
        return self.coordinator.data or CoordinatorData()

    def _current_slot_value(self, slots: dict[str, float]) -> float:
        """Return the value for the current time from a {ts: value} dict.

        Lookup order:
          1. Exact 5-min snapped key  (e.g. T10:15:00+…)
          2. Prefix match on HH:MM    (handles tz-offset format variations)
          3. Hourly key T10:00:00+…   (for raw_forecast and tomorrow slots)
          4. Any slot starting with T10:  (current hour, any minute)
          5. 0.0                          (night / no data)
        """
        if not slots:
            return 0.0
        now = dt_util.now().replace(second=0, microsecond=0)
        snapped_min = (now.minute // 5) * 5
        snapped = now.replace(minute=snapped_min)

        # 1. Exact 5-min key
        key = snapped.isoformat()
        if key in slots:
            return slots[key]

        # 2. Prefix HH:MM match
        prefix = snapped.strftime("%Y-%m-%dT%H:%M")
        for ts, val in slots.items():
            if ts.startswith(prefix):
                return val

        # 3. Hourly key (raw_forecast uses minute=0 keys)
        hour_key = now.replace(minute=0).isoformat()
        if hour_key in slots:
            return slots[hour_key]

        # 4. Any slot in current hour
        hour_prefix = snapped.strftime("%Y-%m-%dT%H:")
        for ts, val in slots.items():
            if ts.startswith(hour_prefix):
                return val

        # 5. No slot for this time (e.g. night, no training data)
        return 0.0


# ---------------------------------------------------------------------------
# Sensor 1: aggregate current slot (corrected)
# ---------------------------------------------------------------------------


class SolarForecastCurrentSensor(_Base):
    _attr_name = "Solar Forecast Hourly"
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "solar_forecast_hourly")

    @property
    def native_value(self) -> float | None:
        return self._current_slot_value(self._data.forecast_today)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "forecast": self._data.forecast_today,
            "forecast_tomorrow": self._data.forecast_tomorrow,
        }


# ---------------------------------------------------------------------------
# Sensor 2: today total
# ---------------------------------------------------------------------------


class SolarForecastTodaySensor(_Base):
    _attr_name = "Solar Forecast Today"
    _attr_icon = "mdi:white-balance-sunny"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "solar_forecast_today")

    @property
    def native_value(self) -> float | None:
        d = self._data
        return d.today_total if d.forecast_today else None


# ---------------------------------------------------------------------------
# Sensor 3: remaining today
# ---------------------------------------------------------------------------


class SolarForecastRemainingSensor(_Base):
    _attr_name = "Solar Forecast Remaining"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "solar_forecast_remaining")

    @property
    def native_value(self) -> float | None:
        d = self._data
        return d.remaining if d.forecast_today else None


# ---------------------------------------------------------------------------
# Sensor 4: raw current slot
# ---------------------------------------------------------------------------


class SolarForecastCurrentRawSensor(_Base):
    _attr_name = "Solar Forecast Hourly Raw"
    _attr_icon = "mdi:solar-power-variant-outline"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "solar_forecast_hourly_raw")

    @property
    def native_value(self) -> float | None:
        return self._current_slot_value(self._data.raw_forecast)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"forecast": self._data.raw_forecast}


# ---------------------------------------------------------------------------
# Sensor 5+: per-string current slot
# ---------------------------------------------------------------------------


def _entity_id_to_slug(entity_id: str) -> str:
    slug = entity_id.split(".", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")


class SolarForecastStringCurrentSensor(_Base):
    """Corrected forecast for a single PV string, current 5-min slot."""

    _attr_icon = "mdi:solar-panel"

    def __init__(
        self,
        coordinator: ShadyCoordinator,
        entry: ConfigEntry,
        conf_key: str,
        pv_entity_id: str,
    ) -> None:
        slug = _entity_id_to_slug(pv_entity_id)
        super().__init__(coordinator, entry, f"solar_forecast_hourly_{conf_key}")
        self._pv_entity_id = pv_entity_id
        self._attr_name = f"Solar Forecast Hourly {slug}"

    @property
    def native_value(self) -> float | None:
        slots = self._data.string_forecasts.get(self._pv_entity_id, {})
        # Per-string dict contains all slots; filter to today for current lookup
        now = dt_util.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        today_slots = {
            ts: wh
            for ts, wh in slots.items()
            if today_start <= _parse_dt(ts) < tomorrow_start
        }
        return self._current_slot_value(today_slots)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "forecast": self._data.string_forecasts.get(self._pv_entity_id, {}),
            "pv_sensor": self._pv_entity_id,
        }
